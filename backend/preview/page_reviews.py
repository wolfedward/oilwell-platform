"""User signoff is explicit, versioned, and separate from rehearsal reset."""
from datetime import datetime,timezone
import json
from .errors import PreviewError
OPEN_INTERFACE={'id':'open-interface','label':'厂家开放接口与设备参考','group':'开放资料','annual_tasks':['Y1-03','Y2-02','Y3-02'],'content_version':'1'}
class PageReviews:
    def __init__(self,state,catalog):
        self.state=state;self.catalog=[*catalog,OPEN_INTERFACE]
        with state.connection() as db:db.execute('CREATE TABLE IF NOT EXISTS page_reviews(sequence INTEGER PRIMARY KEY,page_id TEXT NOT NULL,version INTEGER NOT NULL,content_version TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(page_id,version))')
    def get_page_reviews(self,catalog_version):
        with self.state.connection() as db:
            saved={r['page_id']:json.loads(r['payload']) for r in db.execute('SELECT r.* FROM page_reviews r JOIN (SELECT page_id,max(version) v FROM page_reviews GROUP BY page_id) x ON x.page_id=r.page_id AND x.v=r.version')}
        result=[]
        for page in self.catalog:
            row=saved.get(page['id'],{});current=row.get('content_version')==page['content_version']
            result.append({**page,'page_id':page['id'],'version':row.get('version',0),'decision':row.get('decision','pending') if current else 'pending','comment':row.get('comment',''),'actor_id':row.get('actor_id'),'reviewed_at':row.get('reviewed_at'),'content_changed':bool(row) and not current,'previous_content_version':row.get('content_version'),'catalog_version':catalog_version})
        return result
    def save_page_review(self,actor,page_id,content_version,expected_version,decision,comment):
        page=next((p for p in self.catalog if p['id']==page_id),None)
        if page is None:raise PreviewError('not_found','页面不存在',404)
        if actor.get('role_code') not in {'administrator','reviewer'}:raise PreviewError('forbidden','仅管理员和业务复核员可确认页面',403)
        if content_version!=page['content_version']:raise PreviewError('content_changed','页面内容已更新，请重新查看',409)
        if type(expected_version) is not int or expected_version<0 or decision not in {'pending','changes_requested','approved'} or not isinstance(comment,str) or len(comment)>2000:raise PreviewError('invalid_review','审查意见格式无效')
        if decision=='changes_requested' and not comment.strip():raise PreviewError('comment_required','请填写具体修改意见')
        with self.state.connection() as db:
            db.execute('BEGIN IMMEDIATE');version=db.execute('SELECT coalesce(max(version),0) FROM page_reviews WHERE page_id=?',(page_id,)).fetchone()[0]
            if expected_version!=version:raise PreviewError('version_conflict','意见已被更新，请刷新后确认',409)
            row={'page_id':page_id,'content_version':content_version,'version':version+1,'decision':decision,'comment':comment.strip(),'actor_id':actor['id'],'reviewed_at':datetime.now(timezone.utc).isoformat()}
            db.execute('INSERT INTO page_reviews(page_id,version,content_version,payload) VALUES(?,?,?,?)',(page_id,version+1,content_version,json.dumps(row,ensure_ascii=False)))
        return row
