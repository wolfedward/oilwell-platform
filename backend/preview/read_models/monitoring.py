"""Snapshot-backed monitoring. No clock shifting or calendar-normalized curves."""
from datetime import date,timedelta
import math
from backend.services.wells import WellsService
from ..errors import PreviewError
from ..fixtures.monitoring import supplementary


def well_scope(dataset,query):
    where=[];args=[]
    for key,column in [('well_id','w.id'),('unit_id','u.id'),('group_id','g.id')]:
        if query.get(key):where.append(column+'=?');args.append(query[key])
    sql='SELECT w.*,g.name AS group_name,u.name AS unit_name,u.id AS unit_id FROM wells w JOIN well_groups g ON g.id=w.group_id JOIN units u ON u.id=g.unit_id'
    return dataset.query_all(sql+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY w.code,w.id',tuple(args))


def period(dataset,query):
    latest=dataset.scalar('SELECT max(day) FROM production_daily')
    if not latest:return None,None
    try:
        end=date.fromisoformat(query.get('to') or latest)
        start=date.fromisoformat(query.get('from') or (end-timedelta(days=13)).isoformat())
        if start>end or (end-start).days>366:raise ValueError()
        return start.isoformat(),end.isoformat()
    except ValueError:raise PreviewError('invalid_period','日期区间无效，最长一年')


def rows_for_wells(dataset,table,ids,time_column,start,end,limit=None):
    if not ids or not start:return []
    sql=f'SELECT * FROM {table} WHERE well_id IN ({",".join("?" for _ in ids)}) AND substr({time_column},1,10) BETWEEN ? AND ? ORDER BY {time_column},well_id'
    if limit:sql=sql.rsplit(' ORDER BY ',1)[0]+f' ORDER BY {time_column} DESC,id DESC LIMIT {int(limit)}'
    rows=dataset.query_all(sql,tuple(ids)+(start,end))
    return list(reversed(rows)) if limit else rows


def dashboard(dataset,query):
    wells=well_scope(dataset,query);ids=[x['id'] for x in wells];start,end=period(dataset,query)
    rows=rows_for_wells(dataset,'production_daily',ids,'day',start,end)
    days={}
    for row in rows:
        item=days.setdefault(row['day'],{'day':row['day'],'daily_fluid':0,'daily_oil':0,'power_kwh':0,'pump_efficiency':0,'n':0})
        for key in ('daily_fluid','daily_oil','power_kwh','pump_efficiency'):item[key]+=row[key]
        item['n']+=1
    trends=[]
    for day,item in sorted(days.items()):item['pump_efficiency']/=item.pop('n');trends.append(item)
    production=trends[-1] if trends else {'day':None,'daily_fluid':None,'daily_oil':None,'power_kwh':None,'pump_efficiency':None}
    def records(table):
        return dataset.query_all(f'SELECT * FROM {table} WHERE well_id IN ({",".join("?" for _ in ids)})',tuple(ids)) if ids else []
    devices=records('devices');alarms=records('alarms');quality=records('data_quality_events');strategies=records('energy_strategies')
    active=[x for x in alarms if x['status'] not in ('closed','resolved')]
    def correlation():
        if len(trends)<2:return None
        xs=[r['daily_fluid'] for r in trends];ys=[r['power_kwh'] for r in trends];ax=sum(xs)/len(xs);ay=sum(ys)/len(ys)
        denominator=math.sqrt(sum((x-ax)**2 for x in xs)*sum((y-ay)**2 for y in ys))
        return sum((x-ax)*(y-ay) for x,y in zip(xs,ys))/denominator if denominator else None
    latest=rows_for_wells(dataset,'telemetry',ids,'measured_at',start,end,600)
    from .energy import report
    energy_report=report(dataset,'energy',query)
    energy=energy_report['summary']
    if query.get('group_id'):
        effects=[r for r in energy_report['items'] if r['well_id'] in ids]
        comparable=[r for r in effects if r['valid_for_comparison']]
        baseline=sum(r['baseline_energy_kwh'] for r in comparable);after=sum(r['after_energy_kwh'] for r in comparable)
        energy={'comparable_strategy_count':len(comparable),'insufficient_coverage_count':len(effects)-len(comparable),'baseline_energy_kwh':baseline,'after_energy_kwh':after,'energy_saved_kwh':baseline-after if comparable else None,'saving_rate':(baseline-after)/baseline if baseline else None}
    return {'total_wells':len(wells),'active_wells':sum(w['status'] in ('active','running','intermittent') for w in wells),
            'stopped_wells':sum(w['status']=='stopped' for w in wells),'offline_wells':sum(w['status'] in ('offline','maintenance') for w in wells),
            'low_output_wells':sum(w['is_low_output'] for w in wells),'production':production,'data_timestamp':latest[-1]['measured_at'] if latest else None,
            'devices':{'online':sum(d['status']=='online' for d in devices),'total':len(devices)},
            'alarms':{'active':len(active),'high':sum(a['severity'] in ('high','critical') for a in active)},
            'quality':{'open_events':sum(q['status'] not in ('closed','resolved','repaired') for q in quality),'resolved_events':sum(q['status'] in ('closed','resolved','repaired') for q in quality)},
            'strategies':{'pending_strategies':sum(s['status'] in ('submitted','pending_approval') for s in strategies),'effective_strategies':sum(s['status'] in ('active','effective') for s in strategies)},
            'energy':energy,'trends':trends,'trend_metadata':{'label':'历史快照生产日报','basis':'原始 production_daily 同井同日汇总；未调整原时间和数值','fluid_energy_correlation':correlation(),'fluid_range_ratio':(max(r['daily_fluid'] for r in trends)-min(r['daily_fluid'] for r in trends))/production['daily_fluid'] if trends and production['daily_fluid'] else None,'start_day':start,'end_day':end}}


def monitoring(dataset,query):
    wells=well_scope(dataset,{})
    scope=well_scope(dataset,query)
    selected=scope[0] if scope else None
    if query.get('well_id') and selected is None:raise PreviewError('not_found','油井不存在',404)
    start,end=period(dataset,query);ids=[selected['id']] if selected else []
    production=rows_for_wells(dataset,'production_daily',ids,'day',start,end)
    telemetry=rows_for_wells(dataset,'telemetry',ids,'measured_at',start,end,600)
    samples=supplementary(selected,end) if production else {'fluid_level':[],'haulage':[],'status_history':[]}
    rankings=rows_for_wells(dataset,'production_daily',[w['id'] for w in scope],'day',end,end)
    rankings.sort(key=lambda r:r['daily_fluid'],reverse=True)
    return {'wells':wells,'scope_wells':scope,'rankings':rankings,'selected':selected,'production':production,'telemetry':telemetry,'from':start,'to':end,
            'source_kind':'software_fixture','totals':dashboard(dataset,query),**samples}


def register(repository):
    ds=repository.dataset;service=WellsService(ds,None)
    def paged(query,actor):
        filters=dict(query)
        try:
            filters['page']=int(filters.get('page',1));filters['page_size']=int(filters.get('page_size',20))
            if filters['page']<1 or not 1<=filters['page_size']<=100:raise ValueError()
        except (ValueError,TypeError):raise PreviewError('invalid_pagination','分页参数无效')
        result=service.list_wells(filters)
        for row in result['items']:
            row.update(unit_id=row['unit']['id'],unit_name=row['unit']['name'],group_id=row['group']['id'],group_name=row['group']['name'])
        return result
    repository.register('GET',r'/api/preview/legacy/api/dashboard',lambda q,a:dashboard(ds,q))
    repository.register('GET',r'/api/preview/legacy/api/wells',paged)
    repository.register('GET',r'/api/preview/legacy/api/wells/(?P<well_id>[\w-]+)',lambda q,a,well_id:service.detail(well_id))
    repository.register('GET',r'/api/preview/legacy/api/wells/(?P<well_id>[\w-]+)/telemetry',lambda q,a,well_id:service.telemetry(well_id,q.get('from'),q.get('to'),q.get('interval','1h')))
    repository.register('GET',r'/api/preview/views/monitoring',lambda q,a:monitoring(ds,q))
