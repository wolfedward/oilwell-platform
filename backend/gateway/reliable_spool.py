"""Versioned exact-byte gateway queue; runtime never migrates its schema."""
from dataclasses import dataclass
from hashlib import sha256
import math
import shutil
import sqlite3
import threading
import time

from backend.ingest_reliability.errors import ReliabilityError
from .spool_migrate import _error, _path, _verify_runtime, acquire_spool_lock


MAX_INTEGER = 2**63 - 1
STATES = ('pending', 'inflight', 'awaiting_receipt', 'stored', 'quarantined',
          'broker_confirmed', 'legacy_broker_confirmed', 'dead')


def _instant(value):
    if type(value) not in (float, int) or not -62135596800 <= value <= 253402300799 or not math.isfinite(value):
        raise _error('ingest.spool_clock_invalid')
    return float(value)


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
    def __init__(self, path, *, limits, clock=None):
        if not isinstance(limits, SpoolLimits) or (clock is not None and not callable(clock)):
            raise _error('ingest.spool_limits_invalid')
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
        instant = _instant(self.clock() if now is None else now)
        with self._lock:
            connection = self._open()
            try:
                connection.execute('BEGIN IMMEDIATE')
                old = connection.execute('SELECT topic,payload,confirmation_mode,qos FROM uplink_queue_v2 WHERE message_id=?', (message_id,)).fetchone()
                if old is not None:
                    if tuple(old) != (topic, payload, confirmation_mode, qos):
                        raise _error('ingest.spool_identity_conflict')
                    connection.commit()
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
                connection.commit()
                return True
            except (OSError, sqlite3.Error):
                connection.rollback()
                raise _error('ingest.spool_write_unavailable') from None
            except BaseException:
                connection.rollback()
                raise

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
