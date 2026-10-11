"""Read-only effective privilege checks, including inherited escalation paths."""
import psycopg
from .errors import WaveformError
from .migrate import RUNTIME_ROLES, check_schema
from .registry_dependency import PROJECTIONS, verified_extension_functions, check_history_views

TABLES = set("schema_migrations capacity facts message_outcomes observations captures streams chunks chunk_slots completion capture_conflicts assembly_jobs receipts deliveries imports import_lines import_events worker_health audit operation_receipts state_head".split())
VIEWS = set("fact_metadata outcome_metadata capture_metadata receipt_metadata audit_metadata capacity_metadata worker_metadata".split())
SELECT = {
    "receiver": set("schema_migrations facts message_outcomes observations captures streams chunks chunk_slots capture_conflicts assembly_jobs receipts deliveries imports import_lines import_events operation_receipts capacity state_head worker_health".split()),
    "assembler": set("schema_migrations facts message_outcomes observations captures streams chunks chunk_slots capture_conflicts completion receipts deliveries assembly_jobs capacity state_head worker_health".split()),
    "receipt_publisher": set("schema_migrations receipts deliveries capacity worker_health state_head".split()),
    "reader": {"schema_migrations"} | VIEWS,
    "operator": {"schema_migrations", "state_head", "operation_receipts"} | VIEWS,
}
INSERT = {
    "receiver": set("facts message_outcomes observations captures streams chunks chunk_slots capture_conflicts assembly_jobs receipts deliveries imports import_lines import_events operation_receipts audit worker_health".split()),
    "assembler": {"completion","receipts","deliveries","worker_health"},
    "receipt_publisher": {"worker_health"}, "reader": set(), "operator": {"audit","operation_receipts"},
}
UPDATE = {
    "receiver": {
        "capacity": set("used_raw_bytes reserved_raw_bytes used_facts used_incomplete_captures used_orphan_bytes used_quarantine_bytes".split()),
        "observations": set("last_seen duplicate_count anomaly_count counters_saturated".split()),
        "captures": set("manifest_fact_id manifest_outcome_id manifest_sha256 last_valid_at reserved_raw_bytes used_raw_bytes orphan_bytes version".split()),
        "assembly_jobs": set("requested_generation next_attempt_at version".split()),
        "imports": {"state", "version"}, "state_head": {"version"},
        "deliveries": set("requested_generation requested_at next_attempt_at version".split()),
        "worker_health": set("last_seen last_success last_failure error_code".split()),
    },
    "assembler": {
        "assembly_jobs": set("finished_generation lease_token lease_generation lease_until next_attempt_at attempts last_error_code version".split()),
        "captures": {"state","reserved_raw_bytes","version"},
        "capacity": {"reserved_raw_bytes","used_incomplete_captures"}, "state_head": {"version"},
        "deliveries": set("requested_generation requested_at next_attempt_at version".split()),
        "worker_health": set("last_seen last_success last_failure error_code".split()),
    },
    "receipt_publisher": {
        "state_head": {"version"},
        "capacity": {"last_clock_seen"},
        "deliveries": set("sent_generation lease_token lease_generation lease_until next_attempt_at attempts last_error_code broker_acked_at version".split()),
        "worker_health": set("last_seen last_success last_failure error_code".split()),
    },
    "reader": {}, "operator": {"state_head": {"version"}},
}

BUSINESS_TABLES = set('business_settings attachments evidence_versions evidence_events restrictions quality_runs pair_versions pair_reviews business_audit business_receipts dataset_drafts dataset_revisions dataset_events dataset_versions dataset_members dataset_files dataset_manifest_chunks dataset_exports member_research_exports'.split())
BUSINESS_READ = BUSINESS_TABLES-{'attachments','business_receipts'}
BUSINESS_VIEWS = {'attachment_metadata','provenance_metadata','capture_restrictions','archive_channel_metadata'}
TABLES |= BUSINESS_TABLES
VIEWS |= BUSINESS_VIEWS | set(PROJECTIONS)
for _role in ('reader','operator'):
    SELECT[_role] |= BUSINESS_READ | BUSINESS_VIEWS
