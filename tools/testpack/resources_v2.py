"""Read-only measurement in the attested new inversion namespace. No owner DSN."""
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from backend.inversion.contracts import canonical_bytes
from tools import review_deploy as deploy
from tools.review_deploy_pg import connection_settings
from .lab import checked_handle
from .storage import safe_path


def reader(lab):
    import psycopg
    checked_handle(lab)
    credentials=deploy._credential_file(lab.review_root)
    settings=connection_settings(lab.ports['review']['postgres'],'inversion_reader',credentials['inversion_reader'])
    return lambda:psycopg.connect(**settings)


def assert_fresh(lab):
    from backend.inversion.privileges import probe_role
    from backend.inversion.metadata import probe_metadata
    connect=reader(lab);probe_role(connect,'reader');probe_metadata(connect)
    with connect() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='3s'");conn.execute("SET LOCAL lock_timeout='1s'")
        actual=conn.execute('SELECT current_user,current_database(),inet_server_port()').fetchone()
        if actual!=('review_inversion_reader','review_platform',lab.ports['review']['postgres']):
            raise ValueError('independent reader namespace mismatch')
        if conn.execute('SELECT count(*) FROM inversion.run_requests').fetchone()[0]!=0:
            raise ValueError('v2 validation requires a fresh run namespace')
    return {'role':actual[0],'database':actual[1],'port':actual[2],'fresh_runs':True,'least_privilege_verified':True}


def staging(lab):
    path=safe_path(lab.review_root/'private/inversion-staging')
    return sorted(safe_path(p).name for p in path.iterdir())


def require_run_resources(row):
    if row['reservation_state']!='settled' or row['deadline_seconds']!=180:
        raise ValueError('reservation or original deadline changed')
    expected=row['request_bytes']
    if row['state']=='completed':
        expected+=row['snapshot_bytes']+row['metadata_bytes']+row['chunk_bytes']
        if not 0<=row['attempt_seconds']<=180 or row['terminal_before_deadline'] is not True:
            raise ValueError('completed attempt outside deadline')
        if (max(row['request_bytes']+row['snapshot_bytes'],row['input_transport_bytes'])>64*1024**2 or
            max(row['metadata_bytes']+row['chunk_bytes'],row['result_transport_bytes'])>16*1024**2):
            raise ValueError('retained/transport bytes exceed frozen limit')
    if row['used_bytes']!=expected or not 0<=expected<=80*1024**2:
        raise ValueError('logical byte settlement mismatch')
    if any(type(n) is not int or not 0<n<=4*1024**2 for n in row['attachment_bytes']):
        raise ValueError('attachment size limit exceeded')


def _pins(value):
    found=set()
    def walk(item):
        if isinstance(item,dict):
            for key,child in item.items():
                if key=='attachment_pins':found.update(UUID(v['id']) for v in child)
                elif key=='budget_attachment_pin':found.add(UUID(child['id']))
                else:walk(child)
        elif isinstance(item,list):
            for child in item:walk(child)
    walk(value);return sorted(found,key=str)


