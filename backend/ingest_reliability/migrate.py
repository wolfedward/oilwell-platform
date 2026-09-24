"""Explicit same-database migration; runtime startup only calls read-only checks."""

import hashlib
import json
from pathlib import Path

import psycopg
from psycopg import sql

from .contracts import CapacityLimits
from .errors import ReliabilityError


SQL_DIR = Path(__file__).with_name("sql")
MIGRATIONS = ("001_reliability.sql", "002_repeat_reopen.sql")
VERSION = len(MIGRATIONS)
MIGRATION_LOCK = 499001211
ROLES = {"ingestor", "publisher", "reader", "operator"}


def _source(version=1):
    source = (SQL_DIR / MIGRATIONS[version - 1]).read_text(encoding="utf-8")
    return source, hashlib.sha256(source.encode("utf-8")).hexdigest()


def _target(connection, expected_database):
    if not isinstance(expected_database, str) or not expected_database:
        raise ReliabilityError("ingest.migration_target", "必须明确指定目标数据库")
    if connection.execute("SELECT current_database()").fetchone()[0] != expected_database:
        raise ReliabilityError("ingest.migration_target", "实际数据库与指定目标不一致")


def structure_digest(connection):
    """Fingerprint actual protection definitions, not just their names or OIDs."""
    queries = (
        "SELECT c.relname,c.relkind,c.relrowsecurity,c.relforcerowsecurity,c.reloptions "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='ingest_reliability' ORDER BY c.relname",
        "SELECT c.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,"
        "pg_get_expr(d.adbin,d.adrelid,true) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
        "WHERE n.nspname='ingest_reliability' AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum",
        "SELECT c.relname,k.conname,k.contype,k.convalidated,k.conenforced,k.condeferrable,k.condeferred,"
        "pg_get_constraintdef(k.oid,true) FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='ingest_reliability' ORDER BY c.relname,k.conname",
        "SELECT c.relname,t.tgname,t.tgenabled,t.tgtype,pg_get_triggerdef(t.oid,true) FROM pg_trigger t "
        "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='ingest_reliability' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname",
        "SELECT c.relname,i.indisvalid,i.indisready,i.indislive,pg_get_indexdef(i.indexrelid) FROM pg_index i "
        "JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='ingest_reliability' ORDER BY c.relname",
        "SELECT c.relname,pg_get_viewdef(c.oid,true) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='ingest_reliability' AND c.relkind='v' ORDER BY c.relname",
        "SELECT p.proname,pg_get_function_identity_arguments(p.oid),pg_get_functiondef(p.oid) "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='ingest_reliability' "
        "ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)",
    )
    parts = [connection.execute(query).fetchall() for query in queries]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _check_history(connection, *, expected_database, require_current):
    _target(connection, expected_database)
    connection.execute("SET LOCAL search_path=pg_catalog,public,pg_temp")
    if connection.execute("SELECT to_regclass('ingest_reliability.schema_migrations')").fetchone()[0] is None:
        raise ReliabilityError("ingest.schema_missing", "接收可靠性结构未显式初始化")
    rows = connection.execute("SELECT version,sha256,structure_sha256 FROM ingest_reliability.schema_migrations ORDER BY version").fetchall()
    if not rows or len(rows) > VERSION or (require_current and len(rows) != VERSION):
        raise ReliabilityError("ingest.schema_mismatch", "接收可靠性迁移版本或摘要不匹配")
    for version, row in enumerate(rows, 1):
        if row[:2] != (version, _source(version)[1]):
            raise ReliabilityError("ingest.schema_mismatch", "接收可靠性迁移版本或摘要不匹配")
    if structure_digest(connection) != rows[-1][2]:
        raise ReliabilityError("ingest.schema_incomplete", "接收可靠性结构或保护约束发生变化")
    return {"version": str(len(rows)), "sha256": rows[-1][1], "applied": False}


def check_schema(connection, *, expected_database):
    return _check_history(connection, expected_database=expected_database, require_current=True)


def migrate(settings: dict, *, expected_database: str, limits: CapacityLimits) -> dict:
    try:
        with psycopg.connect(**(settings | {"connect_timeout": 3})) as conn:
            _target(conn, expected_database)
            conn.execute("SET LOCAL lock_timeout='1s'")
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL search_path=pg_catalog,public,pg_temp")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            if conn.execute("SELECT to_regnamespace('ingest_reliability')").fetchone()[0] is not None:
                result = _check_history(conn, expected_database=expected_database, require_current=False)
                applied_version = int(result["version"])
                existing = conn.execute("SELECT total_bytes,quarantine_bytes FROM ingest_reliability.capacity WHERE singleton=1").fetchone()
                if existing != (limits.total_bytes, limits.quarantine_bytes):
                    raise ReliabilityError("ingest.capacity_mismatch", "已有容量设置不一致，拒绝自动覆盖")
            else:
                applied_version = 0
            for version in range(applied_version + 1, VERSION + 1):
                source, digest = _source(version)
                conn.execute(source)
                if version == 1:
                    conn.execute("INSERT INTO ingest_reliability.capacity(singleton,total_bytes,quarantine_bytes) VALUES (1,%s,%s)",
                                 (limits.total_bytes, limits.quarantine_bytes))
                fingerprint = structure_digest(conn)
                conn.execute("INSERT INTO ingest_reliability.schema_migrations(version,sha256,structure_sha256) VALUES (%s,%s,%s)",
                             (version, digest, fingerprint))
                result = {"version": str(version), "sha256": digest, "applied": True}
        return result
    except psycopg.Error as error:
        raise ReliabilityError("ingest.migration_failed", "接收可靠性迁移失败，请核对独立迁移身份与数据库日志") from error


def grant_roles(connection, *, roles: dict[str, str]) -> None:
    if (set(roles) != ROLES or any(not isinstance(value, str) or not value for value in roles.values())
            or len(set(roles.values())) != 4):
        raise ReliabilityError("ingest.invalid_roles", "必须提供四个不同的运行身份")
    for name in roles.values():
        row = connection.execute("SELECT oid,rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,"
                                 "rolcanlogin FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
        if row is None or row[1] or not row[2]:
            raise ReliabilityError("ingest.invalid_roles", "运行身份不存在或持有管理权限")
        owner = connection.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='ingest_reliability' AND nspowner=%s) "
                                   "OR EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=%s)",
                                   (row[0], row[0])).fetchone()[0]
        if owner:
            raise ReliabilityError("ingest.invalid_roles", "迁移所有者不得兼任运行身份")
    statement = sql.SQL((SQL_DIR / "grants.sql").read_text(encoding="utf-8"))
    connection.execute(statement.format(**{role: sql.Identifier(name) for role, name in roles.items()}))