SELECT['reader'] |= set(PROJECTIONS)
SELECT['operator'] |= set(PROJECTIONS)
SELECT['operator'].add('business_receipts')
INSERT['operator'] |= BUSINESS_TABLES-{'business_settings'}
METADATA_SELECT_COLUMNS = {
    'reader': {'imports': {'import_id','file_sha256','file_bytes','line_count','final_lf'},
               'state_head': {'singleton','version'},
               'streams': {'capture_pk','stream_id','chunk_count'},
               'chunk_slots': {'capture_pk','stream_id','chunk_index','fact_id'},
               'completion': {'capture_pk','manifest_fact_id','status','reason_code','result','completed_at'}},
    'operator': {'completion': {'capture_pk','manifest_fact_id','status','reason_code','result','completed_at'}},
}


def _fail(): raise WaveformError("waveform.privilege_unsafe", "Waveform database permissions are unsafe or incomplete")


def _reachable(conn):
    rows = conn.execute("WITH RECURSIVE reachable(oid) AS (SELECT oid FROM pg_roles WHERE rolname IN (session_user,current_user) UNION SELECT m.roleid FROM pg_auth_members m JOIN reachable r ON m.member=r.oid WHERE m.inherit_option OR m.set_option OR m.admin_option) SELECT p.oid,p.rolname,p.rolsuper OR p.rolcreatedb OR p.rolcreaterole OR p.rolreplication OR p.rolbypassrls FROM pg_roles p WHERE p.oid IN (SELECT oid FROM reachable)").fetchall()
    for oid,name,powerful in rows:
        if powerful or name in {"pg_execute_server_program","pg_read_server_files","pg_write_server_files"}: _fail()
        if conn.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname<>'information_schema' AND (nspowner=%s OR has_schema_privilege(%s,oid,'CREATE'))) OR EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datdba=%s) OR has_database_privilege(%s,current_database(),'CREATE,TEMP') OR has_parameter_privilege(%s,'session_replication_role','SET,ALTER SYSTEM')", (oid,)*5).fetchone()[0]: _fail()
    return [row[0] for row in rows]


