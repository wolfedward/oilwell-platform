"""Authorized incident operations; every result follows transaction commit."""
from dataclasses import asdict
from hashlib import sha256
import json
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .errors import ReliabilityError
from .facts import MAX_COUNTER, schedule_receipt
from .management_contracts import Actor, ManagementCommand, invalid


def _setup(cur):
    cur.execute("SET LOCAL lock_timeout='1s'")
    cur.execute("SET LOCAL statement_timeout='3s'")
    cur.execute("SET LOCAL idle_in_transaction_session_timeout='10s'")
    cur.execute("SET LOCAL search_path=pg_catalog,public,pg_temp")
    cur.execute("SET LOCAL timezone='UTC'")


def _missing():
    return ReliabilityError('ingest.not_found', '指定的接收记录不存在', 404)


def _expected(actual, expected):
    if actual != expected:
        raise ReliabilityError('ingest.version_conflict', '记录已经变化，请刷新核对后再操作', 409)


class ReliabilityOperations:
    def __init__(self, connect):
        if not callable(connect):
            raise invalid()
        self.connect = connect

    def execute(self, command):
        if not isinstance(command, ManagementCommand):
            raise invalid()
        command.__post_init__()
        # Authentication (including retries) occurs before this service in the
        # HTTP boundary. Lock the actor/key, then one case OR delivery row;
        # never take the ingestor/publisher capacity lock after a delivery.
        key_bytes = json.dumps(['ingest-operation', command.actor.domain, command.actor.user_id, str(command.key)],
                               ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        lock_key = int.from_bytes(sha256(key_bytes).digest()[:8], 'big', signed=True)
        try:
            with self.connect() as connection:
                with connection.cursor() as cur:
                    _setup(cur)
                    cur.execute('SELECT pg_advisory_xact_lock(%s)', (lock_key,))
                    identity = (command.actor.domain, command.actor.user_id, command.key)
                    cur.execute('SELECT request_hash,result FROM ingest_reliability.operation_receipts '
                                'WHERE actor_domain=%s AND actor_id=%s AND idempotency_key=%s', identity)
                    existing = cur.fetchone()
                    if existing:
                        if existing[0] != command.request_hash:
                            raise ReliabilityError('ingest.idempotency_conflict', '原操作标识已用于不同请求，不能更改后重放', 409)
                        result = existing[1] | {'replayed': True}
                    else:
                        if command.action == 'receipt.resend':
                            fact_id, case_id, receipt_id, before, after, instant = self._resend(cur, command)
                        else:
                            fact_id, case_id, receipt_id, before, after, instant = self._case(cur, command)
                        result = {'entity': {key: after[key] for key in ('id', 'version', 'state')}, 'replayed': False}
                        cur.execute('INSERT INTO ingest_reliability.audit_events '
                                    '(fact_id,case_id,receipt_id,actor,action,reason,before_data,after_data,recorded_at) '
                                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                                    (fact_id, case_id, receipt_id, Jsonb(asdict(command.actor)), command.action,
                                     command.reason, Jsonb(before), Jsonb(after), instant))
                        cur.execute('INSERT INTO ingest_reliability.operation_receipts '
                                    '(actor_domain,actor_id,idempotency_key,request_hash,result,recorded_at) '
                                    'VALUES (%s,%s,%s,%s,%s,%s)',
                                    (*identity, command.request_hash, Jsonb(result), instant))
            return result
        except psycopg.Error:
            raise ReliabilityError('ingest.operation_unavailable', '处置结果尚未确认，请保留原操作标识和内容重试') from None

    @staticmethod
    def _case(cur, command):
        cur.execute('SELECT fact_id,state,version FROM ingest_reliability.cases WHERE case_id=%s FOR UPDATE', (command.target,))
        row = cur.fetchone()
        if row is None:
            raise _missing()
        fact_id, state, version = row
        _expected(version, command.expected_version)
        allowed = {'case.ack': {'open'}, 'case.close': {'open', 'acknowledged'},
                   'case.reopen': {'closed'}, 'case.note': {'open', 'acknowledged', 'closed'}}
        if state not in allowed[command.action]:
            raise ReliabilityError('ingest.state_conflict', '当前案件状态不允许此操作', 409)
        if version == MAX_COUNTER:
            raise ReliabilityError('ingest.version_exhausted', '案件版本已达上限，未修改记录', 409)
        next_state = {'case.ack': 'acknowledged', 'case.close': 'closed', 'case.reopen': 'open'}.get(command.action, state)
        cur.execute('SELECT clock_timestamp()')
        instant = cur.fetchone()[0]
        cur.execute('UPDATE ingest_reliability.cases SET state=%s,version=%s,updated_at=greatest(updated_at,%s) WHERE case_id=%s',
                    (next_state, version + 1, instant, command.target))
        cur.execute('SELECT id FROM ingest_reliability.receipt_metadata WHERE fact_id=%s', (fact_id,))
        receipt = cur.fetchone()
        before = {'id': str(command.target), 'state': state, 'version': str(version)}
        after = {'id': str(command.target), 'state': next_state, 'version': str(version + 1)}
        return fact_id, command.target, receipt[0] if receipt else None, before, after, instant

    @staticmethod
    def _resend(cur, command):
        cur.execute('SELECT requested_generation,sent_generation,version FROM ingest_reliability.deliveries '
                    'WHERE receipt_id=%s FOR UPDATE', (command.target,))
        row = cur.fetchone()
        if row is None:
            raise _missing()
        _expected(row[2], command.expected_version)
        cur.execute('SELECT fact_id FROM ingest_reliability.receipt_metadata WHERE id=%s', (command.target,))
        fact_id = cur.fetchone()[0]
        cur.execute('SELECT id FROM ingest_reliability.case_metadata WHERE fact_id=%s', (fact_id,))
        case = cur.fetchone()
        cur.execute('SELECT clock_timestamp()')
        instant = cur.fetchone()[0]
        def metadata(values):
            return {'id': str(command.target), 'requested_generation': str(values[0]), 'sent_generation': str(values[1]),
                    'version': str(values[2]), 'state': 'pending' if values[0] > values[1] else 'broker_confirmed'}
        before = metadata(row)
        schedule_receipt(cur, command.target, instant)
        cur.execute('SELECT requested_generation,sent_generation,version FROM ingest_reliability.deliveries WHERE receipt_id=%s', (command.target,))
        after = metadata(cur.fetchone())
        return fact_id, case[0] if case else None, command.target, before, after, instant

    def read_raw(self, fact_id, actor, *, preview):
        if not isinstance(fact_id, UUID) or not isinstance(actor, Actor) or type(preview) is not bool:
            raise invalid()
        actor.__post_init__()
        try:
            with self.connect() as connection:
                with connection.cursor() as cur:
                    _setup(cur)
                    cur.execute('SELECT fact_id,payload_sha256,payload_bytes,raw_bytes '
                                'FROM ingest_reliability.audited_raw(%s,%s,%s)', (fact_id, Jsonb(asdict(actor)), preview))
                    row = cur.fetchone()
                    if row is None:
                        raise _missing()
                    data = bytes(row[3])
                    result = {'fact_id': str(row[0]), 'payload_sha256': row[1], 'payload_bytes': str(row[2]),
                              'text': data.decode('utf-8', errors='replace') if preview else None,
                              'raw_bytes': None if preview else data, 'truncated': preview and row[2] > 4096}
            # The audit written inside audited_raw must commit before any bytes
            # reach HTTP; even a lost commit reply returns only a safe error.
            return result
        except psycopg.Error:
            raise ReliabilityError('ingest.raw_unavailable', '原文访问审计尚未确认保存，未返回内容') from None
