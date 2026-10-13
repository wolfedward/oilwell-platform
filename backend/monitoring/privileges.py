"""Fail-closed, read-only permission probe; grants are explicit installer-only calls."""
from pathlib import Path
import psycopg
from psycopg import sql
from .errors import MonitoringError

TABLES = {
    'public.telemetry_samples': None,
    'public.ingest_messages': frozenset('message_id topic gateway_id device_id boot_id sequence_first sequence_last created_at received_at payload_sha256'.split()),
    **{f'device_registry.{name}': None for name in ('wells', 'devices', 'registry_head',
        'device_state_periods', 'well_bindings', 'point_map_assignments')},
    'device_registry.point_map_versions': frozenset('device_id version parent_version transport canonical_rule content sha256 status created_at created_event_id reviewed_at review_event_id'.split()),
    'ingest_reliability.case_metadata': None,
    'ingest_reliability.receipt_metadata': None,
    'ingest_reliability.facts': frozenset('fact_id accepted_message_id status reason_code received_at'.split()),
    'ingest_reliability.fact_observations': frozenset('fact_id first_seen last_seen duplicate_count transport_anomaly_count counters_saturated'.split()),
}


def _unsafe():
    raise MonitoringError('monitoring.privilege_unsafe', '监控查询账号权限不符合只读及原文隔离要求', 503)


def grant_reader(conn, *, role: str, schema: str) -> None:
    """Called by each schema/table owner on a deliberately initialized database."""
    if schema not in {'public', 'device_registry', 'ingest_reliability'}:
        raise ValueError('Unknown monitoring grant schema')
    for table in (name for name in TABLES if name.split('.')[0] == schema):
        if conn.execute('SELECT pg_get_userbyid(relowner)=current_user FROM pg_class WHERE oid=to_regclass(%s)', (table,)).fetchone() != (True,):
            raise ValueError('Monitoring grant requires exact table owner')
    source = (Path(__file__).parent / 'sql' / 'grants.sql').read_text(encoding='utf-8')
    section = source.split(f'-- schema: {schema}\n', 1)[1].split('-- schema:', 1)[0]
    conn.execute(sql.SQL(section).format(reader=sql.Identifier(role)))


def probe_reader(connect) -> None:
    try:
        with connect() as conn:
            with conn.transaction():
                conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                conn.execute("SET LOCAL statement_timeout='3s'")
                conn.execute("SET LOCAL lock_timeout='1s'")
                conn.execute("SET LOCAL search_path=pg_catalog,pg_temp")
                role = conn.execute('SELECT oid,rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls '
                                    'FROM pg_roles WHERE rolname=current_user').fetchone()
                if role is None or role[1]: _unsafe()
                oid = role[0]
                for schema in ('public', 'device_registry', 'ingest_reliability'):
                    if not conn.execute("SELECT has_schema_privilege(current_user,%s,'USAGE')", (schema,)).fetchone()[0]:
                        _unsafe()
                # A dedicated reader needs no inherited or SET ROLE capabilities.
                if conn.execute('SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=%s)', (oid,)).fetchone()[0]: _unsafe()
                if conn.execute("SELECT has_database_privilege(current_user,current_database(),'CREATE') OR "
                    "EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=%s) OR "
                    "EXISTS(SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%%' AND nspname<>'information_schema' "
                    "AND (nspowner=%s OR has_schema_privilege(current_user,oid,'CREATE'))) OR "
                    "has_parameter_privilege(current_user,'session_replication_role','SET')", (oid, oid)).fetchone()[0]: _unsafe()
                rows = conn.execute("SELECT c.oid,n.nspname||'.'||c.relname,c.relowner "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','v','m','f')").fetchall()
                found = set()
                for relation, name, owner in rows:
                    found.add(name)
                    if owner == oid: _unsafe()
                    if conn.execute("SELECT has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES') "
                        "OR has_any_column_privilege(current_user,%s,'INSERT,UPDATE,REFERENCES')", (relation, relation)).fetchone()[0]: _unsafe()
                    selected = {r[0] for r in conn.execute("SELECT attname FROM pg_attribute WHERE attrelid=%s AND attnum>0 "
                        "AND NOT attisdropped AND has_column_privilege(current_user,%s,attnum,'SELECT')", (relation, relation))}
                    if name not in TABLES:
                        if selected: _unsafe()
                    elif TABLES[name] is None:
                        if not conn.execute("SELECT has_table_privilege(current_user,%s,'SELECT')", (relation,)).fetchone()[0]: _unsafe()
                    elif selected != TABLES[name]: _unsafe()
                if not set(TABLES) <= found: _unsafe()
                dangerous = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND p.prosecdef "
                    "AND has_function_privilege(current_user,p.oid,'EXECUTE'))").fetchone()[0]
                if dangerous: _unsafe()
    except MonitoringError:
        raise
    except psycopg.Error:
        raise MonitoringError('monitoring.unavailable', '监控只读账号检查暂不可用', 503) from None
