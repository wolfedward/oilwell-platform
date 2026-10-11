"""Explicit no-seed identity initialization and read-only review startup gate.

This is a single-host engineering review deployment, not a field controller.
Secrets are returned once to the local initializer, never logged here.
"""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import secrets
import sqlite3
import uuid

from backend.auth import password_material
from backend.database import Database
from backend.seed import ROLE_PERMISSIONS
from backend.studies.permissions import ALL as STUDY_PERMISSIONS
from backend.studies.storage import migrate_studies

RUNTIME_VERSION = 1
ROLE_NAMES = {
    'administrator': '系统管理员', 'production_engineer': '生产工程师',
    'algorithm_engineer': '算法工程师', 'maintenance_engineer': '设备运维',
    'reviewer': '业务复核员',
}
STUDY_ROLES = {
    'administrator': STUDY_PERMISSIONS,
    'production_engineer': ('studies.read', 'studies.create', 'studies.export'),
    'algorithm_engineer': ('studies.read', 'studies.create', 'studies.select', 'studies.export'),
    'maintenance_engineer': ('studies.read', 'studies.create', 'studies.export'),
    'reviewer': ('studies.read', 'studies.review', 'studies.select', 'studies.export'),
}


def _regular_path(path, *, existing):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Review identity path must not traverse a link')
    if existing:
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError('Explicitly initialized regular review identity is required')
    elif path.exists():
        raise ValueError('Review initialization refuses any existing identity file')
    return path


def _source_hashes():
    base = Path(__file__).with_name('schema.sql').read_bytes()
    studies = Path(__file__).with_name('studies').joinpath('schema.sql').read_bytes()
    return hashlib.sha256(base).hexdigest(), hashlib.sha256(studies).hexdigest()


def _structure_digest(connection):
    rows = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    return hashlib.sha256(json.dumps([list(row) for row in rows], ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def initialize_identity(path):
    """Create a NEW identity store only; failures retain the partial file."""
    path = _regular_path(path, existing=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic exclusive claim prevents accidentally adopting an existing store.
    with path.open('xb'):
        pass
    database = Database(path)
    database.migrate()
    migrate_studies(database)
    account_rows = []
    for code in ROLE_NAMES:
        password = secrets.token_urlsafe(24)
        account_rows.append((code, password, password_material(password)))
    instance_id = str(uuid.uuid4())
    permissions = {code: set(ROLE_PERMISSIONS[code]) | set(STUDY_ROLES[code]) for code in ROLE_NAMES}
    permissions['administrator'] = set().union(*permissions.values())
    with database.transaction(None, 'review.initialize', 'runtime', instance_id, immediate=True) as conn:
        conn.execute('''CREATE TABLE review_runtime_meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL,
            instance_id TEXT NOT NULL, structure_sha256 TEXT NOT NULL,
            base_source_sha256 TEXT NOT NULL, studies_source_sha256 TEXT NOT NULL)''')
        for permission in sorted(permissions['administrator']):
            conn.execute('INSERT INTO permissions(code,name) VALUES(?,?)', (permission, permission))
        for code, password, material in account_rows:
            role_id = conn.execute('INSERT INTO roles(code,name) VALUES(?,?)', (code, ROLE_NAMES[code])).lastrowid
            salt, digest, iterations = material
            conn.execute('''INSERT INTO users(role_id,username,display_name,password_salt,password_hash,password_iterations)
                VALUES(?,?,?,?,?,?)''', (role_id, code, ROLE_NAMES[code], salt, digest, iterations))
            for permission in sorted(permissions[code]):
                conn.execute('INSERT INTO role_permissions(role_id,permission_id) SELECT ?,id FROM permissions WHERE code=?',
                             (role_id, permission))
        conn.execute('INSERT INTO review_runtime_meta VALUES(1,?,?,?,?,?)',
                     (RUNTIME_VERSION, instance_id, _structure_digest(conn), *_source_hashes()))
    check_identity(path)
    return {'instance_id': instance_id, 'accounts': [
        {'username': code, 'display_name': ROLE_NAMES[code], 'password': password}
        for code, password, _ in account_rows]}


def check_identity(path):
    """No mkdir, schema changes, grants, or demo generation on startup."""
    path = _regular_path(path, existing=True)
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            row = conn.execute('SELECT version,instance_id,structure_sha256,base_source_sha256,studies_source_sha256 FROM review_runtime_meta WHERE singleton=1').fetchone()
            if not row or row[0] != RUNTIME_VERSION or tuple(row[3:]) != _source_hashes():
                raise ValueError('Review identity needs an explicit reviewed migration')
            if _structure_digest(conn) != row[2]:
                raise ValueError('Review identity schema protections differ')
            if conn.execute('PRAGMA quick_check').fetchone() != ('ok',) or conn.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('Review identity integrity check failed')
            if conn.execute('SELECT COUNT(*) FROM seed_manifests').fetchone()[0]:
                raise ValueError('Demo seed identity cannot be a review deployment')
            if conn.execute('SELECT version FROM study_schema_version').fetchall() != [(1,)]:
                raise ValueError('Engineering study schema is unavailable')
            return {'version': row[0], 'instance_id': row[1]}
    except (sqlite3.Error, OSError) as error:
        raise ValueError('Explicitly initialized review identity is unavailable') from error


def reject_review_identity_in_demo(path):
    """A missing mode flag must not seed or migrate an existing review store."""
    path = Path(path).absolute()
    if path.is_file():
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='review_runtime_meta'").fetchone():
                raise ValueError('Review identity requires explicit runtime_mode=review')


def study_engines():
    """Explicit versioned, in-process numeric functions; no dynamic plugin loading."""
    from backend.studies.condition_models import train_conditions, predict_conditions
    from backend.studies.recovery import fit_recovery
    from backend.studies.energy_schedule import optimize_schedule
    from backend.studies.energy_effects import evaluate_effects
    from backend.studies.spectrum import analyze_spectrum
    from backend.studies.protection import replay_protection
    return {
        'condition_training': ('condition_bp/1', train_conditions),
        'condition_prediction': ('condition_bp/1', predict_conditions),
        'recovery_fit': ('recovery_depth/1', fit_recovery),
        'energy_schedule': ('shadow_schedule/1', optimize_schedule),
        'energy_effects': ('energy_effects/1', evaluate_effects),
        'motor_spectrum': ('motor_spectrum/1', analyze_spectrum),
        'protection_replay': ('protection_replay/1', replay_protection),
    }
