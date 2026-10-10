"""Explicit owner-installed metadata ACLs. No raw payload or audited byte capability."""
from psycopg import sql
from .transactions import InversionError

WAVEFORM_COLUMNS = {
    'attachment_metadata': {'attachment_id','accounting_bucket','media_type','payload_bytes','payload_sha256'},
    'evidence_versions': {'evidence_id','accounting_bucket','kind','content_sha256','body'},
    'evidence_events': {'evidence_id','version','state'},
}
REGISTRY_COLUMNS = {'wells': {'id','status'}}


def _grant(conn, roles, schema, tables):
    if set(roles) != {'owner','reader','operator'} or len(set(roles.values())) != 3:
        raise InversionError('invalid_roles')
    owner=conn.execute('SELECT current_user,pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=%s',(schema,)).fetchone()
    if owner is None or owner[0] != owner[1]: raise InversionError('invalid_metadata_owner')
    inversion_owner=conn.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='inversion'").fetchone()
    if inversion_owner != (roles['owner'],): raise InversionError('invalid_roles')
    for role in ('reader','operator'):
        identity=conn.execute('SELECT oid,rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=%s',(roles[role],)).fetchone()
        if identity is None or identity[1] or not identity[2]: raise InversionError('invalid_roles')
        if conn.execute('SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=%s OR roleid=%s)',(identity[0],identity[0])).fetchone()[0]:
            raise InversionError('invalid_roles')
        # Resolve via catalogs: dependency owners need no inversion schema access.
        rights=conn.execute("SELECT has_schema_privilege(%s,'inversion','USAGE'),has_schema_privilege(%s,'inversion','CREATE'),has_table_privilege(%s,(SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='inversion' AND c.relname='assemblies'),'INSERT')",(roles[role],)*3).fetchone()
        if rights != (True,False,role=='operator'): raise InversionError('invalid_roles')
    for role in ('reader','operator'):
        target=sql.Identifier(roles[role])
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),target))
        for table,columns in tables.items():
            conn.execute(sql.SQL('GRANT SELECT ({}) ON {}.{} TO {}').format(
                sql.SQL(',').join(map(sql.Identifier,sorted(columns))),sql.Identifier(schema),sql.Identifier(table),target))


def grant_metadata(conn, *, roles):
    """Run explicitly as waveform schema owner, never at service startup."""
    _grant(conn,roles,'waveforms',WAVEFORM_COLUMNS)


def grant_registry_metadata(conn, *, roles):
    """Run explicitly as registry schema owner."""
    _grant(conn,roles,'device_registry',REGISTRY_COLUMNS)


def probe_metadata(connect):
    """Reject effective inherited/PUBLIC excess and missing column rights."""
    with connect() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='3s'")
        for schema,allowed in (('waveforms',WAVEFORM_COLUMNS),('device_registry',REGISTRY_COLUMNS)):
            if conn.execute('SELECT has_schema_privilege(current_user,%s,\'CREATE\') OR NOT has_schema_privilege(current_user,%s,\'USAGE\')',(schema,schema)).fetchone()[0]:
                raise InversionError('unsafe_metadata_privileges')
            rows=conn.execute("SELECT c.oid,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind IN ('r','v','m')",(schema,)).fetchall()
            for oid,table in rows:
                for privilege in ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN'):
                    if conn.execute('SELECT has_table_privilege(current_user,%s,%s)',(oid,privilege)).fetchone()[0]:
                        raise InversionError('unsafe_metadata_privileges')
                columns=conn.execute('SELECT attnum,attname FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped',(oid,)).fetchall()
                for number,column in columns:
                    for privilege in ('SELECT','INSERT','UPDATE','REFERENCES'):
                        expected=privilege=='SELECT' and column in allowed.get(table,set())
                        actual=conn.execute('SELECT has_column_privilege(current_user,%s,%s,%s)',(oid,number,privilege)).fetchone()[0]
                        grant=conn.execute('SELECT has_column_privilege(current_user,%s,%s,%s)',(oid,number,privilege+' WITH GRANT OPTION')).fetchone()[0]
                        if actual != expected or grant: raise InversionError('unsafe_metadata_privileges')
            if conn.execute('SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=%s AND has_function_privilege(current_user,p.oid,\'EXECUTE\'))',(schema,)).fetchone()[0]:
                raise InversionError('unsafe_metadata_privileges')
