"""Leased delivery of already-committed immutable application receipts."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import random
import re
from uuid import UUID, uuid4

import psycopg

from .errors import ReliabilityError
from .facts import MAX_COUNTER


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReliabilityError('ingest.outbox_input', '回执发送参数超出允许范围')


def _instant(value):
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReliabilityError('ingest.clock_invalid', '回执发送时间必须包含有效时区')
    try:
        instant = value.astimezone(timezone.utc)
        instant + timedelta(seconds=3600)
    except (OverflowError, ValueError):
        raise ReliabilityError('ingest.clock_invalid', '回执发送时间超出可表示范围') from None
    return instant


@dataclass(frozen=True)
class DeliveryLease:
    receipt_id: UUID
    token: UUID
    generation: int
    topic: str
    payload: bytes
    attempts: int


class OutboxRepository:
    def __init__(self, connect, *, retry_max_seconds=300):
        if not callable(connect):
            raise ValueError('outbox connection factory is required')
        _integer(retry_max_seconds, 1, 3600)
        self.connect = connect
        self.retry_max_seconds = retry_max_seconds
        self.worker_id = uuid4()

    @staticmethod
    def _version(value):
        if value >= MAX_COUNTER:
            raise ReliabilityError('ingest.generation_exhausted', '回执投递版本已达上限，未改变发送状态')

    def _clock(self, cur, now):
        cur.execute("SET LOCAL lock_timeout='1s'")
        cur.execute("SET LOCAL statement_timeout='3s'")
        cur.execute("SET LOCAL idle_in_transaction_session_timeout='10s'")
        cur.execute("SET LOCAL search_path=pg_catalog,public,pg_temp")
        cur.execute("SET LOCAL timezone='UTC'")
        # Keep the same capacity-before-delivery lock order as the ingestor.
        cur.execute('SELECT last_clock_seen FROM ingest_reliability.capacity WHERE singleton=1 FOR UPDATE')
        row = cur.fetchone()
        if row is None:
            raise ReliabilityError('ingest.capacity_missing', '回执发送时钟锚点缺失，未领取发送任务')
        if now is None:
            cur.execute('SELECT clock_timestamp()')
            now = _instant(cur.fetchone()[0])
        previous = row[0]
        if previous is not None and now < previous:
            retry_bound, lease_bound = now + timedelta(seconds=self.retry_max_seconds), now + timedelta(seconds=300)
            cur.execute('SELECT receipt_id,version FROM ingest_reliability.deliveries '
                        'WHERE requested_generation>sent_generation AND '
                        '(next_attempt_at>%s OR lease_until>%s) FOR UPDATE', (retry_bound, lease_bound))
            for _receipt, version in cur.fetchall():
                self._version(version)
            cur.execute('UPDATE ingest_reliability.deliveries SET next_attempt_at=least(next_attempt_at,%s),'
                        'lease_until=CASE WHEN lease_until IS NULL THEN NULL ELSE least(lease_until,%s) END,'
                        'version=version+1 WHERE requested_generation>sent_generation '
                        'AND (next_attempt_at>%s OR lease_until>%s)',
                        (retry_bound, lease_bound, retry_bound, lease_bound))
            cur.execute('INSERT INTO ingest_reliability.worker_health(worker_id,last_seen,last_failure,error_code) '
                        "VALUES (%s,%s,%s,'ingest.clock_rollback') ON CONFLICT(worker_id) DO UPDATE SET "
                        'last_seen=EXCLUDED.last_seen,last_failure=EXCLUDED.last_failure,error_code=EXCLUDED.error_code',
                        (self.worker_id, now, now))
        # Store the new anchor, rather than repeating the rebase on every poll.
        cur.execute('UPDATE ingest_reliability.capacity SET last_clock_seen=%s WHERE singleton=1', (now,))
        return now

    def claim(self, *, limit=20, lease_seconds=30, now=None):
        _integer(limit, 1, 200)
        _integer(lease_seconds, 1, 300)
        now = _instant(now)
        leases = []
        try:
            with self.connect() as connection:
                with connection.cursor() as cur:
                    instant = self._clock(cur, now)
                    cur.execute('SELECT d.receipt_id,d.requested_generation,d.attempts,d.version,r.topic,r.payload '
                                'FROM ingest_reliability.deliveries d JOIN ingest_reliability.receipts r USING(receipt_id) '
                                'WHERE d.requested_generation>d.sent_generation AND d.next_attempt_at<=%s '
                                'AND (d.lease_until IS NULL OR d.lease_until<=%s) '
                                'ORDER BY d.next_attempt_at,d.receipt_id LIMIT %s FOR UPDATE OF d SKIP LOCKED',
                                (instant, instant, limit))
                    rows = cur.fetchall()
                    for _receipt, _generation, _attempts, version, _topic, _payload in rows:
                        self._version(version)
                    for receipt_id, generation, attempts, _version, topic, payload in rows:
                        token, attempts = uuid4(), min(attempts + 1, MAX_COUNTER)
                        cur.execute('UPDATE ingest_reliability.deliveries SET lease_token=%s,lease_generation=%s,'
                                    'lease_until=%s,attempts=%s,version=version+1 WHERE receipt_id=%s',
                                    (token, generation, instant + timedelta(seconds=lease_seconds), attempts, receipt_id))
                        leases.append(DeliveryLease(receipt_id, token, generation, topic, bytes(payload), attempts))
            return leases
        except psycopg.Error:
            raise ReliabilityError('ingest.storage_unavailable', '回执领取未确认成功，等待持久租约恢复') from None

    def finish(self, lease, *, success, error_code=None, now=None):
        if not isinstance(lease, DeliveryLease) or not isinstance(lease.receipt_id, UUID) or not isinstance(lease.token, UUID):
            raise ReliabilityError('ingest.outbox_input', '回执发送租约无效')
        _integer(lease.generation, 1, MAX_COUNTER)
        if type(success) is not bool or (success and error_code is not None):
            raise ReliabilityError('ingest.outbox_input', '回执发送结果无效')
        if error_code is not None and (not isinstance(error_code, str) or not re.fullmatch(r'[a-z][a-z0-9_.]{0,95}', error_code)):
            raise ReliabilityError('ingest.outbox_input', '回执错误只能使用有限安全代码')
        now = _instant(now)
        try:
            with self.connect() as connection:
                with connection.cursor() as cur:
                    instant = self._clock(cur, now)
                    cur.execute('SELECT lease_token,lease_generation,lease_until,attempts,version '
                                'FROM ingest_reliability.deliveries WHERE receipt_id=%s FOR UPDATE', (lease.receipt_id,))
                    row = cur.fetchone()
                    if row is None or row[:2] != (lease.token, lease.generation) or row[2] <= instant:
                        return False
                    self._version(row[4])
                    if success:
                        cur.execute('UPDATE ingest_reliability.deliveries SET sent_generation=%s,lease_token=NULL,'
                                    'lease_generation=NULL,lease_until=NULL,last_error_code=NULL,broker_acked_at=%s,'
                                    'next_attempt_at=least(next_attempt_at,%s),version=version+1 '
                                    'WHERE receipt_id=%s AND lease_token=%s AND lease_generation=%s AND lease_until>%s',
                                    (lease.generation, instant, instant, lease.receipt_id, lease.token, lease.generation, instant))
                    else:
                        # Saturating attempts remain retryable. Bound the exponent
                        # before computing it, even for BIGINT-sized counters.
                        maximum = min(self.retry_max_seconds, 2 ** min(row[3], 12))
                        retry_at = instant + timedelta(seconds=random.uniform(maximum / 2, maximum))
                        cur.execute('UPDATE ingest_reliability.deliveries SET lease_token=NULL,lease_generation=NULL,'
                                    'lease_until=NULL,next_attempt_at=%s,last_error_code=%s,version=version+1 '
                                    'WHERE receipt_id=%s AND lease_token=%s AND lease_generation=%s AND lease_until>%s',
                                    (retry_at, error_code or 'ingest.publish_failed', lease.receipt_id, lease.token,
                                     lease.generation, instant))
                    applied = cur.rowcount == 1
            return applied
        except psycopg.Error:
            raise ReliabilityError('ingest.storage_unavailable', '回执发送状态未确认保存，保留持久记录等待恢复') from None
