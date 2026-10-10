"""Read-only effective ACL probe, including PUBLIC and inherited memberships."""
from .transactions import InversionError

INSERT_TABLES=frozenset('assemblies assembly_versions assignments assignment_events parameter_versions reviews model_versions model_reviews selections run_requests run_state run_events attempts attempt_events results result_chunks result_reviews restrictions audit receipts reservations'.split())
INSERT_TABLES = INSERT_TABLES | {'mechanical_events'}
UPDATE_COLUMNS={'state_head':{'version'},'capacity':{'queued','running','reserved_bytes','used_bytes','version'},
                'reservations':{'state','used_bytes','version'},'run_state':{'version','state','generation','owner_id','deadline','heartbeat_at','reason_code'},
                **{table:{'id'} for table in ('assemblies','assembly_versions','parameter_versions','model_versions','run_requests','results')}}


def probe_role(connect,role:str)->None:
    if role not in ('reader','operator'): raise ValueError('Runtime role kind required')
    with connect() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='3s'")
        conn.execute("SET LOCAL lock_timeout='1s'")
        identity=conn.execute('SELECT oid,rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
        def fail(): raise InversionError('unsafe_privileges')
        if identity is None or identity[1]: fail()
        if conn.execute('SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=%s)',(identity[0],)).fetchone()[0]: fail()
        if conn.execute("SELECT has_database_privilege(current_user,current_database(),'CREATE') OR has_schema_privilege(current_user,'inversion','CREATE') OR NOT has_schema_privilege(current_user,'inversion','USAGE')").fetchone()[0]: fail()
        if conn.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='inversion' AND nspowner=%s)",(identity[0],)).fetchone()[0]: fail()
        if conn.execute("""SELECT EXISTS(
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='inversion' AND c.relowner<>n.nspowner
            UNION ALL SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='inversion' AND p.proowner<>n.nspowner)""").fetchone()[0]: fail()
        public_acl=conn.execute("""SELECT EXISTS(
            SELECT 1 FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a WHERE n.nspname='inversion' AND a.grantee=0
            UNION ALL SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE n.nspname='inversion' AND a.grantee=0
            UNION ALL SELECT 1 FROM pg_attribute c JOIN pg_class t ON t.oid=c.attrelid JOIN pg_namespace n ON n.oid=t.relnamespace CROSS JOIN LATERAL aclexplode(c.attacl) a WHERE n.nspname='inversion' AND a.grantee=0
            UNION ALL SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE n.nspname='inversion' AND a.grantee=0)""").fetchone()[0]
        if public_acl: fail()
        tables=conn.execute("SELECT c.oid,c.relname,c.relowner FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='inversion' AND c.relkind='r'").fetchall()
        if not tables: fail()
        for oid,name,owner in tables:
            if owner==identity[0]: fail()
            for privilege in ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN'):
                permitted=privilege=='SELECT' or role=='operator' and privilege=='INSERT' and name in INSERT_TABLES
                actual=conn.execute('SELECT has_table_privilege(current_user,%s,%s)',(oid,privilege)).fetchone()[0]
                if actual!=permitted: fail()
                if conn.execute('SELECT has_table_privilege(current_user,%s,%s)',(oid,privilege+' WITH GRANT OPTION')).fetchone()[0]: fail()
            for number,column in conn.execute('SELECT attnum,attname FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped',(oid,)).fetchall():
                for privilege in ('SELECT','INSERT','UPDATE','REFERENCES'):
                    allowed=(privilege=='SELECT' or role=='operator' and (
                        privilege=='INSERT' and name in INSERT_TABLES or
                        privilege=='UPDATE' and column in UPDATE_COLUMNS.get(name,set())))
                    if conn.execute('SELECT has_column_privilege(current_user,%s,%s,%s)',(oid,number,privilege)).fetchone()[0]!=allowed: fail()
                    if conn.execute('SELECT has_column_privilege(current_user,%s,%s,%s)',(oid,number,privilege+' WITH GRANT OPTION')).fetchone()[0]: fail()
        if conn.execute("SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='inversion' AND has_function_privilege(current_user,p.oid,'EXECUTE'))").fetchone()[0]: fail()
