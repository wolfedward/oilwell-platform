"""Explicit offline queue initialization and migration."""
import argparse
from contextlib import closing
from hashlib import sha256
from itertools import zip_longest
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

from backend.ingest_reliability.errors import ReliabilityError
from backend.lifecycle import InstanceLock, AlreadyRunningError


SQL_PATH = Path(__file__).with_name('sql') / '002_reliable_spool.sql'
LEGACY_COLUMNS = ('message_id', 'topic', 'payload', 'qos', 'state', 'attempts', 'next_attempt_at',
                  'claimed_at', 'created_at', 'acked_at', 'last_error')


def _error(code):
    return ReliabilityError(code, '队列操作未确认完成，原始记录保留；请检查版本、容量、备份与独占状态')


def _path(path, *, allow_missing_parent=False):
    result = Path(path).resolve()
    parent = result.parent
    if allow_missing_parent and not parent.exists():
        parent = next((candidate for candidate in result.parents if candidate.exists()), None)
    if str(result).startswith('\\\\') or parent is None or not parent.is_dir():
        raise _error('ingest.spool_path_invalid')
    if result.exists() and (not result.is_file() or result.stat().st_nlink != 1):
        raise _error('ingest.spool_path_invalid')
    return result


def acquire_spool_lock(path):
    """All supported old/new queue runtimes share this OS-released lock."""
    path = _path(path)
    lock = InstanceLock(path.parent / (path.name + '.owner'))
    try:
        return lock.acquire()
    except (OSError, AlreadyRunningError):
        raise _error('ingest.spool_in_use') from None


def _connect(path, *, readonly=False):
    return sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True,
                           timeout=1, isolation_level=None, check_same_thread=False)


def _limits(limits):
    from .reliable_spool import SpoolLimits
    if not isinstance(limits, SpoolLimits):
        raise _error('ingest.spool_limits_invalid')
    return limits.max_messages, limits.max_payload_bytes, limits.min_free_bytes


