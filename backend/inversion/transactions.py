"""Injected atomic mutations; callers freshly authorize BEFORE calling run, even retries."""
from dataclasses import asdict, dataclass
from enum import Enum
from hashlib import sha256
from uuid import UUID, uuid4
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from backend.registry.transactions import lock_history
from backend.waveforms.business import lock_dependencies as waveform_guard
from .contracts import InversionCommand, canonical_bytes, strict_json, digest, integer, text

DEPENDENCY_GUARD = 499001610


class InversionError(RuntimeError):
    def __init__(self,code,message='Inversion operation refused',status=503):
        self.code='inversion.'+code; self.message=message; self.status=status
        super().__init__(message)


@dataclass(frozen=True)
class MutationRequest:
    action: str
    target: str
    canonical_body: bytes

    def __post_init__(self):
        text(self.action,maximum=128); text(self.target,maximum=256)
        if canonical_bytes(strict_json(self.canonical_body)) != self.canonical_body:
            raise ValueError('Validated canonical JSON bytes required')


class DependencyKind(Enum):
    ASSEMBLY='assemblies'
    ASSEMBLY_VERSION='assembly_versions'
    PARAMETER='parameter_versions'
    MODEL='model_versions'
    RUN='run_requests'
    RESULT='results'


@dataclass(frozen=True)
class Dependency:
    kind: DependencyKind
    id: UUID

    def __post_init__(self):
        if type(self.kind) is not DependencyKind or type(self.id) is not UUID: raise ValueError('Internal dependency identity required')


@dataclass(frozen=True)
class LockPlan:
    waveform: bool=False
    registry: bool=False
    waveform_payer: str | None=None
    capacity: bool=False
    rows: tuple[Dependency,...]=()

    def __post_init__(self):
        if any(type(v) is not bool for v in (self.waveform,self.registry,self.capacity)): raise ValueError('Explicit lock flags required')
        if self.waveform_payer is not None:
            text(self.waveform_payer,maximum=128)
            if not self.waveform: raise ValueError('Waveform capacity requires dependency guard')
        if type(self.rows) is not tuple or any(type(r) is not Dependency for r in self.rows): raise ValueError('Internal dependencies required')


@dataclass(frozen=True)
class Change:
    action: str
    subject_id: UUID
    version: int
    content_sha256: str
    response: dict


@dataclass(frozen=True)
class Receipt:
    subject_id: UUID
    version: int
    content_sha256: str
    replayed: bool


def lock(cur,plan):
    if plan.waveform: waveform_guard(cur)
    if plan.registry: lock_history(cur,shared=True)
    cur.execute('SELECT pg_advisory_xact_lock(%s)',(DEPENDENCY_GUARD,))
    # Requires separately reviewed cross-schema grants; no owner fallback.
    if plan.waveform_payer is not None:
        cur.execute('SELECT * FROM waveforms.lock_business_capacity(%s)',(plan.waveform_payer,))
    if plan.capacity: cur.execute('SELECT singleton FROM inversion.capacity WHERE singleton=1 FOR UPDATE')
    for dependency in sorted(set(plan.rows),key=lambda r:(r.kind.value,str(r.id))):
        cur.execute(sql.SQL('SELECT id FROM inversion.{} WHERE id=%s FOR UPDATE').format(sql.Identifier(dependency.kind.value)),(dependency.id,))
        if cur.fetchone() is None: raise InversionError('dependency_missing',status=409)


class InversionTransactions:
    def __init__(self,connect): self.connect=connect

    def run(self,command:InversionCommand,apply,*,request:MutationRequest,dependencies:LockPlan)->Receipt:
        if type(command) is not InversionCommand or type(request) is not MutationRequest or type(dependencies) is not LockPlan:
            raise ValueError('Validated command, request and lock plan required')
        request_hash=sha256(canonical_bytes({'action':request.action,'target':request.target,
            'body':strict_json(request.canonical_body),'expected':asdict(command.expected),'reason':command.reason})).hexdigest()
        try:
            with self.connect() as conn:
                with conn.transaction(),conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout='3s'")
                    cur.execute("SET LOCAL lock_timeout='1s'")
                    lock(cur,dependencies)
                    cur.execute('SELECT request_hash,subject_id,version,content_sha256 FROM inversion.receipts WHERE actor_domain=%s AND actor_id=%s AND key=%s',
                                (command.actor.domain,command.actor.user_id,command.key))
                    previous=cur.fetchone()
                    if previous:
                        if previous[0]!=request_hash: raise InversionError('idempotency_conflict',status=409)
                        receipt=Receipt(*previous[1:],True)
                    else:
                        change=apply(cur,command)
                        if type(change) is not Change or change.action!=request.action or type(change.subject_id) is not UUID:
                            raise ValueError('Change must match mutation action and identity')
                        integer(change.version,minimum=1); digest(change.content_sha256)
                        response=strict_json(canonical_bytes(change.response))
                        cur.execute('INSERT INTO inversion.audit(id,actor,action,subject_id,reason,request_key,response) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                                    (uuid4(),Jsonb(asdict(command.actor)),change.action,change.subject_id,command.reason,command.key,Jsonb(response)))
                        cur.execute('INSERT INTO inversion.receipts(actor_domain,actor_id,key,request_hash,subject_id,version,content_sha256,response) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                                    (command.actor.domain,command.actor.user_id,command.key,request_hash,change.subject_id,change.version,change.content_sha256,Jsonb(response)))
                        cur.execute('UPDATE inversion.state_head SET version=version+1 WHERE singleton=1')
                        receipt=Receipt(change.subject_id,change.version,change.content_sha256,False)
            return receipt
        except (psycopg.errors.UniqueViolation,psycopg.errors.ExclusionViolation,psycopg.errors.SerializationFailure) as error:
            raise InversionError('write_conflict',status=409) from error
        except psycopg.Error as error:
            raise InversionError('unavailable') from error