def _permissions(conn, role):
    reachable = _reachable(conn)
    extension_functions = verified_extension_functions(conn, reachable)
    tables = conn.execute("SELECT c.oid,n.nspname,c.relname,c.relowner,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','v','m','f','S')").fetchall()
    actual = {(schema,name):kind for _,schema,name,_,kind in tables}
    required = {("waveforms",name):"r" for name in TABLES} | {("waveforms",name):"v" for name in VIEWS}
    if any(actual.get(key)!=kind for key,kind in required.items()): _fail()
    schema_owner = conn.execute("SELECT nspowner FROM pg_namespace WHERE nspname='waveforms'").fetchone()[0]
    for oid,schema,name,owner,kind in tables:
        internal = schema=="waveforms"
        if owner in reachable or internal and owner!=schema_owner: _fail()
        if kind=="S":
            if any(conn.execute("SELECT has_sequence_privilege(%s,%s,'USAGE,SELECT,UPDATE')", (identity,oid)).fetchone()[0] for identity in reachable): _fail()
            continue
        may_select = internal and name in SELECT[role]
        select_columns = METADATA_SELECT_COLUMNS.get(role,{}).get(name,set()) if internal else set()
        may_insert = internal and name in INSERT[role]
        may_update = UPDATE[role].get(name,set()) if internal else set()
        for identity in reachable:
            if conn.execute("SELECT has_table_privilege(%s,%s,'DELETE,TRUNCATE,TRIGGER,REFERENCES,MAINTAIN') OR has_any_column_privilege(%s,%s,'REFERENCES') OR EXISTS(SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE c.oid=%s AND a.grantee IN (0,%s) AND a.is_grantable)", (identity,oid,identity,oid,oid,identity)).fetchone()[0]: _fail()
            columns = conn.execute("SELECT attname,has_column_privilege(%s,%s,attnum,'SELECT'),has_column_privilege(%s,%s,attnum,'INSERT'),has_column_privilege(%s,%s,attnum,'UPDATE'),EXISTS(SELECT 1 FROM aclexplode(attacl) a WHERE a.grantee IN (0,%s) AND a.is_grantable) FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped", (identity,oid,identity,oid,identity,oid,identity,oid)).fetchall()
            if any((selected and not may_select and column not in select_columns) or (inserted and not may_insert) or (updated and column not in may_update) or grantable for column,selected,inserted,updated,grantable in columns): _fail()
            if select_columns and conn.execute("SELECT has_table_privilege(%s,%s,'SELECT')",(identity,oid)).fetchone()[0]: _fail()
        for privilege,needed in (("SELECT",may_select),("INSERT",may_insert)):
            if needed and not conn.execute("SELECT has_table_privilege(current_user,%s,%s)", (oid,privilege)).fetchone()[0]: _fail()
        for column in may_update:
            if not conn.execute("SELECT has_column_privilege(current_user,%s,%s,'UPDATE')", (oid,column)).fetchone()[0]: _fail()
        for column in select_columns:
            if not conn.execute("SELECT has_column_privilege(current_user,%s,%s,'SELECT')",(oid,column)).fetchone()[0]: _fail()
    functions = conn.execute("SELECT p.oid,n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),p.proowner,p.prosecdef,p.provolatile,p.proparallel,p.proconfig,EXISTS(SELECT 1 FROM aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE a.grantee=0 OR a.is_grantable) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'").fetchall()
    for oid,schema,name,args,owner,definer,volatility,parallel,config,unsafe_acl in functions:
        permitted = oid in extension_functions or schema=="waveforms" and (
            role=="reader" and name=="audited_raw" and args=="p_fact uuid, p_actor jsonb"
            or role=="reader" and name=="audited_attachment" and args=="p_id uuid, p_actor jsonb, p_bucket text"
            or role=="operator" and name=="lock_business_capacity" and args=="p_bucket text"
            or role=="operator" and name=="lock_business_captures" and args=="p_ids uuid[]"
            or role in {"receiver","operator"} and name=="valid_actor" and args=="actor jsonb")
        if owner in reachable: _fail()
        if schema=="waveforms":
            if owner!=schema_owner or unsafe_acl or config!=["search_path=pg_catalog, pg_temp"]: _fail()
            if name in {"audited_raw","charge_metadata","charge_business","evidence_event_identity","evidence_identity","quality_identity","lock_business_capacity","lock_business_captures","audited_attachment","dataset_identity","dataset_publication_complete"} and (not definer or volatility!='v' or parallel!='u'): _fail()
        for identity in reachable:
            if conn.execute("SELECT has_function_privilege(%s,%s,'EXECUTE')", (identity,oid)).fetchone()[0] and not permitted: _fail()
        if permitted and not conn.execute("SELECT has_function_privilege(current_user,%s,'EXECUTE')", (oid,)).fetchone()[0]: _fail()
    if conn.execute("SELECT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='waveforms' AND t.tgenabled NOT IN ('O','A'))").fetchone()[0]:
        raise WaveformError("waveform.schema_incomplete", "Waveform constraint triggers are disabled")
    if role=='reader': check_history_views(conn)


def probe_role(connect, role: str) -> None:
    if role not in RUNTIME_ROLES: _fail()
    try:
        with connect() as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                conn.execute("SET LOCAL statement_timeout='3s'")
                conn.execute("SET LOCAL lock_timeout='1s'")
                database = conn.execute("SELECT current_database()").fetchone()[0]
                check_schema(conn, expected_database=database)
                _permissions(conn, role)
    except psycopg.errors.InsufficientPrivilege: _fail()
    except psycopg.Error:
        raise WaveformError("waveform.connection_unavailable", "Waveform connection or safety check is unavailable") from None
