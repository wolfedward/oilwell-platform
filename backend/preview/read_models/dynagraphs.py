from copy import deepcopy
import json,re
from backend.services.analysis import AnalysisService
from ..fixtures.dynagraphs import cards,inversion_fixtures
from ..errors import PreviewError
from .monitoring import well_scope,monitoring
from .acquisition import page_query

def register(repository):
    ds=repository.dataset;service=AnalysisService(ds,None);profiles=cards()
    def detail(q,a,identifier):
        if identifier.startswith('preview-card-'):
            code=identifier[len('preview-card-'):]
            if code not in profiles:raise PreviewError('not_found','功图样例不存在',404)
            return deepcopy(profiles[code])
        return service.detail(identifier)
    base='/api/preview/legacy/api'
    repository.register('GET',base+'/dynagraphs',lambda q,a:service.list_dynagraphs(page_query(q)))
    repository.register('GET',base+r'/dynagraphs/(?P<identifier>[\w-]+)',detail)
    repository.register('GET',base+'/model-runs',lambda q,a:{'items':[],'page':1,'page_size':20,'total':0} if str(q.get('dynagraph_id','')).startswith('preview-card-') else service.model_runs(page_query(q)))
    def view(q,a):
        summary=monitoring(ds,q)
        recent=service.list_dynagraphs({'page':1,'page_size':30,**({'well_id':q['well_id']} if q.get('well_id') else {})})
        return {'profiles':list(profiles.values()),'recent':recent['items'],'wells':well_scope(ds,{}),'from':summary['from'],'to':summary['to'],'production':summary['production'],'selected':summary['selected'],
                'rules':[{'id':'preview-rule-load','name':'峰值载荷偏高','metric':'max_load_kn','operator':'>','threshold':80,'unit':'kN','duration':'连续 3 周期','scope':'全部测试井','version':1,'status':'enabled','hits':2},
                         {'id':'preview-rule-fillage','name':'充满度不足','metric':'fillage','operator':'<','threshold':.5,'unit':'1','duration':'连续 5 周期','scope':'低产井','version':1,'status':'enabled','hits':4}]}
    repository.register('GET','/api/preview/views/dynagraphs',view)
    routes=inversion_fixtures()
    for path in routes:
        def reader(q,a,path=path):
            result=deepcopy(routes[path])
            if 'items' in result or 'rows' in result:
                try:offset=int(q.get('offset',0));limit=int(q.get('limit',1000 if 'rows' in result else 20))
                except ValueError:raise PreviewError('invalid_pagination','分页数值无效')
                if offset<0 or limit<1 or limit>(1000 if 'rows' in result else 100):raise PreviewError('invalid_pagination','分页范围无效')
                key='rows' if 'rows' in result else 'items';result[key]=result[key][offset:offset+limit];result.update(offset=offset,limit=limit)
            return result
        repository.register('GET',re.escape('/api/preview/legacy'+path),reader)

def export_fixture_reads(repository,actor):
    from .acquisition import export_fixture_reads as acquisition_reads
    result=acquisition_reads(repository,actor)
    paths=['/api/dynagraphs','/api/dynagraphs/DG1','/api/model-runs','/api/preview/views/dynagraphs',*['/api/dynagraphs/'+x['id'] for x in cards().values()],*inversion_fixtures()]
    for path in paths:result[path]=repository.read(path if path.startswith('/api/preview/') else '/api/preview/legacy'+path,{},actor)
    return result
