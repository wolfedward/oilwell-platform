from datetime import datetime,timezone
import hashlib
import json
import re
import uuid
from .errors import PreviewError
from .permissions import allowed_action,require_object_action
from .snapshot_policy import redact_json

ACTIONS={'edit','create','submit','approve','reject','review','acknowledge','close','assign','configure','scenario','draft','control_plan','calculate_input'}


def save_rehearsal(state,actor,request_id,action,object_id,expected_version,values):
    if action not in ACTIONS or not allowed_action(actor['role_code'],action): raise PreviewError('forbidden','此岗位不能进行该演练',403)
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',request_id): raise PreviewError('invalid_request','缺少独立演练请求标识')
    if not isinstance(object_id,str) or not 1<=len(object_id)<=200 or any(ord(c)<32 for c in object_id): raise PreviewError('invalid_object','演练对象无效')
    if type(expected_version) is not int or expected_version<0 or not isinstance(values,dict): raise PreviewError('invalid_version','演练版本或表单无效')
    require_object_action(actor,object_id,action)
    values=json.loads(redact_json(json.dumps(values,ensure_ascii=False,allow_nan=False)))
    serialized=json.dumps({'action':action,'object_id':object_id,'expected_version':expected_version,'values':values},ensure_ascii=False,sort_keys=True,allow_nan=False)
    if len(serialized.encode('utf-8'))>256*1024: raise PreviewError('body_limit','演练表单过大',413)
    fingerprint=hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    if action=='create' and object_id in {'/api/work-orders','/api/users','/api/wells','/api/roles'}:
        object_id+='/preview-'+uuid.uuid5(uuid.NAMESPACE_URL,actor['id']+'/'+request_id).hex
    with state.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT fingerprint,result FROM rehearsal_events WHERE actor_id=? AND request_id=?',(actor['id'],request_id)).fetchone()
        if existing:
            if existing['fingerprint']!=fingerprint: raise PreviewError('idempotency_conflict','重试内容与原请求不一致',409)
            return json.loads(existing['result'])
        latest=db.execute('SELECT result FROM rehearsal_events WHERE object_id=? ORDER BY version DESC LIMIT 1',(object_id,)).fetchone()
        previous=json.loads(latest[0]) if latest else {'version':0,'values':{}}
        if previous['version']!=expected_version: raise PreviewError('version_conflict','记录已变化，请重新加载',409)
        author=previous.get('submitted_by')
        if action in {'approve','reject','review'} and author==actor['id']: raise PreviewError('self_review','不能复核本人提交的演练记录',403)
        if action=='close' and object_id.startswith('/api/work-orders/') and author==actor['id']:raise PreviewError('self_review','工单必须由另一操作者独立关闭',403)
        if action in {'approve','reject'} and not author: raise PreviewError('not_submitted','先创建独立的提交演练，再由另一操作者复核',409)
        result={'id':object_id.rsplit('/',1)[-1],'object_id':object_id,'request_id':request_id,'actor_id':actor['id'],'action':action,
                'version':expected_version+1,'values':{**previous.get('values',{}),**values},'mode':'rehearsal',
                'submitted_by':actor['id'] if action=='submit' else author,'created_at':datetime.now(timezone.utc).isoformat(),
                'status':'input_saved' if action=='calculate_input' else 'rehearsed','device_sent':False}
        db.execute('INSERT INTO rehearsal_events(request_id,actor_id,object_id,action,version,fingerprint,payload,result) VALUES(?,?,?,?,?,?,?,?)',
                   (request_id,actor['id'],object_id,action,result['version'],fingerprint,serialized,json.dumps(result,ensure_ascii=False)))
        return result
