"""Read-only catalog checks for the migration's minimum runtime privileges."""
import psycopg

from .errors import RegistryError
from .migrate import check_schema


TABLES = ("schema_migrations", "wells", "devices", "audit_events", "device_state_periods",
          "well_bindings", "point_map_versions", "point_map_assignments", "operation_receipts", "registry_head")
INSERT_TABLES = set(TABLES) - {"schema_migrations", "registry_head"}
UPDATE_COLUMNS = {
    "wells": {"name", "status", "version", "updated_at"},
    "devices": {"name", "manufacturer", "model", "serial_number", "check_note", "usage", "usage_locked", "status", "version", "updated_at"},
    "device_state_periods": {"valid_to"}, "well_bindings": {"valid_to"}, "point_map_assignments": {"valid_to"},
    "point_map_versions": {"status", "reviewed_at", "review_event_id"}, "registry_head": {"revision"},
}
# Names/types come from the explicit v1 migration, not from a permissive live
# baseline. A migration record alone cannot prove protections are still enabled.
CHECKS = {
    "schema_migrations": "version sha256", "wells": "area_code well_code name status version",
    "devices": "gateway_id device_id name manufacturer model serial_number check_note transport usage status version",
    "audit_events": "actor reason before_data after_data", "device_state_periods": "status valid_from",
    "well_bindings": "valid_from", "point_map_versions": "version transport content sha256 status",
    "point_map_assignments": "map_version valid_from", "operation_receipts": "request_hash result",
    "registry_head": "singleton revision",
}
FOREIGN_KEYS = {
    "device_state_periods": "device_id audit_event_id", "well_bindings": "device_id well_id audit_event_id",
    "point_map_versions": "device_id device_id_parent_version created_event_id review_event_id",
    "point_map_assignments": "device_id_map_version audit_event_id",
}
TRIGGERS = {
    "wells": ("well_identity_fixed", "well_identity_fixed"), "devices": ("device_identity_fixed", "device_identity_fixed"),
    "audit_events": ("audit_append_only", "immutable_row"), "operation_receipts": ("receipt_append_only", "immutable_row"),
    "device_state_periods": ("state_close_only", "close_period_only"), "well_bindings": ("binding_close_only", "close_period_only"),
    "point_map_assignments": ("assignment_close_only", "close_period_only"), "point_map_versions": ("map_review_once", "review_once"),
}


def _fail(code="privilege_unsafe"):
    raise RegistryError("registry." + code, "台账数据库权限或保护约束不符合要求，须由管理员检查", 503)


def _structure(conn):
    extension = conn.execute("SELECT oid FROM pg_extension WHERE extname='btree_gist'").fetchone()
    if extension is None:
        _fail("schema_incomplete")
    rows = conn.execute("SELECT c.relname,k.conname,k.contype,k.convalidated,k.condeferrable,k.condeferred,"
                        "COALESCE((to_jsonb(k)->>'conenforced')::boolean,true),"
                        "COALESCE(i.indisvalid AND i.indisready,true) "
                        "FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_index i ON i.indexrelid=k.conindid "
                        "WHERE n.nspname='device_registry'").fetchall()
    constraints = {(row[0], row[1]): row[2:] for row in rows}
    required = {(table, table + "_pkey"): "p" for table in TABLES}
    for table, fields in CHECKS.items():
        required.update({(table, table + "_" + field + "_check"): "c" for field in fields.split()})
    for table, fields in FOREIGN_KEYS.items():
        required.update({(table, table + "_" + field + "_fkey"): "f" for field in fields.split()})
    for table, name, kind in (("wells", "wells_area_code_well_code_key", "u"),
                              ("devices", "devices_gateway_id_device_id_key", "u"),
                              ("device_state_periods", "state_no_overlap", "x"),
                              ("well_bindings", "binding_no_overlap", "x"),
                              ("point_map_assignments", "assignment_no_overlap", "x")):
        required[table, name] = kind
    for table in ("device_state_periods", "well_bindings", "point_map_assignments", "point_map_versions"):
        required[table, table + "_check"] = "c"
    required["point_map_versions", "point_map_versions_check1"] = "c"
    for key, kind in required.items():
        row = constraints.get(key)
        deferred = kind == "f" and any(key[1].endswith("_" + field + "_fkey")
                                        for field in ("audit_event_id", "created_event_id", "review_event_id"))
        if row is None or row != (kind, True, deferred, deferred, True, True):
            _fail("schema_incomplete")
    triggers = conn.execute("SELECT c.relname,t.tgname,t.tgenabled,t.tgtype,t.tgisinternal,p.proname,nf.nspname,p.prosecdef "
                            "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid "
                            "JOIN pg_namespace nf ON nf.oid=p.pronamespace WHERE n.nspname='device_registry'").fetchall()
    if any(row[2] not in ("O", "A") for row in triggers):
        _fail("schema_incomplete")
    by_name = {(row[0], row[1]): row for row in triggers}
    for table, (trigger, function) in TRIGGERS.items():
        row = by_name.get((table, trigger))
        if row is None or row[3:] != (27, False, function, "device_registry", False):
            _fail("schema_incomplete")


