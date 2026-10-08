"""Explicit isolated migration. Runtime callers only use read-only check_schema."""
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import psycopg
from psycopg import sql
from .config import CapacityLimits, Quota
from .contracts import valid_name
from .errors import WaveformError

SQL_DIR = Path(__file__).with_name("sql")
MIGRATION_LOCK = 499001212
ROLES = {"migration_owner", "receiver", "assembler", "receipt_publisher", "reader", "operator"}
RUNTIME_ROLES = ROLES - {"migration_owner"}
MIGRATIONS = ((1, "001_capture.sql"), (2, "002_import_sessions.sql"), (3, "003_evidence.sql"), (4, "004_datasets.sql"), (5, "005_portable_structure.sql"))


def _fail(code, message): raise WaveformError("waveform."+code, message)


def _target(conn, expected_database):
    if not isinstance(expected_database, str) or not expected_database:
        _fail("migration_target", "An explicit target database is required")
    if conn.execute("SELECT current_database()").fetchone()[0] != expected_database:
        _fail("migration_target", "Connected database differs from explicit migration target")


def _source(filename="001_capture.sql"):
    source = (SQL_DIR / filename).read_text(encoding="utf-8")
    return source, hashlib.sha256(source.encode()).hexdigest()


def structure_digest(conn, *, migration_version=4):
    """Version1–4 keep their exact OID order; version5+ use portable index names.

    The default preserves the legacy public helper contract. History callers
    explicitly select the recorded version. ACL/owners remain separate probes.
    """
    if type(migration_version) is not int or migration_version<1:
        raise ValueError('Positive migration version required')
    queries = (
        "SELECT c.relname,c.relkind,c.relrowsecurity,c.relforcerowsecurity,c.reloptions FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' ORDER BY c.relname",
        "SELECT c.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid,true) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE n.nspname='waveforms' AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum",
        "SELECT c.relname,k.conname,k.contype,k.convalidated,k.conenforced,k.condeferrable,k.condeferred,pg_get_constraintdef(k.oid,true) FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' ORDER BY c.relname,k.conname",
        "SELECT c.relname,t.tgname,t.tgenabled,t.tgtype,pg_get_triggerdef(t.oid,true) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname",
        "SELECT c.relname,i.indisvalid,i.indisready,i.indislive,pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' ORDER BY c.relname,i.indexrelid",
        "SELECT c.relname,pg_get_viewdef(c.oid,true) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' AND c.relkind='v' ORDER BY c.relname",
        "SELECT p.proname,pg_get_function_identity_arguments(p.oid),pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='waveforms' ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)",
    )
    if migration_version>=5:
        queries=queries[:4]+("SELECT c.relname,i.indisvalid,i.indisready,i.indislive,pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_class ix ON ix.oid=i.indexrelid JOIN pg_namespace ixn ON ixn.oid=ix.relnamespace WHERE n.nspname='waveforms' ORDER BY n.nspname,c.relname,ixn.nspname,ix.relname",)+queries[5:]
    parts = [conn.execute(query).fetchall() for query in queries]
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _history(conn, *, complete):
    if conn.execute("SELECT to_regclass('waveforms.schema_migrations')").fetchone()[0] is None:
        _fail("schema_missing", "Waveform schema has not been explicitly initialized")
    rows = conn.execute("SELECT version,sha256,structure_sha256 FROM waveforms.schema_migrations ORDER BY version").fetchall()
    expected = [(version, _source(filename)[1]) for version, filename in MIGRATIONS]
    if (not rows or len(rows)>len(expected) or complete and len(rows)!=len(expected)
            or [row[:2] for row in rows] != expected[:len(rows)]):
        _fail("schema_mismatch", "Waveform migration history differs")
    if rows[-1][2] != structure_digest(conn,migration_version=rows[-1][0]): _fail("schema_incomplete", "Waveform schema protections differ")
    return rows


def check_schema(conn, *, expected_database):
    _target(conn, expected_database)
    rows = _history(conn, complete=True)
    return dict(version=str(rows[-1][0]), sha256=rows[-1][1], applied=False)


def _capacity_rows(limits):
    return [(bucket, *(getattr(quota, field.name) for field in fields(Quota)))
            for bucket,quota in sorted({"*": limits.global_quota, **limits.gateways}.items())]


def migrate(settings, *, expected_database: str, limits: CapacityLimits, shared_accounting_bucket=None) -> dict:
    if type(limits) is not CapacityLimits: _fail("capacity_missing", "Explicit validated capacities are required")
    try:
        with psycopg.connect(**(settings | {"connect_timeout": 3})) as conn:
            _target(conn, expected_database)  # Must be the first database statement.
            if not valid_name(shared_accounting_bucket) or shared_accounting_bucket not in limits.gateways:
                _fail('business_payer_missing', 'Explicit configured business payer is required')
            conn.execute("SET LOCAL lock_timeout='1s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            conn.execute("SET LOCAL search_path=pg_catalog,pg_temp")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            columns = sql.SQL(",").join(map(sql.Identifier, ("bucket", *(field.name for field in fields(Quota)))))
            if conn.execute("SELECT to_regnamespace('waveforms')").fetchone()[0] is not None:
                rows = _history(conn, complete=False)
                existing = conn.execute(sql.SQL("SELECT {} FROM waveforms.capacity ORDER BY bucket").format(columns)).fetchall()
                if existing != _capacity_rows(limits): _fail("capacity_mismatch", "Existing capacity configuration differs; no automatic overwrite")
                if len(rows)>=3:
                    binding=conn.execute('SELECT accounting_bucket FROM waveforms.business_settings WHERE singleton=1').fetchone()
                    if binding!=(shared_accounting_bucket,): _fail('business_payer_mismatch','Persisted business payer differs')
            else:
                rows = []
            # Refuse legacy imports before any pending DDL. Do not infer metadata.
            if len(rows)==1 and conn.execute("SELECT EXISTS(SELECT 1 FROM waveforms.imports)").fetchone()[0]:
                _fail("legacy_imports", "Legacy imports require a separately reviewed explicit migration")
            applied = len(rows)<len(MIGRATIONS)
            for version, filename in MIGRATIONS[len(rows):]:
                source,digest = _source(filename)
                conn.execute(source)
                if version==1:
                    for row in _capacity_rows(limits):
                        conn.execute(sql.SQL("INSERT INTO waveforms.capacity({}) VALUES ({})").format(columns,sql.SQL(",").join(sql.Placeholder() for _ in row)),row)
                if version==3:
                    conn.execute('INSERT INTO waveforms.business_settings(singleton,accounting_bucket) VALUES(1,%s)',(shared_accounting_bucket,))
                conn.execute("INSERT INTO waveforms.schema_migrations(version,sha256,structure_sha256) VALUES(%s,%s,%s)", (version,digest,structure_digest(conn,migration_version=version)))
            version, filename = MIGRATIONS[-1]
            result = dict(version=str(version),sha256=_source(filename)[1],applied=applied)
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
