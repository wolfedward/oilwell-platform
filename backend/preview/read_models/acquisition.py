from datetime import datetime,timezone
from copy import deepcopy
import json
import re
from backend.services.acquisition import AcquisitionService
from backend.services.devices import DevicesService
from ..errors import PreviewError
from ..fixtures.acquisition import fixtures
from ..repository import Download
from .monitoring import well_scope


def page_query(query):
    try:
        page=int(query.get('page',1));size=int(query.get('page_size',20))
        if page<1 or size<1 or size>100:raise ValueError()
    except (TypeError,ValueError):raise PreviewError('invalid_pagination','分页范围无效')
    return {**query,'page':page,'page_size':size}


def fixture_read(routes,path,query):
    key=path+('?view='+query['view'] if query.get('view') else '')
    if key not in routes:raise PreviewError('not_found','此样例视图不存在',404)
    result=deepcopy(routes[key])
    if isinstance(result,Download):return result
    if 'items' in result:
        q=page_query(query);items=result['items']
        for field in ('gateway_id','device_id','area_code','well_code','status','state','kind','purpose'):
            if query.get(field):items=[r for r in items if str(r.get(field,''))==str(query[field])]
        size=q['page_size'];start=(q['page']-1)*size
        result.update(items=items[start:start+size],total=len(items),page=q['page'],page_size=size,has_more=start+size<len(items))
    return result


def install_fixtures(repository,routes):
    # Every path and view exists in the fixture index; no catch-all successful DTO.
    for path in sorted({key.split('?')[0] for key in routes}):
        wire=path if path.startswith('/api/preview/') else '/api/preview/legacy'+path
        repository.register('GET',re.escape(wire),lambda q,a,path=path:fixture_read(routes,path,q))


class SnapshotRegistry:
    def __init__(self,dataset,stamp):
        self.stamp=datetime.fromisoformat((stamp or '2026-09-12T02:00:00+00:00').replace('Z','+00:00'))
        self.latest_registers={}
        for row in dataset.query_all('SELECT t.device_id,t.metric_code,t.value FROM telemetry t JOIN (SELECT device_id,metric_code,max(id) AS id FROM telemetry GROUP BY device_id,metric_code) latest ON t.id=latest.id'):
            self.latest_registers.setdefault(row['device_id'],{})[row['metric_code']]=row['value']
    def now(self):return self.stamp
    def get(self,identifier):return None


def register(repository):
    ds=repository.dataset;devices=DevicesService(ds,None,SnapshotRegistry(ds,repository.meta.get('snapshot_at')))
    def stream(q,a):
        try:after=int(q.get('after_id',0));limit=min(100,max(1,int(q.get('limit',100))))
        except (ValueError,TypeError):raise PreviewError('invalid_cursor','游标无效')
        sql='SELECT * FROM raw_messages WHERE id>? ORDER BY id LIMIT ?'
        rows=ds.query_all(sql,(after,limit));items=[AcquisitionService._message(r,include_raw=False) for r in rows]
        return {'items':items,'cursor':items[-1]['id'] if items else after,'queue':{},'worker':{'accepted':ds.scalar("SELECT count(*) FROM raw_messages WHERE status IN ('accepted','replayed')"),'rejected':ds.scalar("SELECT count(*) FROM raw_messages WHERE status='rejected'")},'live':False,'source_kind':'snapshot'}
    def message(q,a,message_id):
        row=ds.query_one('SELECT * FROM raw_messages WHERE id=?',(message_id,))
        if not row:raise PreviewError('not_found','报文不存在',404)
        return AcquisitionService._message(row,include_raw=True)
    def quality(q,a):
        filters=page_query(q);rows=ds.query_all('SELECT * FROM data_quality_events ORDER BY id DESC')
        items=[AcquisitionService._quality_item(r) for r in rows]
        for key in ('device_id','status','kind'):
            if q.get(key):items=[r for r in items if r.get(key)==q[key]]
        start=(filters['page']-1)*filters['page_size']
        return {**filters,'total':len(items),'items':items[start:start+filters['page_size']]}
    base='/api/preview/legacy/api'
    repository.register('GET',base+'/acquisition/stream',stream)
    repository.register('GET',base+r'/acquisition/messages/(?P<message_id>\d+)',message)
    repository.register('GET',base+'/data-quality',quality)
    repository.register('GET',base+'/devices',lambda q,a:devices.list_devices(page_query(q)))
    repository.register('GET',base+r'/devices/(?P<device_id>[\w-]+)',lambda q,a,device_id:devices.detail(device_id))
    repository.register('GET',base+r'/devices/(?P<device_id>[\w-]+)/registers',lambda q,a,device_id:devices.registers(device_id))
    for endpoint in ('health','quality'):
        repository.register('GET',base+'/central/'+endpoint,lambda q,a:{'status':'not_configured','summary':{},'links':[]})
    routes=fixtures(well_scope(ds,{}));install_fixtures(repository,routes)
    from ..fixtures.acquisition import uid
    raw=routes['/api/ingest-reliability/facts/'+uid('fact')+'/raw']['text'].encode()
    repository.register('GET',base+'/waveforms/facts/'+uid('fact')+'/raw',lambda q,a:Download(raw,'application/json','waveform-fact-preview.json'))


def export_fixture_reads(repository,actor):
    """Materialize only explicit preview reads for disconnected browser tests."""
    paths=list(fixtures(well_scope(repository.dataset,{})))+['/api/acquisition/stream','/api/acquisition/messages/1','/api/devices','/api/devices/D1','/api/devices/D1/registers','/api/data-quality','/api/central/health','/api/central/quality']
    result={}
    for key in paths:
        path,_,query=key.partition('?');q=dict(part.split('=',1) for part in query.split('&') if part)
        result[key]=repository.read(path if path.startswith('/api/preview/') else '/api/preview/legacy'+path,q,actor)
    return result
