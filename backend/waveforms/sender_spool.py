"""Dedicated exact-byte local queue. No migration, deletion, or center imports."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import BinaryIO
from uuid import UUID, uuid4

from .contracts import CaptureKey, ChunkFrame, MAX_I64, ReceiptExpectation, WaveReceipt, valid_name, valid_prefix
from .errors import ContractError
from .files import MAX_FILE_BYTES, iter_file_frames, scan_file
from .sender_paths import APPLICATION_ID, Ownership, failure
from .wire import parse_frame

SQL_PATH = Path(__file__).with_name('sql') / 'sender_001.sql'


def _structure(db):
    rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    return sha256(json.dumps([tuple(row) for row in rows], separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class SenderMessage:
    queue_id: str
    ordinal: int
    topic: str
    payload: bytes
    sha256: str


class WaveSenderSpool:
    @classmethod
    def initialize(cls, path, *, max_bytes: int):
        return cls(path, max_bytes=max_bytes, initialize=True)

    @classmethod
    def open(cls, path, *, max_bytes, gateway_id, topic_prefix):
        spool = cls(path, max_bytes=max_bytes)
        try:
            spool.bind(gateway_id, topic_prefix)
            return spool
        except BaseException:
            spool.close()
            raise

    def __init__(self, path, *, max_bytes, initialize=False):
        self._lock = threading.RLock()
        self._db = self._owner = None
        self._pid = os.getpid()
        self._initializing = initialize
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_I64:
            raise failure('budget_invalid')
        self.max_bytes = max_bytes
        source = SQL_PATH.read_text(encoding='utf-8')
        source_hash = sha256(source.encode()).hexdigest()
        try:
            self._owner = Ownership(path)
            self.path = self._owner.path
            fresh = not self.path.exists()
            if fresh:
                if not initialize or any(Path(str(self.path) + s).exists() for s in ('.identity', '-journal', '-wal', '-shm')):
                    raise failure('initialization_required')
                with self.path.open('xb'):
                    pass
                token = str(uuid4())
            else:
                token = self._owner.read(source_hash)['token']
            self._db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True,
                isolation_level=None, check_same_thread=False, timeout=1)
            self._db.row_factory = sqlite3.Row
            # Only newly created or identity/header-verified dedicated files reach
            # SQLite recovery or mutable PRAGMAs. Foreign files never reach here.
            if not fresh:
                self._verify(source_hash, token)
            self._db.execute('PRAGMA journal_mode=DELETE')
            self._db.execute('PRAGMA synchronous=EXTRA')
            self._db.execute('PRAGMA foreign_keys=ON')
            if (self._db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
                    or self._db.execute('PRAGMA synchronous').fetchone()[0] != 3
                    or self._db.execute('PRAGMA foreign_keys').fetchone()[0] != 1):
                raise failure('durability_invalid')
            if fresh:
                self._db.execute('BEGIN IMMEDIATE')
                statement = ''
                for line in source.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self._db.execute(statement)
                        statement = ''
                if statement.strip():
                    raise failure('schema_invalid')
                self._db.execute('PRAGMA application_id=' + str(APPLICATION_ID))
                self._db.execute('PRAGMA user_version=1')
                self._db.execute('INSERT INTO sender_metadata VALUES(1,1,?,?,?,?,NULL,NULL)',
                    (source_hash, _structure(self._db), token, max_bytes))
                self._db.execute('INSERT INTO sender_event_cursor VALUES(1,0)')
                self._db.commit()
                self._owner.establish(source_hash, token)
            self._verify(source_hash, token)
        except BaseException as error:
            self.close()
            if isinstance(error, (OSError, sqlite3.Error, ValueError)):
                raise failure('unavailable') from None
            raise

    def _verify(self, source_hash, token):
        db = self._db
        if db.execute('PRAGMA user_version').fetchone()[0] != 1 or db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
            raise failure('schema_invalid')
        row = db.execute('SELECT * FROM sender_metadata WHERE singleton=1').fetchone()
        if row is None or (row['version'], row['source_sha256'], row['structure_sha256'], row['owner_token']) != (1, source_hash, _structure(db), token):
            raise failure('schema_invalid')
        if row['max_bytes'] != self.max_bytes:
            raise failure('binding_mismatch')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise failure('integrity_invalid')
        wrong = db.execute('SELECT 1 FROM sender_capture c WHERE charge != max(length(content),1024) + 1024 * '
            '(1+(SELECT count(*) FROM sender_message m WHERE m.queue_id=c.queue_id)) LIMIT 1').fetchone()
        used = db.execute('SELECT coalesce(sum(charge),0) FROM sender_capture').fetchone()[0]
        if wrong or used > self.max_bytes:
            raise failure('accounting_invalid')
        self.gateway_id, self.topic_prefix = row['gateway_id'], row['topic_prefix']

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._db is None or os.getpid() != self._pid:
                raise failure('closed')
            try:
                self._owner.check()
                self._db.execute('BEGIN IMMEDIATE')
                yield self._db
                self._owner.check()
                self._db.commit()
            except BaseException as error:
                try:
                    self._db.rollback()
                except sqlite3.Error:
                    pass
                if isinstance(error, (OSError, sqlite3.Error)):
                    # An uncertain commit must never yield success or be followed
                    # by more work on the same connection.
                    self.close()
                    raise failure('unavailable') from None
                raise

    def bind(self, gateway_id, topic_prefix):
        if not valid_name(gateway_id) or not valid_prefix(topic_prefix):
            raise failure('binding_mismatch')
        with self._transaction() as db:
            if self.gateway_id is None and self._initializing:
                db.execute('UPDATE sender_metadata SET gateway_id=?,topic_prefix=? WHERE singleton=1', (gateway_id, topic_prefix))
            elif (self.gateway_id, self.topic_prefix) != (gateway_id, topic_prefix):
                raise failure('binding_mismatch')
        self.gateway_id, self.topic_prefix = gateway_id, topic_prefix

    def enqueue(self, source: BinaryIO) -> UUID:
        # One bounded snapshot; scanning and storage never reread a mutable source.
        try:
            source.seek(0)
            content = source.read(MAX_FILE_BYTES + 1)
        except (AttributeError, OSError, TypeError, ValueError):
            raise ContractError('contract_invalid') from None
        if type(content) is not bytes or len(content) > MAX_FILE_BYTES:
            raise ContractError('payload_too_large')
        scanned = scan_file(BytesIO(content))
        key = scanned.manifest.key
        if key.gateway_id != self.gateway_id or self.topic_prefix is None:
            raise failure('binding_mismatch')
        charge = max(len(content), 1024) + 1024 * (1 + len(scanned.lines))
        with self._transaction() as db:
            old = db.execute('SELECT queue_id,file_sha256 FROM sender_capture WHERE gateway_id=? AND device_id=? AND capture_id=?',
                (key.gateway_id, key.device_id, str(key.capture_id))).fetchone()
            if old:
                if old['file_sha256'] != scanned.sha256:
                    raise failure('capture_conflict')
                return UUID(old['queue_id'])
            used = db.execute('SELECT coalesce(sum(charge),0) FROM sender_capture').fetchone()[0]
            if used + charge > self.max_bytes:
                raise failure('capacity_exhausted')
            identifier = uuid4()
            db.execute('INSERT INTO sender_capture VALUES(?,?,?,?,?,?,?,?,?,NULL)',
                (str(identifier), key.gateway_id, key.device_id, str(key.capture_id), scanned.sha256,
                 scanned.manifest_sha256, content, charge, 'awaiting_capture_receipt'))
            for ordinal, (line, raw) in enumerate(zip(scanned.lines, iter_file_frames(BytesIO(content)))):
                frame = parse_frame(raw)
                db.execute('INSERT INTO sender_message(queue_id,ordinal,offset,length,payload_sha256,stream_id,chunk_index) VALUES(?,?,?,?,?,?,?)',
                    (str(identifier), ordinal, line.offset, line.length, line.sha256,
                     frame.stream_id if isinstance(frame, ChunkFrame) else None,
                     frame.chunk_index if isinstance(frame, ChunkFrame) else None))
        return identifier

    def _event(self, db, code):
        slot = db.execute('SELECT next_slot FROM sender_event_cursor WHERE singleton=1').fetchone()[0]
        db.execute('INSERT OR REPLACE INTO sender_event VALUES(?,?)', (slot, code))
        db.execute('UPDATE sender_event_cursor SET next_slot=? WHERE singleton=1', ((slot + 1) % 128,))

    def apply_receipt(self, topic: str, payload: bytes) -> bool:
        with self._transaction() as db:
            try:
                receipt = WaveReceipt.from_bytes(payload)
            except ContractError:
                self._event(db, 'receipt_invalid')
                return False
            row = db.execute('SELECT * FROM sender_capture WHERE gateway_id=? AND device_id=? AND capture_id=?',
                (receipt.gateway_id, receipt.device_id, str(receipt.capture_id))).fetchone()
            if row is None:
                self._event(db, 'receipt_unknown')
                return False
            key = CaptureKey(row['gateway_id'], row['device_id'], UUID(row['capture_id']))
            message = None
            if receipt.scope == 'message':
                message = db.execute('SELECT * FROM sender_message WHERE queue_id=? AND payload_sha256=?',
                    (row['queue_id'], receipt.payload_sha256)).fetchone()
                if message is None:
                    self._event(db, 'receipt_mismatch')
                    return False
            expected = ReceiptExpectation(self.topic_prefix, key, receipt.scope, row['manifest_sha256'],
                message['payload_sha256'] if message else None, message['stream_id'] if message else None,
                message['chunk_index'] if message else None)
            if not receipt.matches(topic, expected=expected) or row['state'] == 'local_corrupt':
                self._event(db, 'receipt_mismatch')
                return False
            target = message if message else row
            if target['receipt'] is not None:
                previous = WaveReceipt.from_bytes(target['receipt'])
                if previous != receipt:
                    self._event(db, 'receipt_conflict')
                    return False
                return True
            if message:
                if row['receipt'] is not None and receipt.status == 'quarantined':
                    self._event(db, 'receipt_conflict')
                    return False
                db.execute('UPDATE sender_message SET state=?,receipt=? WHERE queue_id=? AND ordinal=?',
                    (receipt.status, payload, row['queue_id'], message['ordinal']))
                if receipt.status == 'quarantined':
                    db.execute("UPDATE sender_capture SET state='failed' WHERE queue_id=?", (row['queue_id'],))
            else:
                if row['state'] == 'failed' and receipt.status == 'complete':
                    self._event(db, 'receipt_conflict')
                    return False
                db.execute('UPDATE sender_capture SET state=?,receipt=? WHERE queue_id=?',
                    ('archived' if receipt.status == 'complete' else 'failed', payload, row['queue_id']))
            return True

    def prepare_batch(self, *, limit=16):
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('batch limit must be 1..16')
        with self._transaction() as db:
            rows = db.execute("SELECT m.* FROM sender_message m JOIN sender_capture c USING(queue_id) "
                "WHERE c.state='awaiting_capture_receipt' AND (m.state='pending' OR (m.ordinal=0 AND NOT EXISTS "
                "(SELECT 1 FROM sender_message x WHERE x.queue_id=m.queue_id AND x.state='pending'))) "
                "ORDER BY m.attempts,c.rowid,m.ordinal LIMIT ?", (limit,)).fetchall()
            captures, messages = {}, []
            for row in rows:
                identifier = row['queue_id']
                if identifier not in captures:
                    capture = db.execute('SELECT * FROM sender_capture WHERE queue_id=?', (identifier,)).fetchone()
                    try:
                        scanned = scan_file(BytesIO(capture['content']))
                        metadata = db.execute('SELECT ordinal,offset,length,payload_sha256,stream_id,chunk_index FROM sender_message WHERE queue_id=? ORDER BY ordinal', (identifier,)).fetchall()
                        expected = []
                        for index, (line, raw) in enumerate(zip(scanned.lines, iter_file_frames(BytesIO(capture['content'])))):
                            frame = parse_frame(raw)
                            expected.append((index, line.offset, line.length, line.sha256,
                                frame.stream_id if isinstance(frame, ChunkFrame) else None,
                                frame.chunk_index if isinstance(frame, ChunkFrame) else None))
                        if (scanned.sha256 != capture['file_sha256'] or scanned.manifest_sha256 != capture['manifest_sha256']
                                or scanned.manifest.key != CaptureKey(capture['gateway_id'], capture['device_id'], UUID(capture['capture_id']))
                                or [tuple(m) for m in metadata] != expected):
                            raise ContractError('digest_mismatch')
                        captures[identifier] = capture
                    except (ContractError, ValueError):
                        db.execute("UPDATE sender_capture SET state='local_corrupt' WHERE queue_id=?", (identifier,))
                        self._event(db, 'local_content_corrupt')
                        captures[identifier] = None
                capture = captures[identifier]
                if capture is None:
                    continue
                payload = capture['content'][row['offset']:row['offset'] + row['length']]
                messages.append(SenderMessage(identifier, row['ordinal'],
                    f"{self.topic_prefix}/v2/{capture['gateway_id']}/{capture['device_id']}/waveform", payload, row['payload_sha256']))
                db.execute('UPDATE sender_message SET attempts=min(attempts+1,2147483647) WHERE queue_id=? AND ordinal=?',
                    (identifier, row['ordinal']))
        return tuple(messages)

    def record_broker_ack(self, message, reason):
        if type(message) is not SenderMessage or reason not in (0, 16):
            return False
        with self._transaction() as db:
            return db.execute('UPDATE sender_message SET broker_accepted=1 WHERE queue_id=? AND ordinal=? AND payload_sha256=?',
                (message.queue_id, message.ordinal, message.sha256)).rowcount == 1

    def capture_state(self, key: CaptureKey) -> str:
        with self._transaction() as db:
            row = db.execute('SELECT state FROM sender_capture WHERE gateway_id=? AND device_id=? AND capture_id=?',
                (key.gateway_id, key.device_id, str(key.capture_id))).fetchone()
            return row['state'] if row else 'unknown'

    def status(self) -> dict:
        with self._transaction() as db:
            count, retained, charged = db.execute('SELECT count(*),coalesce(sum(length(content)),0),coalesce(sum(charge),0) FROM sender_capture').fetchone()
            return {'captures': count, 'retained_capture_bytes': retained, 'charged_bytes': charged,
                    'max_bytes': self.max_bytes, 'states': dict(db.execute('SELECT state,count(*) FROM sender_capture GROUP BY state')),
                    'diagnostic_events': db.execute('SELECT count(*) FROM sender_event').fetchone()[0]}

    def close(self):
        with self._lock:
            try:
                if self._db is not None:
                    self._db.close()
                    self._db = None
            finally:
                if self._owner is not None:
                    self._owner.close()
                    self._owner = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