def _schema_hash(connection):
    rows = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    return sha256(json.dumps([tuple(row) for row in rows], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _source():
    return SQL_PATH.read_text(encoding='utf-8')


def _execute_schema(connection, *, defer_legacy_view=False):
    # executescript implicitly commits in sqlite3 legacy mode. Keep every DDL
    # statement within the caller's explicit migration transaction instead.
    statement = ''
    deferred = []
    for line in _source().splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if defer_legacy_view and statement.lstrip().startswith('CREATE VIEW uplink_spool AS'):
                deferred.append(statement)
            else:
                connection.execute(statement)
            statement = ''
    if statement.strip():
        raise _error('ingest.spool_schema_invalid')
    return deferred


def _finish_schema(connection, limits, count, payload_bytes):
    connection.execute('INSERT INTO spool_capacity(singleton,max_messages,max_payload_bytes,min_free_bytes,'
                       'used_messages,used_payload_bytes,last_clock_seen,last_clock_warning) VALUES(1,?,?,?,?,?,?,NULL)',
                       (*_limits(limits), count, payload_bytes, time.time()))
    connection.execute('INSERT INTO spool_metadata VALUES(1,2,?,?,?)',
                       (sha256(_source().encode()).hexdigest(), _schema_hash(connection), time.time()))
    connection.execute('PRAGMA user_version=2')


def _verify_runtime(connection, limits):
    if connection.execute('PRAGMA user_version').fetchone()[0] != 2:
        raise _error('ingest.spool_migration_required')
    row = connection.execute('SELECT version,migration_sha256,schema_sha256 FROM spool_metadata WHERE singleton=1').fetchone()
    if row is None or tuple(row) != (2, sha256(_source().encode()).hexdigest(), _schema_hash(connection)):
        raise _error('ingest.spool_schema_invalid')
    capacity = connection.execute('SELECT max_messages,max_payload_bytes,min_free_bytes,used_messages,used_payload_bytes '
                                  'FROM spool_capacity WHERE singleton=1').fetchone()
    if capacity is None or tuple(capacity[:3]) != _limits(limits):
        raise _error('ingest.spool_limits_mismatch')
    actual = connection.execute('SELECT count(*),coalesce(sum(length(payload)),0) FROM uplink_queue_v2').fetchone()
    if tuple(actual) != tuple(capacity[3:]):
        raise _error('ingest.spool_capacity_corrupt')


def _legacy_rows(connection):
    return connection.execute('SELECT ' + ','.join(LEGACY_COLUMNS) + ' FROM uplink_spool ORDER BY message_id')


def _check_legacy(connection):
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    tables = connection.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view','trigger')").fetchall()
    columns = connection.execute('PRAGMA table_info(uplink_spool)').fetchall()
    if version not in (0, 1) or tables != [('table', 'uplink_spool')] or tuple(row[1] for row in columns) != LEGACY_COLUMNS:
        raise _error('ingest.spool_legacy_schema_invalid')
    if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise _error('ingest.spool_integrity_failed')


def _space(path, reserve, extra=0):
    if shutil.disk_usage(path.parent).free < reserve + extra:
        raise _error('ingest.spool_disk_low')


def _sidecars(path):
    return tuple(Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))


def _backup(path, backup_path, locked_source):
    # The separate read connection sees the committed WAL snapshot while the
    # migration connection's BEGIN IMMEDIATE excludes competing writers.
    with backup_path.open('xb'):
        pass
    deadline = time.monotonic() + 60
    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise _error('ingest.spool_backup_timeout')
    with closing(_connect(path, readonly=True)) as source, closing(_connect(backup_path)) as backup:
        source.backup(backup, pages=256, progress=progress, sleep=.05)
        if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise _error('ingest.spool_backup_failed')
        for original, copied in zip_longest(_legacy_rows(locked_source), _legacy_rows(backup)):
            if original != copied:
                raise _error('ingest.spool_backup_failed')


def _legacy_values(row):
    identifier, topic, payload, qos, state, attempts, next_at, claimed, created, acked, error = row
    from .reliable_spool import _instant
    if (not isinstance(identifier, str) or not identifier or not isinstance(topic, str) or not topic
            or type(payload) is not bytes or qos not in (1, 2) or state not in ('pending', 'inflight', 'acked', 'dead')
            or type(attempts) is not int or attempts < 0):
        raise _error('ingest.spool_legacy_row_invalid')
    for timestamp in (next_at, created, claimed, acked):
        if timestamp is not None:
            _instant(timestamp)
    mapped = {'acked': 'legacy_broker_confirmed', 'inflight': 'pending'}.get(state, state)
    return (identifier, topic, payload, sha256(payload).hexdigest(), qos, 'broker', mapped, attempts,
            next_at, created, state, error, claimed, acked)


def migrate_spool(path, *, backup_path, limits):
    _limits(limits)
    ownership = None
    try:
        path, backup_path = _path(path), _path(backup_path)
        if (not path.is_file() or backup_path.exists() or backup_path == path or backup_path in _sidecars(path)
                or any(sidecar.exists() for sidecar in _sidecars(backup_path))):
            raise _error('ingest.spool_backup_path_invalid')
        ownership = acquire_spool_lock(path)
        with closing(_connect(path)) as connection:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE')
            try:
                _check_legacy(connection)
                size = connection.execute('PRAGMA page_count').fetchone()[0] * connection.execute('PRAGMA page_size').fetchone()[0]
                _space(path, limits.min_free_bytes, 3 * size)
                _space(backup_path, limits.min_free_bytes, size)
                _backup(path, backup_path, connection)
                # Validate the source before dropping anything; copy verification
                # below is in the same transaction as the replacement schema.
                count, payload_bytes = connection.execute('SELECT count(*),coalesce(sum(length(payload)),0) FROM uplink_spool').fetchone()
                if count > limits.max_messages or payload_bytes > limits.max_payload_bytes:
                    raise _error('ingest.spool_capacity_exhausted')
                views = _execute_schema(connection, defer_legacy_view=True)
                columns = 'message_id,topic,payload,payload_sha256,qos,confirmation_mode,state,attempts,next_attempt_at,created_at,legacy_state,legacy_error,legacy_claimed_at,legacy_acked_at'
                source = _legacy_rows(connection)
                for row in source:
                    expected = _legacy_values(row)
                    connection.execute('INSERT INTO uplink_queue_v2(' + columns + ') VALUES(' + ','.join('?' * 14) + ')', expected)
                    copied = connection.execute('SELECT ' + columns + ' FROM uplink_queue_v2 WHERE message_id=?', (row[0],)).fetchone()
                    if copied != expected:
                        raise _error('ingest.spool_copy_failed')
                if connection.execute('SELECT count(*) FROM uplink_queue_v2').fetchone()[0] != count:
                    raise _error('ingest.spool_copy_failed')
                connection.execute('DROP TABLE uplink_spool')
                for statement in views:
                    connection.execute(statement)
                _finish_schema(connection, limits, count, payload_bytes)
                if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise _error('ingest.spool_integrity_failed')
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            connection.execute('PRAGMA journal_mode=WAL')
        return {'version': 2, 'migrated_messages': count, 'backup': str(backup_path)}
    except (OSError, sqlite3.Error):
        raise _error('ingest.spool_migration_failed') from None
    finally:
        if ownership is not None:
            ownership.release()


def initialize_spool(path, *, limits):
    _limits(limits)
    ownership = None
    try:
        path = _path(path)
        ownership = acquire_spool_lock(path)
        if any(sidecar.exists() for sidecar in _sidecars(path)):
            raise _error('ingest.spool_recovery_required')
        _space(path, limits.min_free_bytes, 1024 * 1024)
        with path.open('xb'):
            pass
        with closing(_connect(path)) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
            try:
                _execute_schema(connection)
                _finish_schema(connection, limits, 0, 0)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {'version': 2, 'initialized': True}
    except (OSError, sqlite3.Error):
        raise _error('ingest.spool_initialization_failed') from None
    finally:
        if ownership is not None:
            ownership.release()


def read_spool_version(path):
    try:
        # Inspection must not create anything. Legacy broker initialization may
        # create a safe missing directory later; application mode still refuses.
        path = _path(path, allow_missing_parent=True)
        if not path.exists():
            return 0
        with closing(_connect(path, readonly=True)) as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 0 and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='uplink_spool'").fetchone():
                return 1
            return version
    except (OSError, sqlite3.Error):
        raise _error('ingest.spool_unavailable') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Explicit offline gateway queue migration; never overwrites backups')
    parser.add_argument('--database', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--backup')
    mode.add_argument('--initialize', action='store_true')
    parser.add_argument('--max-messages', required=True, type=int)
    parser.add_argument('--max-payload-bytes', required=True, type=int)
    parser.add_argument('--min-free-bytes', required=True, type=int)
    args = parser.parse_args(argv)
    try:
        from .reliable_spool import SpoolLimits
        limits = SpoolLimits(args.max_messages, args.max_payload_bytes, args.min_free_bytes)
        result = initialize_spool(args.database, limits=limits) if args.initialize else migrate_spool(
            args.database, backup_path=args.backup, limits=limits)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except ReliabilityError as error:
        print(json.dumps({'code': error.code}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
