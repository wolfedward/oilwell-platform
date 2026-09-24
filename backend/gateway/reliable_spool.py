"""Versioned exact-byte gateway queue; runtime never migrates its schema."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import math
import random
import re
import shutil
import sqlite3
import threading
import time
from uuid import UUID, uuid4

from backend.ingest_reliability.contracts import Observation, Receipt
from backend.ingest_reliability.errors import ReliabilityError
from .receipt_state import transition
from .spool_migrate import _error, _path, _verify_runtime, acquire_spool_lock


MAX_INTEGER = 2**63 - 1
STATES = ('pending', 'inflight', 'awaiting_receipt', 'stored', 'quarantined',
          'broker_confirmed', 'legacy_broker_confirmed', 'dead')


def _instant(value):
    if type(value) not in (float, int) or not -62135596800 <= value <= 253402300799 or not math.isfinite(value):
        raise _error('ingest.spool_clock_invalid')
    return float(value)


def _deadline(instant, seconds):
    deadline = _instant(instant + seconds)
    if deadline <= instant:
        raise _error('ingest.spool_clock_invalid')
    return deadline


@dataclass(frozen=True)
class SpoolLimits:
    max_messages: int
    max_payload_bytes: int
    min_free_bytes: int

    def __post_init__(self):
        for value in (self.max_messages, self.max_payload_bytes, self.min_free_bytes):
            if type(value) is not int or not 1 <= value <= MAX_INTEGER:
                raise _error('ingest.spool_limits_invalid')


@dataclass(frozen=True)
class SpoolLease:
    message_id: str
    token: str
    topic: str
    payload: bytes
    qos: int
    confirmation_mode: str
    attempts: int


class ReliableSpool:
    def __init__(self, path, *, limits, clock=None, receipt_timeout_seconds=60,
                 base_backoff_seconds=2, max_backoff_seconds=300):
        if not isinstance(limits, SpoolLimits) or (clock is not None and not callable(clock)):
            raise _error('ingest.spool_limits_invalid')
        for value in (receipt_timeout_seconds, base_backoff_seconds, max_backoff_seconds):
            if type(value) not in (float, int) or not 0 < value <= 300 or not math.isfinite(value):
                raise _error('ingest.spool_config_invalid')
        if base_backoff_seconds > max_backoff_seconds:
            raise _error('ingest.spool_config_invalid')
        self.receipt_timeout_seconds = float(receipt_timeout_seconds)
        self.base_backoff_seconds = float(base_backoff_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self.limits, self.clock = limits, clock or time.time
        self._lock = threading.RLock()
        self._connection = None
        self._ownership = None
        try:
            self.path = _path(path)
            if not self.path.is_file():
                raise _error('ingest.spool_migration_required')
            self._ownership = acquire_spool_lock(self.path)
            self._connection = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=1,
                                               check_same_thread=False, isolation_level=None)
            self._connection.row_factory = sqlite3.Row
            _verify_runtime(self._connection, limits)
            self._connection.execute('PRAGMA journal_mode=WAL')
            self._connection.execute('PRAGMA synchronous=FULL')
            self._connection.execute('PRAGMA foreign_keys=ON')
        except (OSError, sqlite3.Error):
            self.close()
            raise _error('ingest.spool_unavailable') from None
        except BaseException:
            self.close()
            raise

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if self._ownership is not None:
                self._ownership.release()
                self._ownership = None

    def _open(self):
        if self._connection is None:
            raise _error('ingest.spool_closed')
        return self._connection

    def _rollback(self, connection):
        try:
            connection.rollback()
        except sqlite3.Error:
            # A failed rollback leaves the connection's transaction uncertain.
            # Closing rolls back pending SQLite work; no caller may reuse it.
            self.close()

    @contextmanager
    def _write(self, now):
        with self._lock:
            connection = self._open()
            instant = _instant(self.clock() if now is None else now)
            _instant(instant + 300)  # Reserve representable room for bounded deadlines.
            try:
                connection.execute('BEGIN IMMEDIATE')
                self._clock(connection, instant)
                yield connection, instant
                connection.commit()
            except (OSError, sqlite3.Error):
                self._rollback(connection)
                raise _error('ingest.spool_write_unavailable') from None
            except BaseException:
                self._rollback(connection)
                raise

    def _clock(self, connection, instant):
        row = connection.execute('SELECT last_clock_seen FROM spool_capacity WHERE singleton=1').fetchone()
        if row is None:
            raise _error('ingest.spool_metadata_invalid')
        previous = _instant(row[0])
        if instant < previous:
            connection.execute('UPDATE uplink_queue_v2 SET next_attempt_at=CASE WHEN next_attempt_at<=? '
                               'THEN min(next_attempt_at,?) ELSE min(next_attempt_at,?) END,'
                               'lease_until=CASE WHEN lease_until IS NULL THEN NULL WHEN lease_until<=? '
                               'THEN min(lease_until,?) ELSE min(lease_until,?) END '
                               "WHERE state IN ('pending','inflight','awaiting_receipt')",
                               (previous, instant, instant + self.max_backoff_seconds, previous, instant, instant + 300))
            connection.execute('UPDATE spool_capacity SET last_clock_warning=? WHERE singleton=1', (instant,))
        connection.execute('UPDATE spool_capacity SET last_clock_seen=? WHERE singleton=1', (instant,))

    def enqueue(self, message_id, topic, payload, *, confirmation_mode, qos=1, now=None):
        if (not isinstance(message_id, str) or not message_id or len(message_id) > 128
                or any(ord(c) < 32 for c in message_id) or not isinstance(topic, str) or not topic
                or any(c in topic for c in ('\x00', '+', '#')) or type(payload) is not bytes
                or len(payload) > 1024 * 1024 or type(qos) is not int or qos not in (1, 2)
                or confirmation_mode not in ('broker', 'application') or (confirmation_mode == 'application' and qos != 1)):
            raise _error('ingest.spool_input_invalid')
        try:
            if len(topic.encode('utf-8')) > 65535:
                raise _error('ingest.spool_input_invalid')
            message_id.encode('utf-8')
        except UnicodeError:
            raise _error('ingest.spool_input_invalid') from None
        with self._write(now) as (connection, instant):
            old = connection.execute('SELECT topic,payload,confirmation_mode,qos FROM uplink_queue_v2 WHERE message_id=?', (message_id,)).fetchone()
            if old is not None:
                if tuple(old) != (topic, payload, confirmation_mode, qos):
                    raise _error('ingest.spool_identity_conflict')
                return False
            if shutil.disk_usage(self.path.parent).free < self.limits.min_free_bytes + len(payload) * 2 + 65536:
                raise _error('ingest.spool_disk_low')
            cursor = connection.execute('UPDATE spool_capacity SET used_messages=used_messages+1,used_payload_bytes=used_payload_bytes+? '
                                        'WHERE singleton=1 AND used_messages<max_messages AND ?<=max_payload_bytes-used_payload_bytes',
                                        (len(payload), len(payload)))
            if cursor.rowcount != 1:
                raise _error('ingest.spool_capacity_exhausted')
            connection.execute('INSERT INTO uplink_queue_v2(message_id,topic,payload,payload_sha256,qos,confirmation_mode,state,next_attempt_at,created_at) '
                               "VALUES(?,?,?,?,?,?,'pending',?,?)", (message_id, topic, payload, sha256(payload).hexdigest(), qos,
                                                                    confirmation_mode, instant, instant))
        return True

    def claim(self, *, now=None, limit=1, lease_seconds=60):
        if (type(limit) is not int or not 1 <= limit <= 200 or type(lease_seconds) not in (int, float)
                or not 0 < lease_seconds <= 300 or not math.isfinite(lease_seconds)):
            raise _error('ingest.spool_input_invalid')
        leases = []
        with self._write(now) as (connection, instant):
            deadline = _deadline(instant, lease_seconds)
            rows = connection.execute("SELECT * FROM uplink_queue_v2 WHERE "
                                      "(state IN ('pending','awaiting_receipt') AND next_attempt_at<=?) OR "
                                      "(state='inflight' AND lease_until<=?) ORDER BY created_at,message_id LIMIT ?",
                                      (instant, instant, limit)).fetchall()
            for row in rows:
                state = transition(row['state'], row['confirmation_mode'], 'claim')
                token, attempts = str(uuid4()), min(MAX_INTEGER, row['attempts'] + 1)
                connection.execute('UPDATE uplink_queue_v2 SET state=?,attempts=?,claimed_at=?,lease_until=?,lease_token=? '
                                   'WHERE message_id=?', (state, attempts, instant, deadline, token, row['message_id']))
                leases.append(SpoolLease(row['message_id'], token, row['topic'], row['payload'], row['qos'], row['confirmation_mode'], attempts))
        return leases

    def broker_ack(self, lease, *, now=None):
        self._validate_lease(lease)
        with self._write(now) as (connection, instant):
            row = self._leased_row(connection, lease, instant)
            if row is None:
                return False
            state = transition(row['state'], row['confirmation_mode'], 'broker_ack')
            delay = (max(self.receipt_timeout_seconds, self._backoff(row['attempts']))
                     if row['confirmation_mode'] == 'application' else 0)
            deadline = _deadline(instant, delay) if row['confirmation_mode'] == 'application' else instant
            connection.execute('UPDATE uplink_queue_v2 SET state=?,broker_acked_at=?,next_attempt_at=?,lease_token=NULL,'
                               'lease_until=NULL,claimed_at=NULL,last_error_code=NULL WHERE message_id=?',
                               (state, instant, deadline, row['message_id']))
        return True

    def fail(self, lease, error_code, *, now=None):
        self._validate_lease(lease)
        if type(error_code) is not str or re.fullmatch(r'[a-z][a-z0-9_.]{0,95}', error_code) is None:
            raise _error('ingest.spool_input_invalid')
        with self._write(now) as (connection, instant):
            row = self._leased_row(connection, lease, instant)
            if row is None:
                return False
            # The database, never the callback's copied DTO, owns mode/attempts.
            state = transition(row['state'], row['confirmation_mode'], 'exhausted' if row['attempts'] >= 12 else 'fail')
            connection.execute('UPDATE uplink_queue_v2 SET state=?,next_attempt_at=?,lease_token=NULL,lease_until=NULL,'
                               'claimed_at=NULL,last_error_code=? WHERE message_id=?',
                               (state, _deadline(instant, self._backoff(row['attempts'])), error_code, row['message_id']))
        return True

    @staticmethod
    def _validate_lease(lease):
        if (not isinstance(lease, SpoolLease) or type(lease.message_id) is not str or not lease.message_id
                or len(lease.message_id) > 128 or type(lease.token) is not str or len(lease.token) != 36):
            raise _error('ingest.spool_input_invalid')
        try:
            UUID(lease.token)
        except ValueError:
            raise _error('ingest.spool_input_invalid') from None

    @staticmethod
    def _leased_row(connection, lease, instant):
        return connection.execute("SELECT * FROM uplink_queue_v2 WHERE message_id=? AND lease_token=? "
                                  "AND state='inflight' AND lease_until>?", (lease.message_id, lease.token, instant)).fetchone()

    def _backoff(self, attempts):
        exponent = max(0, attempts - 1)
        # Compare logarithms first: even saturated attempts or tiny positive
        # configuration values cannot overflow a float/exponential operation.
        if exponent >= math.log2(self.max_backoff_seconds) - math.log2(self.base_backoff_seconds):
            bound = self.max_backoff_seconds
        else:
            bound = min(self.max_backoff_seconds, math.ldexp(self.base_backoff_seconds, exponent))
        return random.uniform(bound / 2, bound)

    def apply_receipt(self, topic, payload, *, retain=False, now=None):
        if type(topic) is not str or type(payload) is not bytes or type(retain) is not bool:
            raise _error('ingest.spool_input_invalid')
        try:
            topic_hash = sha256(topic.encode('utf-8')).hexdigest()
        except UnicodeError:
            raise _error('ingest.spool_input_invalid') from None
        payload_hash = sha256(payload).hexdigest()
        invalid, receipt = None, None
        if retain:
            invalid = 'receipt.retained'
        else:
            try:
                receipt = Receipt.from_bytes(payload)
            except ValueError:
                invalid = 'receipt.invalid'
        with self._write(now) as (connection, instant):
            if invalid is not None:
                self._security_event(connection, invalid, topic_hash, payload_hash, instant)
                return 'ignored'
            return self._apply_matching(connection, instant, topic, payload, receipt, topic_hash, payload_hash)

    def _apply_matching(self, connection, instant, topic, payload, receipt, topic_hash, payload_hash):
        def ignored(code):
            self._security_event(connection, code, topic_hash, payload_hash, instant)
            return 'ignored'

        row = connection.execute('SELECT * FROM uplink_queue_v2 WHERE message_id=?', (receipt.message_id,)).fetchone()
        if row is None:
            return ignored('receipt.unknown_message')
        if row['confirmation_mode'] != 'application':
            return ignored('receipt.wrong_mode')
        observed_at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=instant)
        observation = Observation(row['topic'], row['payload'], row['qos'], False, False, observed_at)
        if not receipt.matches(topic, observation):
            return ignored('receipt.mismatch')
        old = connection.execute('SELECT * FROM spool_receipts WHERE message_id=?', (receipt.message_id,)).fetchone()
        if old is not None:
            try:
                previous = Receipt.from_bytes(old['payload'])
            except ValueError:
                raise _error('ingest.spool_evidence_invalid') from None
            if row['state'] != previous.status or row['receipt_id'] != old['receipt_id']:
                raise _error('ingest.spool_evidence_invalid')
            if old['topic'] == topic and previous == receipt:
                return 'duplicate'
            return ignored('receipt.conflict')
        if row['state'] not in ('pending', 'inflight', 'awaiting_receipt'):
            raise _error('ingest.spool_evidence_invalid')
        receipt_id = str(UUID(receipt.receipt_id))
        if connection.execute('SELECT 1 FROM spool_receipts WHERE receipt_id=?', (receipt_id,)).fetchone() is not None:
            return ignored('receipt.identity_reuse')
        state = transition(row['state'], row['confirmation_mode'], receipt.status)
        connection.execute('INSERT INTO spool_receipts(receipt_id,message_id,topic,payload,received_at) VALUES(?,?,?,?,?)',
                           (receipt_id, receipt.message_id, topic, payload, instant))
        connection.execute('UPDATE uplink_queue_v2 SET state=?,receipt_id=?,lease_token=NULL,lease_until=NULL,'
                           'claimed_at=NULL,last_error_code=? WHERE message_id=?',
                           (state, receipt_id, None if state == 'stored' else receipt.reason_code, receipt.message_id))
        return 'applied'

    @staticmethod
    def _security_event(connection, code, topic_hash, payload_hash, instant):
        key = (code, topic_hash, payload_hash)
        found = connection.execute('UPDATE spool_security_events SET '
                                   'occurrences=CASE WHEN occurrences<? THEN occurrences+1 ELSE occurrences END,'
                                   'last_seen=max(last_seen,?) WHERE error_code=? AND topic_sha256=? AND payload_sha256=?',
                                   (MAX_INTEGER, instant, *key))
        if found.rowcount:
            return
        if connection.execute('SELECT count(*) FROM spool_security_events').fetchone()[0] >= 1024:
            connection.execute('UPDATE spool_capacity SET security_overflow_count=CASE WHEN security_overflow_count<? '
                               'THEN security_overflow_count+1 ELSE security_overflow_count END WHERE singleton=1', (MAX_INTEGER,))
            return
        connection.execute('INSERT INTO spool_security_events(error_code,occurrences,first_seen,last_seen,topic_sha256,payload_sha256) '
                           'VALUES(?,1,?,?,?,?)', (code, instant, instant, topic_hash, payload_hash))

    def get(self, message_id):
        with self._lock:
            try:
                row = self._open().execute('SELECT * FROM uplink_queue_v2 WHERE message_id=?', (message_id,)).fetchone()
                return dict(row) if row is not None else None
            except sqlite3.Error:
                raise _error('ingest.spool_read_unavailable') from None

    def counts(self):
        with self._lock:
            try:
                result = dict.fromkeys(STATES, 0)
                for state, count in self._open().execute('SELECT state,count(*) FROM uplink_queue_v2 GROUP BY state'):
                    result[state] = count
                return result
            except sqlite3.Error:
                raise _error('ingest.spool_read_unavailable') from None
