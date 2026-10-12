"""Read-only projections of independent rehearsal events, never business writes."""
from copy import deepcopy

def history(repository,path):
    state=getattr(repository,'state',None)
    return state.history(path) if state else []

def object_paths(repository,prefix):
    state=getattr(repository,'state',None)
    return sorted({r['object_id'] for r in state.history() if r['object_id'].startswith(prefix+'/')}) if state else []

def project(repository,path,original):
    events=history(repository,path)
    if not events:return original
    row=deepcopy(original or {});row.setdefault('logs',[]);row.setdefault('approvals',[])
    row.update(id=path.rsplit('/',1)[-1],source_kind='preview_rehearsal',preview_only=True)
    for event in events:
        old=row.get('status');v=event['values'];action=event['action'];at=event['created_at']
        for key in ('name','title','well_id','alarm_id','kind','assignee_id','due_at'):
            if key in v:row[key]=v[key]
        row.update(rehearsal_version=event['version'],updated_at=at)
        row.setdefault('created_by',event['actor_id']);row.setdefault('created_at',at)
        if action=='create':row['status']='pending' if '/work-orders/' in path else 'draft'
        if '/work-orders/' in path:
            target=v.get('target')
            if target in {'pending','accepted','processing','review','closed'}:row['status']=target
            if target=='closed':row['closed_by']=event['actor_id'];row['closed_at']=at
            row['logs'].append({'from_status':old,'to_status':row.get('status'),'comment':v.get('comment','创建演练记录'),'actor_id':event['actor_id'],'changed_at':at,'source_kind':'preview_rehearsal'})
        if action=='submit':row.update(status='review' if '/work-orders/' in path else 'pending_approval',submitted_by=event['actor_id'],submitted_at=at)
        if action in {'approve','reject'}:
            row['status']='approved' if action=='approve' else 'rejected'
            row['approvals'].append({'id':event['request_id'],'object_version':event['version'],'decision':row['status'],'comment':v.get('comment'),'decided_at':at,'source_kind':'preview_rehearsal'})
    return row

def paginated(rows,q):
    from .acquisition import page_query
    pq=page_query(q)
    for key in ('well_id','status','assignee_id'):
        if q.get(key):rows=[r for r in rows if str(r.get(key,''))==str(q[key])]
    start=(pq['page']-1)*pq['page_size']
    return {**pq,'total':len(rows),'items':rows[start:start+pq['page_size']]}
