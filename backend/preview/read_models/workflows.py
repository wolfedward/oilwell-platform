import csv,io,json
from datetime import datetime,timezone
from types import SimpleNamespace
from backend.services.commands import CommandService
from backend.services.workflows import WorkflowService
from backend.services.reports import ReportService
from ..snapshot_policy import redact_json
from ..repository import Download
from .acquisition import page_query
from .energy import report
from .rehearsal_views import project,object_paths,history,paginated

REPORTS=('production','diagnosis','data-quality','strategy','energy','work-orders')
def csv_report(dataset,kind,query):
    result=report(dataset,kind,query);out=io.StringIO(newline='');writer=csv.writer(out)
    cell=lambda v:ReportService._csv_value(json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v)
    writer.writerow(['report_type',kind]);writer.writerow(['source','历史快照 · 页面预览'])
    for k,v in result['filters'].items():writer.writerow(['filter.'+k,cell(v)])
    for k,v in result['summary'].items():writer.writerow(['summary.'+k,cell(v)])
    keys=list(result['items'][0]) if result['items'] else []
    writer.writerow(keys)
    for row in result['items']:writer.writerow([cell(row.get(k)) for k in keys])
    return out.getvalue().encode('utf-8-sig')

def register(repository):
    ds=repository.dataset;now=datetime.fromisoformat((repository.meta.get('data_to') or '2026-09-12')[:10]+'T23:59:59+08:00')
    workflow=WorkflowService(ds,None,lambda:now);commands=CommandService(ds,None,SimpleNamespace(now=now))
    base='/api/preview/legacy/api'
    def clean(data):
        if isinstance(data,dict):return {k:clean(v) for k,v in data.items() if k not in {'signature','idempotency_key','nonce'}}
        if isinstance(data,list):return [clean(v) for v in data]
        return data
    def command_list(q,a):
        # Intermediate receipt states are not command ledger terminal states.
        if q.get('state') in {'received','validated','executing'}:return {'items':[],'total':0,'page':1,'page_size':20}
        return clean(commands.list_commands(page_query(q)))
    def order(row):
        deadline=row.get('due_at');row['overdue']=bool(deadline and row['status'] not in ('closed','completed','cancelled') and datetime.fromisoformat(deadline.replace('Z','+00:00'))<now)
        return row
    def orders(q,a):
        result=workflow.list_work_orders({'page':1,'page_size':100});rows=result['items']
        for p in range(2,(result['total']+99)//100+1):rows+=workflow.list_work_orders({'page':p,'page_size':100})['items']
        mapped={r['id']:project(repository,'/api/work-orders/'+r['id'],r) for r in rows}
        for path in object_paths(repository,'/api/work-orders'):
            identifier=path.rsplit('/',1)[-1]
            if identifier not in mapped and history(repository,path)[0]['action']=='create':mapped[identifier]=project(repository,path,{})
        return paginated([order(r) for r in reversed(list(mapped.values()))],q)
    def order_detail(identifier):
        path='/api/work-orders/'+identifier;events=history(repository,path)
        original={} if events and events[0]['action']=='create' else workflow.get_work_order(identifier)
        return order(project(repository,path,original))
    def trace(identifier):
        events=history(repository,'/api/work-orders/'+identifier)
        if events:return {'work_order_id':identifier,'work_order':order_detail(identifier),'alarm_id':events[0]['values'].get('alarm_id'),'audit_ids':[],'approval_ids':[],'source_kind':'preview_rehearsal'}
        return workflow.trace(identifier)
    repository.register('GET',base+'/commands',command_list)
    repository.register('GET',base+r'/commands/(?P<identifier>[\w-]+)',lambda q,a,identifier:clean(commands.get(identifier)))
    repository.register('GET',base+'/alarms',lambda q,a:workflow.list_alarms(page_query(q)))
    repository.register('GET',base+r'/alarms/(?P<identifier>[\w-]+)',lambda q,a,identifier:workflow.get_alarm(identifier))
    repository.register('GET',base+'/work-orders',orders)
    repository.register('GET',base+r'/work-orders/(?P<identifier>[\w-]+)',lambda q,a,identifier:order_detail(identifier))
    repository.register('GET',base+r'/work-orders/(?P<identifier>[\w-]+)/trace',lambda q,a,identifier:trace(identifier))
    for kind in REPORTS:
        if kind!='energy':repository.register('GET',base+'/reports/'+kind,lambda q,a,kind=kind:report(ds,kind,q))
        repository.register('GET',base+'/reports/'+kind+r'\.csv',lambda q,a,kind=kind:Download(csv_report(ds,kind,q),'text/csv; charset=utf-8',kind+'-report.csv'))

def export_reads(repository,actor):
    from .dynagraphs import export_fixture_reads
    result=export_fixture_reads(repository,actor)
    paths=['/api/strategies','/api/commands','/api/commands/C1','/api/alarms','/api/alarms/A1','/api/work-orders','/api/work-orders/O1','/api/work-orders/O1/trace','/api/users','/api/roles','/api/audit','/api/health','/api/acceptance/evidence',*['/api/reports/'+t for t in REPORTS]]
    for p in paths:result[p]=repository.read('/api/preview/legacy'+p,{},actor)
    return result
