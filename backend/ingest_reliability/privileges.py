"""Read-only startup checks for each independently credentialed component."""

import psycopg

from .errors import ReliabilityError
from .migrate import ROLES, check_schema


TABLES = {
    "schema_migrations", "facts", "fact_observations", "cases", "receipts", "deliveries",
    "audit_events", "operation_receipts", "worker_health", "capacity",
}
VIEWS = {"case_metadata", "receipt_metadata", "health_metadata", "audit_metadata", "worker_health_metadata"}
SELECT = {
    "ingestor": {"schema_migrations", "facts", "fact_observations", "cases", "receipts", "deliveries", "capacity"},
    "publisher": {"schema_migrations", "receipts", "deliveries", "worker_health", "capacity"},
    "reader": {"schema_migrations"} | VIEWS,
    "operator": {"schema_migrations", "cases", "deliveries", "operation_receipts"} | VIEWS,
}
INSERT = {
    "ingestor": {"facts", "fact_observations", "cases", "receipts", "deliveries", "audit_events"},
    "publisher": {"worker_health"}, "reader": set(), "operator": {"audit_events", "operation_receipts"},
}
UPDATE = {
    "ingestor": {
        "capacity": {"used_total_bytes", "used_quarantine_bytes"},
        "fact_observations": {"last_seen", "duplicate_count", "transport_anomaly_count", "counters_saturated"},
        "deliveries": {"requested_generation", "requested_at", "next_attempt_at", "version"},
    },
    "publisher": {
        "capacity": {"last_clock_seen"},
        "deliveries": {"sent_generation", "lease_token", "lease_generation", "lease_until", "next_attempt_at",
                       "attempts", "last_error_code", "broker_acked_at", "version"},
        "worker_health": {"last_seen", "last_success", "last_failure", "error_code"},
    },
    "reader": {},
    "operator": {"cases": {"state", "version", "updated_at"},
                 "deliveries": {"requested_generation", "requested_at", "next_attempt_at", "version"}},
}


def _fail():
    raise ReliabilityError("ingest.privilege_unsafe", "接收可靠性数据库权限不符合最小权限要求")


def _reachable(conn):
    # ADMIN-only membership can be converted into a SET/INHERIT path by the
    # grantee. Checking only current inherited privileges misses that route.
    rows = conn.execute("WITH RECURSIVE reachable(oid) AS ("
                        "SELECT oid FROM pg_roles WHERE rolname IN (session_user,current_user) "
                        "UNION SELECT m.roleid FROM pg_auth_members m JOIN reachable r ON m.member=r.oid "
                        "WHERE m.inherit_option OR m.set_option OR m.admin_option) "
                        "SELECT p.oid,p.rolname,p.rolsuper OR p.rolcreatedb OR p.rolcreaterole "
                        "OR p.rolreplication OR p.rolbypassrls FROM pg_roles p "
                        "WHERE p.oid IN (SELECT oid FROM reachable)").fetchall()
    for oid, name, powerful in rows:
        if powerful or name in {"pg_execute_server_program", "pg_read_server_files", "pg_write_server_files"}:
            _fail()
        unsafe = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname IN ('ingest_reliability','public') AND nspowner=%s) "
            "OR EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=%s) "
            "OR has_database_privilege(%s,current_database(),'CREATE,TEMP') "
            "OR has_schema_privilege(%s,'ingest_reliability','CREATE') "
            "OR has_schema_privilege(%s,'public','CREATE') "
            "OR has_parameter_privilege(%s,'session_replication_role','SET,ALTER SYSTEM')",
            (oid,) * 6).fetchone()[0]
        if unsafe:
            _fail()
    return [row[0] for row in rows]


