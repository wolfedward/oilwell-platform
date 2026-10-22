"""Durable requests and atomic, generation-fenced terminal publication.

input_sha256 on a request identifies its pinned request input identities. It is
NOT the subsequently prepared InputSnapshot hash, which lives in result metadata.
Only retained canonical request/snapshot/result bytes count toward run capacity.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from backend.waveforms.commands import Actor
from backend.waveforms.pair_contracts import public
from . import capacity
from .contracts import AuthorityRef, AuthoritySnapshot, InversionCommand, VersionPrecondition, canonical_bytes
from .input_contracts import RunRequest, parse_run_request
from .history import recheck_with_cursor
from .transactions import InversionTransactions, MutationRequest, LockPlan, Change, InversionError, lock

CAPABILITIES=frozenset(('inversion.run','waveform.read','waveform.raw.read','waveform.export'))
TERMINAL=frozenset(('completed','blocked','failed','cancelled'))

def authority_body(value):
    return dict(ref=asdict(value.ref),permissions=sorted(value.permissions),
                checked_at=value.checked_at.isoformat(),user_version=value.user_version)

@dataclass(frozen=True)
class RunRecord:
    id: UUID
    request: RunRequest
    actor: Actor
    authority: AuthorityRef
    predecessor_id: UUID | None
    version: int
    state: str
    generation: int
    owner_id: UUID | None
    deadline: datetime | None
    reason_code: str | None
    request_bytes: int

@dataclass(frozen=True)
class Attempt:
    id: UUID
    run_id: UUID
    generation: int
    owner_id: UUID
    deadline: datetime

@dataclass(frozen=True)
class ResultRecord:
    id: UUID
    run_id: UUID
    content_sha256: str


class RunService:
    def __init__(self,connect,*,read_connect,limits,authorize,refresh_authority):
        if not all(callable(v) for v in (connect,read_connect,authorize,refresh_authority)):
            raise ValueError('Configured connections and current authority providers required')
        self.connect,self.read_connect,self.limits=connect,read_connect,limits
        self.authorize,self.refresh_authority=authorize,refresh_authority
        self.transactions=InversionTransactions(connect)

    def _authorize(self,actor):
        for permission in CAPABILITIES: self.authorize(actor,permission)

    def _authority(self,ref,actor):
        value=self.refresh_authority(ref)
        self.validate_authority(value,ref,actor)
        return value

    @staticmethod
    def validate_authority(value,ref,actor):
        if (type(value) is not AuthoritySnapshot or value.ref!=ref or not CAPABILITIES<=value.permissions
            or (ref.domain,str(ref.user_id))!=(actor.domain,actor.user_id)):
            raise InversionError('authority_changed',status=403)

    @staticmethod
    def _read(cur,identifier,*,for_update=False):
        cur.execute('SELECT r.id,r.body,r.actor,r.predecessor_id,s.version,s.state,s.generation,s.owner_id,s.deadline,s.reason_code FROM inversion.run_requests r JOIN inversion.run_state s ON s.run_id=r.id WHERE r.id=%s'+(' FOR UPDATE OF s' if for_update else ''),(identifier,))
        row=cur.fetchone()
        if row is None: raise InversionError('run_missing',status=404)
        body=row[1]
        return RunRecord(row[0],parse_run_request(canonical_bytes(body['request'])),Actor(**row[2]),
            AuthorityRef(**body['authority']),row[3],*row[4:],len(canonical_bytes(body)))

    def get(self,identifier):
        with self.read_connect() as conn:
            conn.execute("SET LOCAL statement_timeout='3s'"); conn.execute("SET LOCAL lock_timeout='1s'")
            return self._read(conn.cursor(),identifier)

    def _reservation(self,run):
        return capacity.Reservation(run.id,self.limits.input_bytes,self.limits.result_bytes,1)

    @staticmethod
    def _event(cur,run,state,reason,*,generation=None,owner=None,deadline=None,details=None):
        version=run.version+1
        cur.execute('UPDATE inversion.run_state SET version=%s,state=%s,reason_code=%s,generation=%s,owner_id=%s,deadline=%s,heartbeat_at=clock_timestamp() WHERE run_id=%s',
                    (version,state,reason,run.generation if generation is None else generation,
                     run.owner_id if owner is None else owner,run.deadline if deadline is None else deadline,run.id))
        cur.execute('INSERT INTO inversion.run_events(id,run_id,version,state,body) VALUES(%s,%s,%s,%s,%s)',
                    (uuid4(),run.id,version,state,Jsonb(dict(reason_code=reason,details=details or {}))))
        return version

    @staticmethod
    def _pins(cur,request):
        cur.execute('SELECT content_sha256 FROM waveforms.dataset_versions WHERE version_id=%s',(request.dataset_version_id,))
        if cur.fetchone()!=(request.dataset_sha256,): raise InversionError('dataset_pin_changed',status=409)
        cur.execute('SELECT kind,reference_id,snapshot FROM waveforms.dataset_members WHERE version_id=%s AND ordinal=%s',
                    (request.dataset_version_id,request.member_ordinal))
        member=cur.fetchone()
        if (member is None or member[:2]!=('pair',request.pair_id)
            or member[2]['pair']['content_sha256']!=request.pair_sha256):
            raise InversionError('member_pin_changed',status=409)
        cur.execute('SELECT version,decision FROM waveforms.pair_reviews WHERE pair_version_id=%s ORDER BY version DESC LIMIT 1',(request.pair_id,))
        if cur.fetchone()!=(request.pair_review_version,'approve'): raise InversionError('pair_pin_changed',status=409)
        cur.execute('SELECT assembly_id,content_sha256 FROM inversion.parameter_versions WHERE id=%s',(request.parameter_id,))
        if cur.fetchone()!=(request.assembly_id,request.parameter_sha256): raise InversionError('parameter_pin_changed',status=409)
        cur.execute('SELECT version,action FROM inversion.reviews WHERE subject_id=%s ORDER BY version DESC LIMIT 1',(request.parameter_id,))
        if cur.fetchone()!=(request.parameter_review_version,'approve'): raise InversionError('parameter_pin_changed',status=409)
        cur.execute('SELECT content_sha256 FROM inversion.model_versions WHERE id=%s',(request.model_id,))
        if cur.fetchone()!=(request.model_sha256,): raise InversionError('model_pin_changed',status=409)
        cur.execute('SELECT version,action FROM inversion.model_reviews WHERE subject_id=%s ORDER BY version DESC LIMIT 1',(request.model_id,))
        if cur.fetchone()!=(request.model_review_version,'approve'): raise InversionError('model_pin_changed',status=409)
        cur.execute('SELECT version,body FROM inversion.selections WHERE assembly_id=%s ORDER BY version DESC LIMIT 1',(request.assembly_id,))
        selection=cur.fetchone()
        if selection is None or selection[0]!=request.selection_version: raise InversionError('selection_changed',status=409)
        for key in ('parameter_id','parameter_sha256','parameter_review_version','model_id','model_sha256','model_review_version'):
            value=getattr(request,key); value=str(value) if type(value) is UUID else value
            if selection[1].get(key)!=value: raise InversionError('selection_changed',status=409)

    def create(self,body:RunRequest,command:InversionCommand,authority:AuthorityRef)->RunRecord:
        return self._create(body,command,authority,None)

    def _create(self,body,command,authority,predecessor):
        if type(body) is not RunRequest or type(authority) is not AuthorityRef: raise ValueError('Validated request and authority required')
        self._authorize(command.actor); self._authority(authority,command.actor)
        envelope=dict(request=public(body),authority=asdict(authority))
        encoded=canonical_bytes(envelope)
        if len(encoded)>self.limits.policy_bytes: raise InversionError('request_size_limit',status=409)
        action='runs.create' if predecessor is None else 'runs.retry'
        def apply(cur,cmd):
            if predecessor is None:
                if cmd.expected.kind!='absent': raise InversionError('version_conflict',status=409)
            else:
                previous=self._read(cur,predecessor,for_update=True)
                if cmd.expected!=VersionPrecondition('match',previous.version) or previous.state not in TERMINAL:
                    raise InversionError('version_conflict',status=409)
            self._pins(cur,body)
            identifier=uuid4(); content=sha256(encoded).hexdigest()
            capacity.reserve(cur,run_id=identifier,limits=self.limits)
            cur.execute('INSERT INTO inversion.run_requests(id,pair_id,parameter_id,model_id,predecessor_id,actor,body,content_sha256,input_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (identifier,body.pair_id,body.parameter_id,body.model_id,predecessor,Jsonb(asdict(cmd.actor)),Jsonb(envelope),content,sha256(canonical_bytes(public(body))).hexdigest()))
            cur.execute("INSERT INTO inversion.run_state(run_id,state) VALUES(%s,'queued')",(identifier,))
            cur.execute("INSERT INTO inversion.run_events(id,run_id,version,state,body) VALUES(%s,%s,1,'queued',%s)",(uuid4(),identifier,Jsonb({})))
            return Change(action,identifier,1,content,dict(id=str(identifier)))
        # A refreshed login session must not change the actor-scoped request key.
        # The original authority reference remains immutable on receipt replay.
        receipt=self.transactions.run(command,apply,request=MutationRequest(action,str(predecessor or 'new'),canonical_bytes(public(body))),dependencies=LockPlan(waveform=True,registry=True,capacity=True))
        return self.get(receipt.subject_id)

    def retry(self,identifier,command,authority):
        self._authorize(command.actor)
        previous=self.get(identifier)
        return self._create(previous.request,command,authority,identifier)

    def claim(self,owner:UUID)->Attempt|None:
        if type(owner) is not UUID: raise ValueError('Worker owner identity required')
        # Identity store refresh happens outside all business locks. Recheck
        # exact chosen queued row after locking; never substitute a stale actor.
        with self.read_connect() as conn:
            conn.execute("SET LOCAL statement_timeout='3s'")
            row=conn.execute("SELECT run_id FROM inversion.run_state WHERE state='queued' ORDER BY run_id LIMIT 1").fetchone()
        if row is None: return None
        selected=self.get(row[0]); denied=None
        try: auth=self._authority(selected.authority,selected.actor)
        except InversionError as error: denied=error.code
        with self.connect() as conn:
            cur=conn.cursor(); self._lock(cur)
            run=self._read(cur,selected.id,for_update=True)
            if run.state!='queued': return None
            if denied:
                self._terminal(cur,run,'blocked',denied)
                return None
            if capacity.usage(cur)['running']: return None
            capacity.claim(cur,self._reservation(run))
            deadline=datetime.now(timezone.utc)+timedelta(seconds=self.limits.deadline_seconds)
            attempt=Attempt(uuid4(),run.id,run.generation+1,owner,deadline)
            self._event(cur,run,'running',None,generation=attempt.generation,owner=owner,deadline=deadline)
            cur.execute('INSERT INTO inversion.attempts(id,run_id,generation,owner_id,deadline,body) VALUES(%s,%s,%s,%s,%s,%s)',
                (attempt.id,run.id,attempt.generation,owner,deadline,Jsonb(authority_body(auth))))
            self._audit(cur,run,'runs.claim',dict(generation=attempt.generation))
        return attempt

    @staticmethod
    def _lock(cur,*,publication=False):
        cur.execute("SET LOCAL statement_timeout='3s'"); cur.execute("SET LOCAL lock_timeout='1s'")
        lock(cur,LockPlan(waveform=publication,registry=publication,capacity=True))

    @staticmethod
    def _audit(cur,run,action,body):
        cur.execute('INSERT INTO inversion.audit(id,actor,action,subject_id,reason,request_key,response) VALUES(%s,%s,%s,%s,%s,%s,%s)',
            (uuid4(),Jsonb(asdict(run.actor)),action,run.id,'Durable worker state transition',uuid4(),Jsonb(body)))
        cur.execute('UPDATE inversion.state_head SET version=version+1 WHERE singleton=1')

    def _terminal(self,cur,run,state,reason,*,details=None,used_bytes=None):
        self._event(cur,run,state,reason,details=details)
        if run.generation:
            cur.execute('INSERT INTO inversion.attempt_events(id,attempt_id,action,body) SELECT %s,id,%s,%s FROM inversion.attempts WHERE run_id=%s AND generation=%s',
                (uuid4(),state,Jsonb(dict(reason_code=reason,details=details or {})),run.id,run.generation))
        capacity.settle(cur,self._reservation(run),run.request_bytes if used_bytes is None else used_bytes)
        self._audit(cur,run,'runs.'+state,dict(reason_code=reason))

    @staticmethod
    def _fence(run,attempt):
        if (run.generation,run.owner_id)!=(attempt.generation,attempt.owner_id):
            raise InversionError('attempt_fenced',status=409)

    def finish(self,attempt,state,reason,*,details=None):
        """Internal supervised exit reconciliation, called only after owned reap."""
        if state not in ('failed','blocked','cancelled'): raise ValueError('Worker terminal state required')
        if len(canonical_bytes(details or {}))>65536: raise InversionError('diagnostics_size_limit')
        with self.connect() as conn:
            cur=conn.cursor(); self._lock(cur)
            run=self._read(cur,attempt.run_id,for_update=True); self._fence(run,attempt)
            if run.state not in TERMINAL:
                if run.reason_code=='cancel_requested': state,reason='cancelled','user_cancelled'
                elif state=='cancelled': raise InversionError('cancel_not_requested',status=409)
                self._terminal(cur,run,state,reason,details=details)
        return self.get(attempt.run_id)

    def heartbeat(self,attempt):
        with self.connect() as conn:
            cur=conn.cursor(); self._lock(cur)
            run=self._read(cur,attempt.run_id,for_update=True); self._fence(run,attempt)
            if run.state!='running': raise InversionError('run_terminal',status=409)
            self._event(cur,run,'running',run.reason_code)

    def cancel(self,identifier,command):
        # Cancellation consumes no waveform bytes. A current run-capable owner
        # can stop work even after raw-data access was removed.
        self.authorize(command.actor,'inversion.run')
        def apply(cur,cmd):
            run=self._read(cur,identifier,for_update=True)
            if cmd.expected!=VersionPrecondition('match',run.version): raise InversionError('version_conflict',status=409)
            if run.state=='queued': self._terminal(cur,run,'cancelled','user_cancelled')
            elif run.state=='running': self._event(cur,run,'running','cancel_requested')
            return Change('runs.cancel',run.id,run.version+(run.state not in TERMINAL),sha256(canonical_bytes(public(run.request))).hexdigest(),dict(id=str(run.id)))
        self.transactions.run(command,apply,request=MutationRequest('runs.cancel',str(identifier),b'{}'),dependencies=LockPlan(capacity=True))
        return self.get(identifier)

    def expired_owner(self)->UUID|None:
        """Bounded internal restart discovery; never exposes unexpired owners."""
        with self.read_connect() as conn:
            conn.execute("SET LOCAL statement_timeout='3s'"); conn.execute("SET LOCAL lock_timeout='1s'")
            row=conn.execute("SELECT owner_id FROM inversion.run_state WHERE state='running' AND deadline<=clock_timestamp() ORDER BY run_id LIMIT 1").fetchone()
            return None if row is None else row[0]

    def recover(self,owner):
        """Expired fence is proof of lost publication authority, never a UUID alone."""
        with self.connect() as conn:
            cur=conn.cursor(); self._lock(cur)
            cur.execute("SELECT run_id FROM inversion.run_state WHERE state='running' AND owner_id=%s AND deadline<=clock_timestamp() ORDER BY run_id",(owner,))
            for identifier, in cur.fetchall():
                run=self._read(cur,identifier,for_update=True)
                if run.state=='running': self._terminal(cur,run,'failed','deadline_expired')

    def publish(self,attempt,prepared,authority)->ResultRecord | None:
        """Publish an applicable result, or return None after durable blocking."""
        from .result_dispatch import validate_result
        validate_result(prepared)
        before=self.get(attempt.run_id)
        # Refreshed here even if a caller supplied an older valid snapshot.
        current=self._authority(before.authority,before.actor)
        self.validate_authority(authority,before.authority,before.actor)
        if canonical_bytes(prepared.snapshot.body['request'])!=canonical_bytes(public(before.request)):
            raise InversionError('snapshot_request_mismatch',status=409)
        actor=prepared.snapshot.body['actor']
        if (actor['domain'],actor['user_id'])!=(before.actor.domain,before.actor.user_id):
            raise InversionError('snapshot_actor_mismatch',status=409)
        if before.request_bytes+len(prepared.snapshot.canonical)>self.limits.input_bytes:
            raise InversionError('input_size_limit',status=409)
        with self.connect() as conn:
            cur=conn.cursor(); self._lock(cur,publication=True)
            run=self._read(cur,attempt.run_id,for_update=True); self._fence(run,attempt)
            if run.state=='completed':
                cur.execute('SELECT id,run_id,content_sha256 FROM inversion.results WHERE run_id=%s',(run.id,))
                return ResultRecord(*cur.fetchone())
            if run.state!='running': raise InversionError('run_terminal',status=409)
            if run.reason_code=='cancel_requested': raise InversionError('cancel_requested',status=409)
            cur.execute('SELECT clock_timestamp()'); now=cur.fetchone()[0]
            if now>=run.deadline: raise InversionError('deadline_expired',status=409)
            reasons=recheck_with_cursor(cur,prepared.snapshot)
            if reasons:
                self._terminal(cur,run,'blocked','input_changed',details=dict(reasons=list(reasons)))
                return None
            if prepared.status=='not_applicable':
                self._terminal(cur,run,'blocked','not_applicable',details=prepared.diagnostics)
                return None
            result=ResultRecord(uuid4(),run.id,prepared.content_sha256)
            cur.execute('INSERT INTO inversion.results(id,run_id,generation,input_snapshot,body,content_sha256) VALUES(%s,%s,%s,%s,%s,%s)',
                (result.id,run.id,attempt.generation,Jsonb(public(prepared.snapshot.body)),Jsonb(prepared.body),result.content_sha256))
            for ordinal,payload in enumerate(prepared.chunks):
                cur.execute('INSERT INTO inversion.result_chunks(result_id,ordinal,payload,content_sha256) VALUES(%s,%s,%s,%s)',
                    (result.id,ordinal,payload,sha256(payload).hexdigest()))
            self._terminal(cur,run,'completed',None,used_bytes=run.request_bytes+len(prepared.snapshot.canonical)+prepared.result_bytes)
            # Publication receipt is atomically bound to this exact attempt.
            cur.execute('INSERT INTO inversion.receipts(actor_domain,actor_id,key,request_hash,subject_id,version,content_sha256,response) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                (run.actor.domain,run.actor.user_id,attempt.id,prepared.content_sha256,result.id,run.version+1,result.content_sha256,Jsonb(dict(id=str(result.id),authority=authority_body(current)))))
        return result