def _roles(conn):
    # Include SET-only and ADMIN-only routes, not merely inherited privileges.
    # Combining these capabilities can expose privileges after changing roles.
    rows = conn.execute("WITH RECURSIVE reachable(oid) AS ("
                        "SELECT oid FROM pg_roles WHERE rolname IN (session_user,current_user) "
                        "UNION SELECT m.roleid FROM pg_auth_members m JOIN reachable r ON m.member=r.oid "
                        "WHERE m.inherit_option OR m.set_option OR m.admin_option) "
                        "SELECT p.oid,p.rolname,p.rolsuper OR p.rolcreatedb OR p.rolcreaterole OR p.rolreplication OR p.rolbypassrls "
                        "FROM pg_roles p WHERE p.oid IN (SELECT oid FROM reachable)").fetchall()
    for oid, name, powerful in rows:
        if powerful or name in {"pg_execute_server_program", "pg_read_server_files", "pg_write_server_files"}:
            _fail()
        unsafe = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname IN ('device_registry','public') AND nspowner=%s) "
                              "OR EXISTS(SELECT 1 FROM pg_extension WHERE extname='btree_gist' AND extowner=%s) "
                              "OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                              "WHERE n.nspname='device_registry' AND c.relowner=%s) "
                              "OR EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=%s) "
                              "OR has_database_privilege(%s,current_database(),'CREATE') "
                              "OR has_schema_privilege(%s,'device_registry','CREATE') "
                              "OR has_schema_privilege(%s,'public','CREATE') "
                              "OR has_parameter_privilege(%s,'session_replication_role','SET')",
                              (oid,) * 8).fetchone()[0]
        if unsafe:
            _fail()
    return [row[0] for row in rows]


def _permissions(conn, *, writer):
    roles = _roles(conn)
    # Include descendants: a direct grant on a partition bypasses its parent's ACL.
    tables = conn.execute("WITH RECURSIVE raw(oid) AS ("
                          "SELECT oid FROM pg_class WHERE oid IN (to_regclass('public.ingest_messages'),to_regclass('public.telemetry_samples')) "
                          "UNION SELECT i.inhrelid FROM pg_inherits i JOIN raw r ON i.inhparent=r.oid) "
                          "SELECT c.oid,n.nspname,c.relname,c.relowner,c.relkind FROM pg_class c "
                          "JOIN pg_namespace n ON n.oid=c.relnamespace "
                          "WHERE (n.nspname='device_registry' AND c.relkind IN ('r','p','v','m','f')) OR c.oid IN (SELECT oid FROM raw)").fetchall()
    by_name = {(row[1], row[2]): row for row in tables}
    for table in TABLES:
        if ("device_registry", table) not in by_name or by_name["device_registry", table][4] != "r":
            _fail("schema_incomplete")
    if any(("public", table) not in by_name for table in ("ingest_messages", "telemetry_samples")):
        _fail("schema_incomplete")
    for oid, schema, name, owner, kind in tables:
        is_registry = schema == "device_registry" and name in TABLES
        allowed_update = UPDATE_COLUMNS.get(name, set()) if writer and is_registry else set()
        allowed_insert = writer and is_registry and name in INSERT_TABLES
        if owner in roles:
            _fail()
        for role in roles:
            row = conn.execute("SELECT has_any_column_privilege(%s,%s,'INSERT'), "
                               "has_table_privilege(%s,%s,'DELETE,TRUNCATE,TRIGGER,REFERENCES'),"
                               "has_any_column_privilege(%s,%s,'REFERENCES')", (role, oid) * 3).fetchone()
            if (row[0] and not allowed_insert) or row[1] or row[2]:
                _fail()
            updates = {r[0] for r in conn.execute("SELECT attname FROM pg_attribute WHERE attrelid=%s AND attnum>0 "
                                                  "AND NOT attisdropped AND has_column_privilege(%s,%s,attnum,'UPDATE')", (oid, role, oid))}
            if updates - allowed_update:
                _fail()
        # Actual current-user privileges must be sufficient, not merely reachable.
        if is_registry or (not writer and (schema, name) in {("public", "ingest_messages"), ("public", "telemetry_samples")}):
            if not conn.execute("SELECT has_table_privilege(current_user,%s,'SELECT')", (oid,)).fetchone()[0]:
                _fail()
        if allowed_insert and not conn.execute("SELECT has_table_privilege(current_user,%s,'INSERT')", (oid,)).fetchone()[0]:
            _fail()
        for column in allowed_update:
            if not conn.execute("SELECT has_column_privilege(current_user,%s,%s,'UPDATE')", (oid, column)).fetchone()[0]:
                _fail()


def probe(writer_connect, reader_connect) -> None:
    identities = []
    try:
        for connect, writer in ((writer_connect, True), (reader_connect, False)):
            with connect() as conn:
                with conn.transaction():
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    conn.execute("SET LOCAL statement_timeout='3s'")
                    conn.execute("SET LOCAL lock_timeout='1s'")
                    database, user = conn.execute("SELECT current_database(),current_user").fetchone()
                    identities.append((database, user))
                    check_schema(conn, expected_database=database)
                    _structure(conn)
                    _permissions(conn, writer=writer)
        if identities[0][0] != identities[1][0] or identities[0][1] == identities[1][1]:
            _fail()
    except RegistryError:
        raise
    except psycopg.Error:
        raise RegistryError("registry.connection_unavailable", "台账数据库连接或安全检查暂不可用", 503) from None
