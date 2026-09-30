"""Bounded metadata reads with revision-checked repeatable-read pagination."""
from dataclasses import fields,is_dataclass
from datetime import datetime,timedelta,timezone
from fractions import Fraction
from hashlib import sha256
import json
import re
from uuid import UUID
from .contracts import CaptureKey
from .errors import WaveformError
from .routes import invalid


def public(value,key=None):
    if is_dataclass(value): return {f.name:public(getattr(value,f.name),f.name) for f in fields(value)}
    if hasattr(value,'items'): return {k:public(v,k) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [public(v) for v in value]
    if isinstance(value,(UUID,Fraction)): return str(value)
    if isinstance(value,datetime): return utc(value)
    if type(value) is int and key in ('version','revision','draft_revision'): return str(value)
    if isinstance(value,(bytes,bytearray,memoryview)): raise TypeError('Binary values require controlled download')
    return value

def utc(value): return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')

def timestamp(value):
    try:
        parsed=datetime.strptime(value,'%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
        if utc(parsed)!=value: raise ValueError()
        return parsed
    except (ValueError,TypeError): raise invalid('Canonical UTC timestamp required') from None

def rows(cur):
    names=[d.name for d in cur.description]
    return [dict(zip(names,r)) for r in cur.fetchall()]

def one(cur,query,params=()):
    from .capacity import row
    cur.execute(query,params); result=row(cur)
    if result is None: raise WaveformError('waveform.not_found','Resource does not exist',404)
    return result


class WaveformQueries:
    def __init__(self,connect): self.connect=connect

    def capture_key(self,id):
        with self.connect() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute("SET LOCAL statement_timeout='3s'"); conn.execute("SET LOCAL lock_timeout='1s'")
            v=one(conn.cursor(),'SELECT gateway_id,device_id,capture_id FROM waveforms.capture_metadata WHERE capture_pk=%s',(id,))
            return CaptureKey(v['gateway_id'],v['device_id'],v['capture_id'])

    def raw(self,id,actor):
        from .capacity import prepare_transaction
        from psycopg.types.json import Jsonb
        with self.connect() as conn:
            cur=conn.cursor(); prepare_transaction(cur)
            meta=one(cur,'SELECT payload_sha256,payload_bytes FROM waveforms.fact_metadata WHERE fact_id=%s',(id,))
            cur.execute('SELECT waveforms.audited_raw(%s,%s)',(id,Jsonb(public(actor))))
            payload=bytes(cur.fetchone()[0]); prepare_transaction(cur); conn.commit()
        return payload,meta

    def export_file(self,export_id,capture_id,actor):
        with self.connect() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute("SET LOCAL statement_timeout='3s'"); conn.execute("SET LOCAL lock_timeout='1s'")
            value=one(conn.cursor(),"SELECT f.descriptor FROM waveforms.dataset_exports e JOIN waveforms.dataset_files f USING(version_id) WHERE e.export_id=%s AND f.capture_pk=%s AND e.actor->>'domain'=%s AND e.actor->>'user_id'=%s",(export_id,capture_id,actor.domain,actor.user_id))
            return value['descriptor']

    def dataset_selection(self,id,version_id=None):
        with self.connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            conn.execute("SET LOCAL statement_timeout='3s'"); conn.execute("SET LOCAL lock_timeout='1s'")
            return one(conn.cursor(),'SELECT r.revision,v.version_id FROM waveforms.dataset_revisions r '
                       'LEFT JOIN waveforms.dataset_versions v USING(dataset_id) WHERE r.dataset_id=%s'+
                       (' AND v.version_id=%s' if version_id else '')+' ORDER BY r.revision DESC LIMIT 1',
                       (id,version_id) if version_id else (id,))

    def fetch(self,resource:str,filters:dict)->dict:
        import psycopg
        try:
            with self.connect() as conn:
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                conn.execute("SET LOCAL statement_timeout='3s'"); conn.execute("SET LOCAL lock_timeout='1s'")
                cur=conn.cursor()
                if resource=='health':
                    cur.execute('SELECT * FROM waveforms.capacity_metadata ORDER BY bucket LIMIT 66'); capacity=rows(cur)
                    cur.execute('SELECT * FROM waveforms.worker_metadata ORDER BY component,worker_id LIMIT 257'); workers=rows(cur)
                    if len(workers)>256: raise WaveformError('waveform.health_limit','Health metadata exceeds bound')
                    return public(dict(status='ready',time_basis='current',capacity=capacity,workers=workers))
                if resource=='capture' and filters.get('view','summary')=='summary': return self._capture(cur,filters)
                return public(self._list(cur,resource,filters))
        except psycopg.Error:
            raise WaveformError('waveform.query_unavailable','Waveform metadata query unavailable') from None

    def _capture(self,cur,f):
        from .capacity import row
        if set(f)-{'id','view'}: raise invalid()
        value=one(cur,'SELECT * FROM waveforms.capture_metadata WHERE capture_pk=%s',(f['id'],))
        cur.execute('SELECT stream_id,chunk_count FROM waveforms.streams WHERE capture_pk=%s ORDER BY stream_id LIMIT 9',(f['id'],)); streams=cur.fetchall()
        if len(streams)>8: raise WaveformError('waveform.metadata_limit','Stream count exceeds bound')
        cur.execute('SELECT stream_id,chunk_index FROM waveforms.chunk_slots WHERE capture_pk=%s LIMIT 513',(f['id'],)); present=set(cur.fetchall())
        if len(present)>512: raise WaveformError('waveform.metadata_limit','Slot count exceeds bound')
        value['missing_chunks']={s:[i for i in range(n) if (s,i) not in present] for s,n in streams}
        cur.execute('SELECT status,reason_code,result,completed_at FROM waveforms.completion WHERE capture_pk=%s',(f['id'],)); value['completion']=row(cur)
        return public(value)

    def _list(self,cur,resource,f):
        page,size=f.get('page',1),f.get('page_size',50)
        supplied=f.get('snapshot_revision'); cutoff=f.get('received_before')
        if bool(supplied)!=bool(cutoff) or page>1 and not supplied: raise invalid('Pagination requires both snapshot tokens')
        if supplied is not None:
            grammar=r'w([1-9][0-9]{0,18})\.r(0|[1-9][0-9]{0,18})'+(r'\.a(0|[1-9][0-9]{0,18})\.b(0|[1-9][0-9]{0,18})' if resource=='audit' else '')
            grammar+=r'\.q[0-9a-f]{64}'
            match=re.fullmatch(grammar,supplied)
            if not match or any(int(n)>9223372036854775807 for n in match.groups()): raise invalid('Malformed snapshot revision')
        cur.execute('SELECT w.version,r.revision,CURRENT_TIMESTAMP FROM waveforms.state_head w CROSS JOIN waveforms.registry_head r WHERE w.singleton=1 AND r.singleton=1')
        w,r,now=cur.fetchone(); before=timestamp(cutoff) if cutoff else now
        after=timestamp(f['received_after']) if 'received_after' in f else None
        if resource=='audit':
            if page>1 and after is None: raise invalid('Audit pages require fixed received_after')
            after=after or before-timedelta(days=31)
        if after is not None and not timedelta(0)<before-after<=timedelta(days=31): raise invalid('Time window exceeds 31 days')
        token=f'w{w}.r{r}'; params=[]
        base,timecol,order,extra=self._source(cur,resource,f,before)
        # All caller values become parameters; relation/column text is fixed here.
        clauses=[f'{timecol}<%s']; params.append(before)
        if after is not None: clauses.append(f'{timecol}>=%s'); params.append(after)
        clauses.extend(extra[0]); params.extend(extra[1])
        where=' AND '.join(clauses)
        if resource=='audit':
            cur.execute(f'SELECT family,count(*) FROM ({base}) q WHERE {where} GROUP BY family',params)
            counts=dict(cur.fetchall()); token+=f".a{counts.get('raw',0)}.b{counts.get('business',0)}"
        scope=dict(resource=resource,filters={k:v for k,v in f.items() if k not in ('page','page_size','snapshot_revision','received_before','received_after')},
                   page_size=size,received_before=utc(before),received_after=utc(after) if after else None)
        token+='.q'+sha256(json.dumps(public(scope),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()
        if supplied is not None and supplied!=token: raise WaveformError('waveform.refresh_required','List changed; refresh from first page',409)
        cur.execute(f'SELECT * FROM ({base}) q WHERE {where} ORDER BY {order} OFFSET %s LIMIT %s',(*params,(page-1)*size,size+1))
        result=rows(cur); more=len(result)>size
        if resource=='dataset':
            result=[v['descriptor'] if 'descriptor' in v else v['member'] if 'member' in v else
                    {k:v[k] for k in ('kind','reference_id','quality_run_id','split')} for v in result]
        return dict(items=result[:size],page=page,page_size=size,has_more=more,snapshot_revision=token,received_before=utc(before),**({'received_after':utc(after)} if after else {}))

    def _source(self,cur,resource,f,before):
        conditions=[]; values=[]
        def eq(key,column=None,allowed=None):
            if key not in f: return
            value=f[key]
            if allowed and value not in allowed: raise invalid('Unsupported filter value')
            if isinstance(value,str) and (len(value)>128 or not value or any(ord(c)<32 for c in value)): raise invalid()
            conditions.append((column or key)+'=%s'); values.append(value)
        purposes=('raw_archive','software_pair_test','field_pair_input')
        if resource=='captures':
            # Recompute the derived clock field using the fixed cutoff, not view.stalled.
            base="SELECT c.capture_pk,c.gateway_id,c.device_id,c.capture_id,c.manifest_fact_id,c.manifest_sha256,c.state,c.first_seen,c.last_valid_at,c.version,c.has_conflicts,c.used_raw_bytes,(c.state='collecting' AND c.last_valid_at<TIMESTAMPTZ '"+utc(before)+"'-INTERVAL '24 hours') AS stalled FROM waveforms.capture_metadata c"
            eq('gateway_id'); eq('device_id'); eq('state',allowed=('collecting','complete','invalid'))
            if 'stalled' in f:
                if f['stalled'] not in ('true','false'): raise invalid()
                conditions.append('stalled=%s'); values.append(f['stalled']=='true')
            if 'well_id' in f:
                conditions.append('EXISTS(SELECT 1 FROM waveforms.registry_devices d JOIN waveforms.registry_well_bindings b ON b.device_id=d.id WHERE d.gateway_id=q.gateway_id AND d.device_id=q.device_id AND b.well_id=%s AND b.valid_from<=%s AND (b.valid_to IS NULL OR b.valid_to>%s))'); values.extend((f['well_id'],before,before))
            return base,'first_seen','first_seen,capture_pk',(conditions,values)
        if resource=='capture':
            one(cur,'SELECT capture_pk FROM waveforms.capture_metadata WHERE capture_pk=%s',(f['id'],))
            key=self._key_condition(f['id']); view=f.get('view')
            if view=='facts':
                if set(f)&{'status','reason_code','transport','source_ref'}: raise invalid()
                base='SELECT DISTINCT a.* FROM waveforms.fact_metadata a JOIN waveforms.outcome_metadata m USING(fact_id) JOIN waveforms.capture_metadata c ON '+key
                base+=' WHERE c.capture_pk=\''+str(f['id'])+"'"; timecol='received_at'; order='received_at,fact_id'
            elif view=='outcomes':
                if set(f)&{'transport','source_ref'}: raise invalid()
                base='SELECT m.*,a.received_at FROM waveforms.outcome_metadata m JOIN waveforms.fact_metadata a USING(fact_id) JOIN waveforms.capture_metadata c ON '+key+" WHERE c.capture_pk='"+str(f['id'])+"'"
                eq('status',allowed=('stored','quarantined')); eq('reason_code'); timecol='received_at'; order='received_at,outcome_id'
            elif view=='provenance':
                if set(f)&{'status','reason_code'}: raise invalid()
                base='SELECT p.*,a.received_at AS fact_received_at FROM waveforms.provenance_metadata p JOIN waveforms.fact_metadata a USING(fact_id) JOIN waveforms.capture_metadata c ON c.gateway_id=p.gateway_id AND c.device_id=p.device_id AND c.capture_id=p.capture_id WHERE c.capture_pk=\''+str(f['id'])+"'"
                eq('transport',allowed=('mqtt','file')); eq('source_ref'); timecol='fact_received_at'; order='fact_received_at,observation_id'
            else: raise invalid()
            return base,timecol,order,(conditions,values)
        if resource=='evidence':
            base='SELECT e.evidence_id,e.kind,e.content_sha256,e.creator,e.recorded_at,v.version,v.state FROM waveforms.evidence_versions e JOIN LATERAL(SELECT version,state FROM waveforms.evidence_events WHERE evidence_id=e.evidence_id ORDER BY version DESC LIMIT 1)v ON true'
            eq('kind',allowed=('sampling_config','calibration','sync','origin','profile')); eq('state',allowed=('draft','approved','rejected','revoked')); idcol='evidence_id'
        elif resource=='evaluations':
            base='SELECT quality_run_id,capture_pk,manifest_sha256,validator_version,profile_id,profile_sha256,purpose,status,creator,recorded_at FROM waveforms.quality_runs'
            eq('id','capture_pk'); eq('status',allowed=('eligible','ineligible','pending')); eq('purpose',allowed=purposes); idcol='quality_run_id'
        elif resource=='pairs':
            base="SELECT p.pair_version_id,p.left_quality_run_id,p.right_quality_run_id,p.method_version,p.content_sha256,p.creator,p.recorded_at,coalesce(r.version,1) AS version,coalesce(r.decision,'proposed') AS state FROM waveforms.pair_versions p LEFT JOIN LATERAL(SELECT version,decision FROM waveforms.pair_reviews WHERE pair_version_id=p.pair_version_id ORDER BY version DESC LIMIT 1)r ON true"
            eq('state',allowed=('proposed','approve','reject','revoke')); idcol='pair_version_id'
        elif resource=='datasets':
            base="SELECT d.dataset_id,d.creator,d.recorded_at,r.revision,r.state,r.body->>'title' AS title,r.body->>'purpose' AS purpose,jsonb_array_length(r.body->'members') AS member_count,v.version_id FROM waveforms.dataset_drafts d JOIN LATERAL(SELECT revision,state,body FROM waveforms.dataset_revisions WHERE dataset_id=d.dataset_id ORDER BY revision DESC LIMIT 1)r ON true LEFT JOIN waveforms.dataset_versions v USING(dataset_id)"
            eq('state',allowed=('draft','submitted','frozen')); eq('purpose',allowed=purposes); idcol='dataset_id'
        elif resource=='audit':
            base="SELECT 'raw'::text AS family,audit_id,intake_gateway_id AS accounting_bucket,fact_id AS subject_id,'fact'::text AS subject_kind,actor,action,details,recorded_at FROM waveforms.audit_metadata UNION ALL SELECT 'business',audit_id,accounting_bucket,subject_id,'business',actor,action,details,recorded_at FROM waveforms.business_audit"
            eq('family',allowed=('raw','business')); eq('action'); eq('actor_domain',"actor->>'domain'"); eq('actor_id',"actor->>'user_id'"); eq('subject_id'); idcol='family,audit_id'
        elif resource=='dataset': return self._members(cur,f)
        else: raise invalid()
        return base,'recorded_at','recorded_at,'+idcol,(conditions,values)

    @staticmethod
    def _key_condition(id): return 'c.gateway_id=m.gateway_id AND c.device_id=m.device_id AND c.capture_id=m.capture_id'

    def _members(self,cur,f):
        if f.get('page_size',50)>100 or f.get('view') not in ('members','files') or 'revision' in f and 'version_id' in f: raise invalid()
        id=f['id']; version=f.get('version_id')
        if version:
            one(cur,'SELECT version_id FROM waveforms.dataset_versions WHERE dataset_id=%s AND version_id=%s',(id,version))
            if f['view']=='members': base="SELECT m.ordinal,m.kind,m.reference_id,m.quality_run_id,m.split,v.recorded_at FROM waveforms.dataset_members m JOIN waveforms.dataset_versions v USING(version_id) WHERE version_id='"+str(version)+"'"
            else: base="SELECT f.ordinal,f.descriptor,v.recorded_at FROM waveforms.dataset_files f JOIN waveforms.dataset_versions v USING(version_id) WHERE version_id='"+str(version)+"'"
        else:
            if f['view']=='files': raise invalid('Files require explicit version_id')
            revision=one(cur,'SELECT revision FROM waveforms.dataset_revisions WHERE dataset_id=%s'+(' AND revision=%s' if 'revision' in f else '')+' ORDER BY revision DESC LIMIT 1',(id,f['revision']) if 'revision' in f else (id,))['revision']
            f['revision']=revision
            base="SELECT x.ordinal,x.member,r.recorded_at FROM waveforms.dataset_revisions r CROSS JOIN LATERAL jsonb_array_elements(r.body->'members') WITH ORDINALITY x(member,ordinal) WHERE dataset_id='"+str(id)+"' AND revision="+str(revision)
        return base,'recorded_at','ordinal',([],[])