def measure(lab):
    connect=reader(lab);output=[]
    with connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        conn.execute("SET LOCAL statement_timeout='3s'");conn.execute("SET LOCAL lock_timeout='1s'")
        rows=conn.execute('''SELECT q.id,q.body,s.state,s.reason_code,r.state,r.used_bytes
            FROM inversion.run_requests q JOIN inversion.run_state s ON s.run_id=q.id
            JOIN inversion.reservations r ON r.run_id=q.id ORDER BY q.created_at''').fetchall()
        if not rows or len(rows)>100:raise ValueError('unexpected isolated run count')
        for run_id,request,state,reason,reservation,used in rows:
            attempts=conn.execute('''SELECT a.id,a.created_at,a.deadline,e.created_at,e.action
                FROM inversion.attempts a JOIN inversion.attempt_events e ON e.attempt_id=a.id
                WHERE a.run_id=%s AND e.action IN ('completed','blocked','failed','cancelled')''',(run_id,)).fetchall()
            if len(attempts)!=1:raise ValueError('expected one attested terminal attempt')
            aid,start,deadline,end,action=attempts[0]
            if action!=state:raise ValueError('terminal evidence mismatch')
            row={'run_id':str(run_id),'attempt_id':str(aid),'state':state,'reason_code':reason,
                'attempt_seconds':(end-start).total_seconds(),'deadline_seconds':180,
                'declared_deadline':deadline.isoformat(),'attempt_started_at':start.isoformat(),
                'terminal_at':end.isoformat(),'terminal_before_deadline':end<=deadline,
                'timing_scope':'claim through durable terminal event, not native CPU time',
                'request_bytes':len(canonical_bytes(request)),'snapshot_bytes':0,'metadata_bytes':0,
                'chunk_bytes':0,'input_transport_bytes':None,'result_transport_bytes':None,
                'reservation_state':reservation,'used_bytes':used,'attachment_bytes':[]}
            results=conn.execute('SELECT id,input_snapshot,body,content_sha256 FROM inversion.results WHERE run_id=%s',(run_id,)).fetchall()
            if (state=='completed')!=(len(results)==1):raise ValueError('unexpected result for terminal state')
            if results:
                result_id,snapshot,body,digest=results[0]
                chunks=conn.execute('SELECT ordinal,octet_length(payload),content_sha256 FROM inversion.result_chunks WHERE result_id=%s ORDER BY ordinal',(result_id,)).fetchall()
                if [v[0] for v in chunks]!=list(range(len(chunks))):raise ValueError('result chunk gap')
                sampling=[v['body']['power']['averaging_window_ns'] for v in snapshot['evidence']
                    if v['kind']=='sampling_config' and v['body'].get('power') is not None]
                windows={int(n) for n in sampling}
                if len(windows)!=1:raise ValueError('ambiguous retained power window')
                transport={'snapshot':snapshot,'cycle':body['cycle'],'power_window_ns':windows.pop()}
                row.update(result_id=str(result_id),result_sha256=digest,
                    snapshot_bytes=len(canonical_bytes(snapshot)),metadata_bytes=len(canonical_bytes(body)),
                    chunk_bytes=sum(v[1] for v in chunks),chunk_inventory=[dict(ordinal=v[0],bytes=v[1],sha256=v[2]) for v in chunks],
                    input_transport_bytes=len(canonical_bytes(transport)))
                row['result_transport_bytes']=row['metadata_bytes']+row['chunk_bytes']+8*(1+len(chunks))
                pins=_pins(snapshot['applicability']['parameter_body'])
                attachments=conn.execute('SELECT attachment_id,payload_bytes,payload_sha256,media_type FROM waveforms.attachment_metadata WHERE attachment_id=ANY(%s)',(pins,)).fetchall()
                if {v[0] for v in attachments}!=set(pins) or not pins:raise ValueError('attachment metadata incomplete')
                row['attachment_bytes']=[v[1] for v in attachments]
                row['attachments']=[dict(id=str(v[0]),bytes=v[1],sha256=v[2],media_type=v[3]) for v in attachments]
            require_run_resources(row);output.append(row)
        capacity=conn.execute('SELECT queued,running,reserved_bytes,used_bytes,total_storage_bytes FROM inversion.capacity WHERE singleton=1').fetchone()
        if capacity[:3]!=(0,0,0) or capacity[3]!=sum(v['used_bytes'] for v in output) or capacity[3]>capacity[4]:
            raise ValueError('capacity did not settle to exact retained byte usage')
    return {'schema':'owp.testpack.v2-resources/1','runs':output,
        'capacity':dict(zip(('queued','running','reserved_bytes','used_bytes','total_storage_bytes'),capacity)),
        'scope':'logical retained inputs/results; excludes database tuple/index/WAL/filesystem allocation',
        'staging':staging(lab),'reader_role':'review_inversion_reader'}
