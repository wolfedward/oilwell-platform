"""Bounded R25 business guard/accounting/receipts; no caller-selected tables."""
from dataclasses import asdict, fields
from hashlib import sha256
from psycopg.types.json import Jsonb
from .capacity import row
from .config import Quota
from .evidence_contracts import canonical_bytes
from .errors import WaveformError

DEPENDENCY_GUARD = int.from_bytes(sha256(canonical_bytes(['waveform.business-lock/1','dependencies'])).digest()[:8],'big',signed=True)


def lock_dependencies(cursor):
    cursor.execute('SELECT pg_advisory_xact_lock(%s)',(DEPENDENCY_GUARD,))


def check_payer(cursor,bucket):
    cursor.execute('SELECT accounting_bucket FROM waveforms.business_settings WHERE singleton=1')
    if cursor.fetchone()!=(bucket,):
        raise WaveformError('waveform.business_payer_mismatch','Configured business payer differs')


def lock_payer(cursor,bucket,limits):
    check_payer(cursor,bucket)
    cursor.execute('SELECT * FROM waveforms.lock_business_capacity(%s)',(bucket,))
    current=row(cursor)
    if current is None or any(current[f.name]!=getattr(limits.gateways[bucket],f.name) for f in fields(Quota)):
        raise WaveformError('waveform.capacity_configuration','Configured business quota differs')
    return current


def receipt(cursor,command,digest):
    cursor.execute('SELECT request_hash,result FROM waveforms.business_receipts WHERE actor_domain=%s AND actor_id=%s AND idempotency_key=%s',
                   (command.actor.domain,command.actor.user_id,command.key))
    previous=cursor.fetchone()
    if previous is None: return None
    if previous[0]!=digest: raise WaveformError('waveform.idempotency_conflict','Command key already binds a different request',409)
    return previous[1]


def finish(cursor,bucket,command,digest,operation,subject,result):
    cursor.execute('UPDATE waveforms.state_head SET version=version+1 WHERE singleton=1 RETURNING version')
    revision=cursor.fetchone()[0]
    cursor.execute('INSERT INTO waveforms.business_audit(accounting_bucket,actor,action,subject_id,details) VALUES(%s,%s,%s,%s,%s)',
        (bucket,Jsonb(asdict(command.actor)),operation,subject,Jsonb({'reason':command.reason,'revision':str(revision)})))
    cursor.execute('INSERT INTO waveforms.business_receipts(accounting_bucket,actor_domain,actor_id,idempotency_key,request_hash,result) VALUES(%s,%s,%s,%s,%s,%s)',
        (bucket,command.actor.domain,command.actor.user_id,command.key,digest,Jsonb(result)))
