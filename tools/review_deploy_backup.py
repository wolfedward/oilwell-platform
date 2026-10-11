"""Quiescent SQLite/PostgreSQL snapshots and a sealed restore manifest."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import uuid
import psycopg
from psycopg import sql

from .review_deploy_config import DATABASE, PG_OWNERS, PG_RUNTIME
from .review_deploy_paths import private_acl, write_json_private
from .review_deploy_pg import PG_BIN


FILES = ('database.dump', 'identity.sqlite', 'accounts.json')


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def seal_backup(folder, *, instance_id, source, system_id, source_ref, row_counts=None):
    folder = Path(folder).absolute()
    uuid.UUID(instance_id)
    uuid.UUID(source_ref)
    if not system_id.isdigit() or not source['commit'] or not source['files']:
        raise ValueError('incomplete backup identity')
    digests = {}
    for name in FILES:
        path = folder / name
        if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
            raise ValueError('backup payload is missing or linked')
        digests[name] = {'sha256': _sha256(path), 'bytes': path.stat().st_size}
    manifest = {'format': 1, 'instance_id': instance_id, 'system_id': system_id,
                'source': source, 'source_ref': source_ref,
                'roles': ['review_' + name for name in (*PG_OWNERS, *PG_RUNTIME)],
                'capacities': {'reliability_total_bytes': 1073741824,
                               'waveform_global_raw_bytes': 1073741824,
                               'inversion_total_storage_bytes': 2147483648},
                'row_counts': row_counts if row_counts is not None else {},
                'files': digests}
    write_json_private(folder / 'manifest.json', manifest)
    return manifest


def verify_backup(folder):
    folder = Path(folder).absolute()
    marker = folder / 'manifest.json'
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_nlink != 1:
        raise ValueError('backup manifest missing or linked')
    try:
        manifest = json.loads(marker.read_text(encoding='utf-8'))
        uuid.UUID(manifest['instance_id'])
        uuid.UUID(manifest['source_ref'])
        if manifest['format'] != 1 or set(manifest['files']) != set(FILES) or not manifest['system_id'].isdigit():
            raise ValueError()
        if not isinstance(manifest['row_counts'], dict) or any(type(value) is not int or value < 0
                                                               for value in manifest['row_counts'].values()):
            raise ValueError()
        for name in FILES:
            path = folder / name
            recorded = manifest['files'][name]
            if (path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or
                    path.stat().st_size != recorded['bytes'] or _sha256(path) != recorded['sha256']):
                raise ValueError()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError('backup integrity mismatch') from error
    return manifest


def count_business_rows(*, port, password):
    """Exact counts from a read-only transaction, excluding child partitions."""
    from .review_deploy_pg import connection_settings
    with psycopg.connect(**connection_settings(port, 'bootstrap', password)) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        tables = conn.execute("SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                              "WHERE n.nspname IN ('public','device_registry','ingest_reliability','waveforms','inversion') "
                              "AND c.relkind IN ('r','p') AND NOT EXISTS "
                              "(SELECT 1 FROM pg_inherits i WHERE i.inhrelid=c.oid) ORDER BY n.nspname,c.relname").fetchall()
        counts = {}
        for schema, table in tables:
            counts[schema + '.' + table] = conn.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(
                sql.Identifier(schema), sql.Identifier(table))).fetchone()[0]
        return counts


def snapshot_identity(source, target):
    with sqlite3.connect(f'file:{Path(source).as_posix()}?mode=ro', uri=True) as live:
        with sqlite3.connect(target) as copy:
            live.backup(copy)
            if copy.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise RuntimeError('identity snapshot integrity check failed')
    private_acl(target)


def dump_database(folder, *, port, password):
    target = Path(folder) / 'database.dump'
    if target.exists():
        raise ValueError('database snapshot exists')
    env = os.environ.copy()
    env['PGPASSWORD'] = password
    command = [str(PG_BIN / 'pg_dump.exe'), '--format=custom', '--no-password', '--host=127.0.0.1',
               '--port=' + str(port), '--username=review_bootstrap', '--file=' + str(target), DATABASE]
    result = subprocess.run(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=600, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('PostgreSQL dump failed; backup retained for inspection')
    private_acl(target)


def restore_database(folder, *, port, password):
    manifest = verify_backup(folder)
    env = os.environ.copy()
    env['PGPASSWORD'] = password
    command = [str(PG_BIN / 'pg_restore.exe'), '--exit-on-error', '--no-password',
               '--host=127.0.0.1', '--port=' + str(port), '--username=review_bootstrap',
               '--dbname=' + DATABASE, str(Path(folder) / 'database.dump')]
    result = subprocess.run(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=600, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('PostgreSQL restore failed; target retained for inspection')
    return manifest
