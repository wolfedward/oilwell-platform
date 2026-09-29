"""Explicit isolated migration. Runtime callers only use read-only check_schema."""
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import psycopg
from psycopg import sql
from .config import CapacityLimits, Quota
from .errors import WaveformError

SQL_DIR = Path(__file__).with_name("sql")
MIGRATION_LOCK = 499001212
ROLES = {"migration_owner", "receiver", "assembler", "receipt_publisher", "reader", "operator"}
RUNTIME_ROLES = ROLES - {"migration_owner"}


def _fail(code, message): raise WaveformError("waveform."+code, message)


def _target(conn, expected_database):
    if not isinstance(expected_database, str) or not expected_database:
        _fail("migration_target", "An explicit target database is required")
    if conn.execute("SELECT current_database()").fetchone()[0] != expected_database:
        _fail("migration_target", "Connected database differs from explicit migration target")


def _source():
    source = (SQL_DIR / "001_capture.sql").read_text(encoding="utf-8")
    return source, hashlib.sha256(source.encode()).hexdigest()


def structure_digest(conn):
    """Fingerprint definitions; ACL/owners and internal triggers are probed separately."""
    queries = (
        "SELECT c.relname,c.relkind,c.relrowsecurity,c.relforcerowsecurity,c.reloptions FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' ORDER BY c.relname",
        "SELECT c.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid,true) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE n.nspname='waveforms' AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum",
        "SELECT c.relname,k.conname,k.contype,k.convalidated,k.conenforced,k.condeferrable,k.condeferred,pg_get_constraintdef(k.oid,true) FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' ORDER BY c.relname,k.conname",
        "SELECT c.relname,t.tgname,t.tgenabled,t.tgtype,pg_get_triggerdef(t.oid,true) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname",
        "SELECT c.relname,i.indisvalid,i.indisready,i.indislive,pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' ORDER BY c.relname,i.indexrelid",
        "SELECT c.relname,pg_get_viewdef(c.oid,true) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' AND c.relkind='v' ORDER BY c.relname",
        "SELECT p.proname,pg_get_function_identity_arguments(p.oid),pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='waveforms' ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)",
    )
    parts = [conn.execute(query).fetchall() for query in queries]
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def check_schema(conn, *, expected_database):
    _target(conn, expected_database)
    if conn.execute("SELECT to_regclass('waveforms.schema_migrations')").fetchone()[0] is None:
        _fail("schema_missing", "Waveform schema has not been explicitly initialized")
    rows = conn.execute("SELECT version,sha256,structure_sha256 FROM waveforms.schema_migrations ORDER BY version").fetchall()
    if len(rows)!=1 or rows[0][:2] != (1,_source()[1]): _fail("schema_mismatch", "Waveform migration history differs")
    if rows[0][2] != structure_digest(conn): _fail("schema_incomplete", "Waveform schema protections differ")
    return dict(version="1", sha256=rows[0][1], applied=False)


def _capacity_rows(limits):
    return [(bucket, *(getattr(quota, field.name) for field in fields(Quota)))
            for bucket,quota in sorted({"*": limits.global_quota, **limits.gateways}.items())]


def migrate(settings, *, expected_database: str, limits: CapacityLimits) -> dict:
    if type(limits) is not CapacityLimits: _fail("capacity_missing", "Explicit validated capacities are required")
    try:
        with psycopg.connect(**(settings | {"connect_timeout": 3})) as conn:
            _target(conn, expected_database)  # Must be the first database statement.
            conn.execute("SET LOCAL lock_timeout='1s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            conn.execute("SET LOCAL search_path=pg_catalog,pg_temp")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            columns = sql.SQL(",").join(map(sql.Identifier, ("bucket", *(field.name for field in fields(Quota)))))
            if conn.execute("SELECT to_regnamespace('waveforms')").fetchone()[0] is not None:
                result = check_schema(conn, expected_database=expected_database)
                existing = conn.execute(sql.SQL("SELECT {} FROM waveforms.capacity ORDER BY bucket").format(columns)).fetchall()
                if existing != _capacity_rows(limits): _fail("capacity_mismatch", "Existing capacity configuration differs; no automatic overwrite")
            else:
                source,digest = _source()
                conn.execute(source)
                for row in _capacity_rows(limits):
                    conn.execute(sql.SQL("INSERT INTO waveforms.capacity({}) VALUES ({})").format(columns,sql.SQL(",").join(sql.Placeholder() for _ in row)),row)
                conn.execute("INSERT INTO waveforms.schema_migrations(version,sha256,structure_sha256) VALUES(1,%s,%s)", (digest,structure_digest(conn)))
                result = dict(version="1",sha256=digest,applied=True)
        return result
    except psycopg.Error as error:
        raise WaveformError("waveform.migration_failed", "Waveform migration failed; inspect isolated database diagnostics") from error


def grant_roles(connection, *, roles: dict) -> None:
    if set(roles)!=ROLES or len(set(roles.values()))!=6 or any(not isinstance(name,str) or not name for name in roles.values()):
        _fail("invalid_roles", "Six distinct explicit waveform identities are required")
    owner = connection.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='waveforms'").fetchone()
    if not owner or owner[0] != roles["migration_owner"]: _fail("invalid_roles", "Migration owner must own waveform schema")
    for role,name in roles.items():
        row = connection.execute("SELECT oid,rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
        if row is None or row[1] or not row[2]: _fail("invalid_roles", "Waveform identity is absent or elevated")
        if role != "migration_owner":
            if connection.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='waveforms' AND nspowner=%s) OR EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=%s)", (row[0],row[0])).fetchone()[0]:
                _fail("invalid_roles", "Runtime must not own schema or database")
    connection.execute(sql.SQL((SQL_DIR/"grants.sql").read_text(encoding="utf-8")).format(**{role:sql.Identifier(name) for role,name in roles.items()}))