def _permissions(conn, role):
    reachable = _reachable(conn)
    tables = conn.execute(
        "WITH RECURSIVE raw(oid) AS ("
        "SELECT oid FROM pg_class WHERE oid IN (to_regclass('public.ingest_messages'),to_regclass('public.telemetry_samples')) "
        "UNION SELECT i.inhrelid FROM pg_inherits i JOIN raw r ON i.inhparent=r.oid) "
        "SELECT c.oid,n.nspname,c.relname,c.relowner,c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE (n.nspname='ingest_reliability' AND c.relkind IN ('r','p','v','m','f','S')) "
        "OR c.oid IN (SELECT oid FROM raw)").fetchall()
    actual = {(schema, name): kind for _, schema, name, _, kind in tables}
    required = {("ingest_reliability", name): "r" for name in TABLES}
    required.update({("ingest_reliability", name): "v" for name in VIEWS})
    required.update({("public", "ingest_messages"): "r", ("public", "telemetry_samples"): "p"})
    if any(actual.get(name) != kind for name, kind in required.items()):
        raise ReliabilityError("ingest.schema_incomplete", "接收可靠性数据表或视图不完整")
    for oid, schema, name, owner, kind in tables:
        if owner in reachable or kind == "S":
            _fail()
        internal = schema == "ingest_reliability"
        parent = schema == "public" and name in {"ingest_messages", "telemetry_samples"}
        may_select = internal and name in SELECT[role] or parent and role == "ingestor"
        may_insert = internal and name in INSERT[role] or parent and role == "ingestor"
        may_update = UPDATE[role].get(name, set()) if internal else set()
        for identity in reachable:
            unsafe = conn.execute(
                "SELECT has_table_privilege(%s,%s,'DELETE,TRUNCATE,TRIGGER,REFERENCES,MAINTAIN') "
                "OR has_any_column_privilege(%s,%s,'REFERENCES') "
                "OR EXISTS(SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a "
                "WHERE c.oid=%s AND a.grantee IN (0,%s) AND a.is_grantable)",
                (identity, oid, identity, oid, oid, identity)).fetchone()[0]
            if unsafe:
                _fail()
            columns = conn.execute(
                "SELECT attname,has_column_privilege(%s,%s,attnum,'SELECT'),"
                "has_column_privilege(%s,%s,attnum,'INSERT'),has_column_privilege(%s,%s,attnum,'UPDATE'),"
                "EXISTS(SELECT 1 FROM aclexplode(attacl) a WHERE a.grantee IN (0,%s) AND a.is_grantable) "
                "FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped",
                (identity, oid, identity, oid, identity, oid, identity, oid)).fetchall()
            if any((selected and not may_select) or (inserted and not may_insert)
                   or (updated and column not in may_update) or grantable
                   for column, selected, inserted, updated, grantable in columns):
                _fail()
        # A SET-only grant is not enough for normal runtime statements: enforce
        # every required privilege on current_user as well as rejecting excess.
        for privilege, required_here in (("SELECT", may_select), ("INSERT", may_insert)):
            if required_here and not conn.execute("SELECT has_table_privilege(current_user,%s,%s)",
                                                  (oid, privilege)).fetchone()[0]:
                _fail()
        for column in may_update:
            if not conn.execute("SELECT has_column_privilege(current_user,%s,%s,'UPDATE')", (oid, column)).fetchone()[0]:
                _fail()
    functions = conn.execute(
        "SELECT p.oid,p.proname,pg_get_function_identity_arguments(p.oid),p.proowner,n.nspowner,"
        "EXISTS(SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee=0 OR a.is_grantable) "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='ingest_reliability'").fetchall()
    for oid, name, arguments, owner, schema_owner, unsafe_acl in functions:
        required_execute = (role == "ingestor" and name == "reopen_repeated_case"
                            and arguments == "p_fact uuid, p_instant timestamp with time zone")
        required_execute |= (role == 'operator' and name == 'audited_raw'
                             and arguments == 'p_fact uuid, p_actor jsonb, p_preview boolean')
        if owner in reachable or owner != schema_owner or unsafe_acl:
            _fail()
        for identity in reachable:
            if conn.execute("SELECT has_function_privilege(%s,%s,'EXECUTE')", (identity, oid)).fetchone()[0] and not required_execute:
                _fail()
        if required_execute and not conn.execute("SELECT has_function_privilege(current_user,%s,'EXECUTE')", (oid,)).fetchone()[0]:
            _fail()
    # Constraint triggers are PostgreSQL-generated, so check their enabled
    # state independently from the migration's application-object fingerprint.
    if conn.execute("SELECT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='ingest_reliability' "
                    "AND t.tgenabled NOT IN ('O','A'))").fetchone()[0]:
        raise ReliabilityError("ingest.schema_incomplete", "接收可靠性约束触发器未启用")


def probe_role(connect, role: str) -> None:
    """Validate actual effective grants, including indirect escalation paths."""
    if role not in ROLES:
        _fail()
    try:
        with connect() as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                conn.execute("SET LOCAL statement_timeout='3s'")
                conn.execute("SET LOCAL lock_timeout='1s'")
                database = conn.execute("SELECT current_database()").fetchone()[0]
                check_schema(conn, expected_database=database)
                _permissions(conn, role)
    except psycopg.errors.InsufficientPrivilege:
        _fail()
    except psycopg.Error:
        raise ReliabilityError("ingest.connection_unavailable", "接收可靠性数据库连接或安全检查暂不可用") from None
