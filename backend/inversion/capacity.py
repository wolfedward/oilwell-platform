"""Persistent admission and settlement on the caller's transaction.

Accounting scope is logical retained run input AND result artifact bytes, plus
outstanding reservations. The worker must include input snapshots, point chunks,
intermediate arrays and metrics that it retains, including on failed runs.
Governance/audit/receipt metadata, PostgreSQL tuple/index overhead, WAL and disk
allocation are excluded; deployment must monitor those independently.
"""
from dataclasses import asdict, dataclass, field
from uuid import UUID
from .transactions import InversionError

ACCOUNTING_SCOPE='run_input_and_results'


@dataclass(frozen=True)
class InversionLimits:
    total_storage_bytes: int
    running_slots: int=field(default=1,init=False)
    queued_requests: int=field(default=16,init=False)
    pairs_per_request: int=field(default=1,init=False)
    captures_per_request: int=field(default=2,init=False)
    capture_decoded_bytes: int=field(default=16*1024**2,init=False)
    capture_raw_bytes: int=field(default=32*1024**2,init=False)
    selection_samples: int=field(default=32768,init=False)
    input_bytes: int=field(default=64*1024**2,init=False)
    result_bytes: int=field(default=16*1024**2,init=False)
    policy_bytes: int=field(default=256*1024,init=False)
    deadline_seconds: int=field(default=180,init=False)
    statement_seconds: int=field(default=3,init=False)
    lock_seconds: int=field(default=1,init=False)

    def __post_init__(self):
        if type(self.total_storage_bytes) is not int or not 1<=self.total_storage_bytes<=2**63-1:
            raise ValueError('Explicit positive durable storage ceiling required')

    @property
    def accounting_scope(self): return ACCOUNTING_SCOPE


@dataclass(frozen=True)
class Reservation:
    run_id: UUID
    input_bytes: int
    result_bytes: int
    version: int


def usage(cur)->dict:
    """Read-only operational snapshot; values alone do not certify disk health."""
    cur.execute('SELECT total_storage_bytes,queued,running,reserved_bytes,used_bytes,version FROM inversion.capacity WHERE singleton=1')
    row=cur.fetchone()
    if row is None: raise InversionError('capacity_missing')
    return dict(zip(('total_storage_bytes','queued','running','reserved_bytes','used_bytes','version'),row),accounting_scope=ACCOUNTING_SCOPE)


def _lock(cur):
    cur.execute("SET LOCAL statement_timeout='3s'")
    cur.execute("SET LOCAL lock_timeout='1s'")
    cur.execute('SELECT limits,queued,running,reserved_bytes,used_bytes,total_storage_bytes FROM inversion.capacity WHERE singleton=1 FOR UPDATE')
    row=cur.fetchone()
    if row is None: raise InversionError('capacity_missing')
    return row


def reserve(cur,*,run_id:UUID,limits:InversionLimits)->Reservation:
    if type(run_id) is not UUID or type(limits) is not InversionLimits: raise ValueError('Validated reservation required')
    configured,queued,running,reserved,used,total=_lock(cur)
    if configured!=asdict(limits) or total!=limits.total_storage_bytes: raise InversionError('capacity_configuration')
    cur.execute('SELECT input_bytes,result_bytes,version,state FROM inversion.reservations WHERE run_id=%s',(run_id,))
    previous=cur.fetchone()
    if previous:
        if previous[3]=='settled': raise InversionError('reservation_settled',status=409)
        return Reservation(run_id,previous[0],previous[1],1)
    amount=limits.input_bytes+limits.result_bytes
    if queued>=limits.queued_requests or reserved+used+amount>total: raise InversionError('capacity_exhausted',status=409)
    cur.execute("INSERT INTO inversion.reservations(run_id,input_bytes,result_bytes,state) VALUES(%s,%s,%s,'queued')",(run_id,limits.input_bytes,limits.result_bytes))
    cur.execute('UPDATE inversion.capacity SET queued=queued+1,reserved_bytes=reserved_bytes+%s,version=version+1 WHERE singleton=1',(amount,))
    return Reservation(run_id,limits.input_bytes,limits.result_bytes,1)


def _reservation(cur,reservation):
    if type(reservation) is not Reservation or type(reservation.run_id) is not UUID or reservation.version!=1:
        raise ValueError('Original reservation token required')
    cur.execute('SELECT input_bytes,result_bytes,version,state,used_bytes FROM inversion.reservations WHERE run_id=%s FOR UPDATE',(reservation.run_id,))
    row=cur.fetchone()
    if row is None or row[:2]!=(reservation.input_bytes,reservation.result_bytes): raise InversionError('reservation_mismatch',status=409)
    return row


def claim(cur,reservation:Reservation)->None:
    configured,queued,running,*_=_lock(cur)
    row=_reservation(cur,reservation)
    if row[3]!='queued': raise InversionError('reservation_not_queued',status=409)
    if running>=configured['running_slots']: raise InversionError('slot_exhausted',status=409)
    cur.execute("UPDATE inversion.reservations SET state='running',version=version+1 WHERE run_id=%s",(reservation.run_id,))
    cur.execute('UPDATE inversion.capacity SET queued=queued-1,running=running+1,version=version+1 WHERE singleton=1')


def settle(cur,reservation:Reservation,used_bytes:int)->None:
    if type(used_bytes) is not int or used_bytes<0: raise ValueError('Nonnegative integer settlement required')
    _lock(cur)
    row=_reservation(cur,reservation)
    if used_bytes>row[0]+row[1]: raise InversionError('settlement_exceeded',status=409)
    if row[3]=='settled':
        if row[4]!=used_bytes: raise InversionError('settlement_conflict',status=409)
        return
    queued,running=int(row[3]=='queued'),int(row[3]=='running')
    cur.execute("UPDATE inversion.reservations SET state='settled',used_bytes=%s,version=version+1 WHERE run_id=%s",(used_bytes,reservation.run_id))
    cur.execute('UPDATE inversion.capacity SET queued=queued-%s,running=running-%s,reserved_bytes=reserved_bytes-%s,used_bytes=used_bytes+%s,version=version+1 WHERE singleton=1',
                (queued,running,row[0]+row[1],used_bytes))
