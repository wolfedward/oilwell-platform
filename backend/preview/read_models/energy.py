from copy import deepcopy
from datetime import datetime
import json
from backend.services.strategies import StrategyService
from backend.services.reports import ReportService,SHANGHAI
from ..fixtures.energy import strategies,effects
from ..fixtures.studies import records
from ..errors import PreviewError
from .monitoring import period
from .acquisition import page_query
from .rehearsal_views import project,paginated

class SnapshotStrategies(StrategyService):
    def _public(self,row):
        result=dict(row);result['configuration']=json.loads(row['configuration'] or '{}')
        # No legacy automatic fitting or optimization on a read request.
        config=result['configuration']
        if not config.get('selected_candidate'):
            config['selected_candidate']={k:config[k] for k in ('on_minutes','off_minutes','starts_per_day','predicted_liquid_m3','predicted_energy_kwh','saving_rate') if k in config}
        return result
    def _effect_trace(self,strategy):
        return {'valid_for_comparison':False,'strategy_id':strategy['id'],'conclusion':'历史版本原值保留；请在效果页选择观测窗口核对覆盖率。'}

def report(dataset,kind,query):
    start,end=period(dataset,query);at=datetime.fromisoformat((end or '2026-09-12')+'T23:59:59').replace(tzinfo=SHANGHAI)
    allowed={'unit_id','well_id','strategy_id','from','to','status','assignee_id'}
    filters={k:v for k,v in query.items() if k in allowed and v and not (k=='strategy_id' and str(v).startswith('preview-'))}
    if start:filters.setdefault('from',start);filters.setdefault('to',end)
    return ReportService(dataset,lambda:at).report(kind,filters)

def register(repository):
    ds=repository.dataset;service=SnapshotStrategies(ds,None);fixed=strategies()
    def listing(q,a):
        pq=page_query(q);source=service.list_strategies({'page':1,'page_size':100})
        # Page through all original rows, never truncate a long source history.
        items=source['items']
        for page in range(2,(source['total']+99)//100+1):items+=service.list_strategies({'page':page,'page_size':100})['items']
        rows=[project(repository,'/api/strategies/'+x['id'],deepcopy(x)) for x in fixed+items]
        return paginated(rows,q)
    def detail(q,a,identifier):
        for row in fixed:
            if row['id']==identifier:return project(repository,'/api/strategies/'+identifier,deepcopy(row))
        return project(repository,'/api/strategies/'+identifier,service.detail(identifier))
    repository.register('GET','/api/preview/legacy/api/strategies',listing)
    repository.register('GET',r'/api/preview/legacy/api/strategies/(?P<identifier>[\w-]+)',detail)
    repository.register('GET','/api/preview/legacy/api/reports/energy',lambda q,a:report(ds,'energy',q))
    repository.register('GET','/api/preview/views/energy',lambda q,a:{'effects':effects(),'schedule':deepcopy(next(r for r in records() if r['kind']=='energy_schedule' and r['status']=='feasible')),'rejected':deepcopy(next(r for r in records() if r['status']=='no_feasible_candidate')),'source_kind':'software_fixture'})
