"""Immutable result decoding, independent review and current applicability.

authorize(actor, capability) MUST freshly resolve the caller's bound session and
return AuthoritySnapshot. A cached HTTP-entry grant or a None-return callback is
not this contract. Current auth is the decision point across the two stores.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from backend.waveforms.commands import Actor
from .contracts import AuthoritySnapshot, InversionCommand, canonical_bytes, freeze
from .input_contracts import InputSnapshot
from .result_codec import PreparedResult, MAX_RESULT
from .history import recheck_with_cursor
from .parameters import actor_key, expected
from .runs import authority_body
from .transactions import InversionTransactions, MutationRequest, LockPlan, Change, InversionError, lock

GUARDS=LockPlan(waveform=True,registry=True)

def refuse(code,status=409):
    raise InversionError(code,code,status=status)

def configure(cur):
    cur.execute("SET LOCAL statement_timeout='3s'")
    cur.execute("SET LOCAL lock_timeout='1s'")

def field_validation(source,facts):
    independent=facts.get('independent_comparison') is True
    return dict(state='single_field_comparison' if source=='field_measurement' and independent else 'not_field_validated',
        certified=False,independent_comparison=independent,
        independence_reasons=list(facts.get('independence_reasons',())),
        external_evidence_admission_required=True)

@dataclass(frozen=True)
class StoredResult:
    id: UUID
    run_id: UUID
    execution: str
    author: object
    request_sha256: str
    prepared: PreparedResult
    content_sha256: str
    body: object

@dataclass(frozen=True)
class ResultView:
    id: UUID
    run_id: UUID
    content_sha256: str
    request_sha256: str
    snapshot_sha256: str
    execution: str
    source: str
    review: object
    current_applicability: object
    field_validation: object
    body: object
    chunks: tuple[bytes,...]
    input_snapshot_metadata: object = None

class ResultService:
    def __init__(self,connect,*,read_connect,authorize):
        if not all(callable(v) for v in (connect,read_connect,authorize)):
            raise ValueError('Explicit connections and fresh session authority callback required')
        self.connect,self.read_connect,self.authorize=connect,read_connect,authorize
        self.transactions=InversionTransactions(connect)

    def _auth(self,actor,capabilities):
        if type(actor) is not Actor: refuse('actor_invalid',403)
        snapshots=[]
        for capability in sorted(capabilities):
            value=self.authorize(actor,capability)
            if (type(value) is not AuthoritySnapshot or capability not in value.permissions or
                (value.ref.domain,str(value.ref.user_id))!=actor_key(actor)):
                refuse('authority_changed',403)
            if snapshots and value.ref!=snapshots[0].ref: refuse('authority_changed',403)
            snapshots.append(value)
        return [authority_body(v) for v in snapshots]

    def _load(self,id):
        if type(id) is not UUID: refuse('result_identity_invalid')
        with self.read_connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            cur=conn.cursor(); configure(cur)
            cur.execute('SELECT r.run_id,r.input_snapshot,r.body,r.content_sha256,s.state,q.actor,q.input_sha256 FROM inversion.results r JOIN inversion.run_requests q ON q.id=r.run_id JOIN inversion.run_state s ON s.run_id=r.run_id WHERE r.id=%s',(id,))
            row=cur.fetchone()
            if row is None: refuse('result_missing',404)
            metadata=canonical_bytes(row[2]); raw=canonical_bytes(row[1])
            try:
                snapshot=InputSnapshot(row[1],raw,row[2]['input_sha256'])
                # Bound transfer before the client cursor materializes any bytea.
                # Repeatable read keeps this budget and subsequent rows coherent.
                cur.execute('SELECT count(*),COALESCE(sum(octet_length(payload)),0) FROM inversion.result_chunks WHERE result_id=%s',(id,))
                count,size=cur.fetchone()
                if count>4*32768 or len(metadata)+size>MAX_RESULT: refuse('result_size_limit')
                cur.execute('SELECT ordinal,payload,content_sha256 FROM inversion.result_chunks WHERE result_id=%s ORDER BY ordinal',(id,))
                chunks=[]; total=len(metadata)
                for ordinal,payload,digest in cur:
                    payload=bytes(payload); total+=len(payload)
                    if ordinal!=len(chunks) or sha256(payload).hexdigest()!=digest: refuse('result_chunk_corrupt')
                    if total>MAX_RESULT: refuse('result_size_limit')
                    chunks.append(payload)
                prepared=PreparedResult(snapshot,metadata,tuple(chunks))
                if prepared.content_sha256!=row[3]: refuse('result_digest_corrupt')
                if row[4]!='completed' or prepared.status!='applicable': refuse('result_lifecycle_invalid')
            except (ValueError,KeyError,TypeError) as error:
                raise InversionError('result_corrupt') from error
            return StoredResult(id,row[0],row[4],freeze(row[5]),row[6],prepared,row[3],freeze(row[2]))

    @staticmethod
    def _review(cur,id):
        cur.execute('SELECT version,action,actor,reason,body FROM inversion.result_reviews WHERE subject_id=%s ORDER BY version',(id,))
        history=[dict(zip(('version','action','actor','reason','body'),row)) for row in cur]
        state={'approve':'approved','reject':'rejected','revoke':'revoked'}[history[-1]['action']] if history else 'unreviewed'
        return dict(state=state,version=history[-1]['version'] if history else 0,history=history)

    def _view(self,cur,stored):
        review=self._review(cur,stored.id)
        reasons=list(recheck_with_cursor(cur,stored.prepared.snapshot))
        if review['state'] in ('revoked','rejected'): reasons.append('result_'+review['state'])
        body=stored.body; facts=stored.prepared.snapshot.body['applicability']
        field=field_validation(body['source_kind'],facts)
        return ResultView(stored.id,stored.run_id,stored.content_sha256,stored.request_sha256,
            stored.prepared.snapshot.content_sha256,stored.execution,body['source_kind'],freeze(review),
            freeze(dict(state='restricted' if reasons else 'applicable',reasons=sorted(set(reasons)))),
            freeze(field),body,stored.prepared.chunks)

    def get(self,id,*,actor,include_input_metadata=False):
        if type(include_input_metadata) is not bool: refuse('input_metadata_invalid',400)
        capabilities={'inversion.read','waveform.read'} if include_input_metadata else {'inversion.read'}
        self._auth(actor,capabilities); stored=self._load(id)
        with self.connect() as conn:
            cur=conn.cursor(); configure(cur); lock(cur,GUARDS)
            self._auth(actor,capabilities)
            view=self._view(cur,stored)
            if include_input_metadata:
                from dataclasses import replace
                from .input_metadata import project_input_metadata
                metadata=project_input_metadata(stored.prepared.snapshot.body,result_id=str(stored.id),
                    content_sha256=stored.content_sha256,snapshot_sha256=stored.prepared.snapshot.content_sha256,
                    request_sha256=stored.request_sha256)
                view=replace(view,input_snapshot_metadata=freeze(metadata))
            return view

    def review(self,id,decision,command):
        if decision not in ('approve','reject'): refuse('review_decision_invalid')
        return self._transition(id,decision,command)

    def revoke(self,id,command): return self._transition(id,'revoke',command)

    def _transition(self,id,action,command):
        if type(command) is not InversionCommand: refuse('command_invalid')
        authority=self._auth(command.actor,{'inversion.read','inversion.review'})
        stored=self._load(id)
        def apply(cur,cmd):
            view=self._view(cur,stored); expected(cmd,view.review['version'])
            state=view.review['state']
            if action=='revoke':
                if state!='approved': refuse('review_transition')
            else:
                if state!='unreviewed': refuse('review_transition')
                if actor_key(cmd.actor) in (actor_key(stored.author),actor_key(stored.prepared.snapshot.body['actor'])):
                    refuse('self_review')
            if action=='approve' and view.current_applicability['state']!='applicable': refuse('result_restricted')
            version=view.review['version']+1
            attestation=dict(result_sha256=view.content_sha256,authority=authority,
                             meaning='Independent result review; not field accuracy certification')
            cur.execute('INSERT INTO inversion.result_reviews(id,subject_id,version,action,actor,reason,body) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                (uuid4(),id,version,action,Jsonb(asdict(cmd.actor)),cmd.reason,Jsonb(attestation)))
            if action=='revoke':
                cur.execute("INSERT INTO inversion.restrictions(id,subject_id,subject_kind,version,action,body,actor) VALUES(%s,%s,'result',%s,'revoke',%s,%s)",
                    (uuid4(),id,version,Jsonb(dict(reason=cmd.reason,result_sha256=view.content_sha256)),Jsonb(asdict(cmd.actor))))
            return Change('result.'+action,id,version,view.content_sha256,dict(action=action,authority=authority))
        return self.transactions.run(command,apply,request=MutationRequest('result.'+action,str(id),canonical_bytes({})),dependencies=GUARDS)
