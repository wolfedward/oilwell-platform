"""Explicit migration, never imported by startup; refuses partial or drifted schema."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from .capacity import InversionLimits
from .transactions import InversionError

SQL_DIR=Path(__file__).with_name('sql')
MIGRATION_LOCK=499001611
MIGRATIONS=((1,'001_inversion.sql'),(2,'002_assembly_versions.sql'),(3,'003_mechanical_validity.sql'))


def _source(filename='001_inversion.sql'):
    source=(SQL_DIR/filename).read_text(encoding='utf-8')
    return source,sha256(source.encode()).hexdigest()


def _target(conn,expected_database):
    if type(expected_database) is not str or not expected_database or conn.execute('SELECT current_database()').fetchone()!=(expected_database,):
        raise InversionError('migration_target')


def structure_digest(conn):
    queries=(
        "SELECT c.relname,c.relkind,c.relrowsecurity,c.relforcerowsecurity,c.reloptions FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='inversion' ORDER BY c.relname",
        "SELECT c.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid,true) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE n.nspname='inversion' AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum",
        "SELECT c.relname,k.conname,k.contype,k.convalidated,k.condeferrable,k.condeferred,pg_get_constraintdef(k.oid,true) FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='inversion' ORDER BY c.relname,k.conname",
        "SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid,true) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='inversion' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname",
        "SELECT c.relname,ix.relname,i.indisvalid,i.indisready,pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_class ix ON ix.oid=i.indexrelid WHERE n.nspname='inversion' ORDER BY c.relname,ix.relname",
        "SELECT p.proname,pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='inversion' ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)",
    )
    return sha256(json.dumps([conn.execute(q).fetchall() for q in queries],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _history(conn,*,complete):
    if conn.execute("SELECT to_regclass('inversion.schema_migrations')").fetchone()[0] is None: raise InversionError('schema_missing')
    rows=conn.execute('SELECT version,sha256,structure_sha256 FROM inversion.schema_migrations ORDER BY version').fetchall()
    expected=[(version,_source(filename)[1]) for version,filename in MIGRATIONS]
    if not rows or len(rows)>len(expected) or complete and len(rows)!=len(expected) or [r[:2] for r in rows]!=expected[:len(rows)]:
        raise InversionError('schema_mismatch')
    if rows[-1][2]!=structure_digest(conn): raise InversionError('schema_mismatch')
    return rows


def _limits(conn,limits):
    if limits is not None:
        if type(limits) is not InversionLimits or conn.execute('SELECT limits,total_storage_bytes FROM inversion.capacity WHERE singleton=1').fetchone()!=(asdict(limits),limits.total_storage_bytes):
            raise InversionError('capacity_configuration')


def check_schema(conn,*,expected_database,limits=None):
    _target(conn,expected_database)
    rows=_history(conn,complete=True)
    _limits(conn,limits)
    return dict(version=str(rows[-1][0]),sha256=rows[-1][1],applied=False)


def migrate(settings:dict,*,expected_database:str,limits:InversionLimits)->dict:
    if type(limits) is not InversionLimits: raise InversionError('capacity_missing')
    try:
        with psycopg.connect(**(settings|{'connect_timeout':3})) as conn:
            _target(conn,expected_database)
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='1s'")
            conn.execute("SET LOCAL search_path=pg_catalog,pg_temp")
            conn.execute('SELECT pg_advisory_xact_lock(%s)',(MIGRATION_LOCK,))
            if conn.execute("SELECT to_regnamespace('inversion')").fetchone()[0] is not None:
                rows=_history(conn,complete=False)
                _limits(conn,limits)
            else:
                rows=[]
            applied=len(rows)<len(MIGRATIONS)
            for version,filename in MIGRATIONS[len(rows):]:
                source,digest=_source(filename)
                conn.execute(source)
                if version==1:
                    conn.execute('INSERT INTO inversion.capacity(singleton,limits,total_storage_bytes) VALUES(1,%s,%s)',(Jsonb(asdict(limits)),limits.total_storage_bytes))
                conn.execute('INSERT INTO inversion.schema_migrations VALUES(%s,%s,%s)',(version,digest,structure_digest(conn)))
            version,filename=MIGRATIONS[-1]
            result=dict(version=str(version),sha256=_source(filename)[1],applied=applied)
        return result
    except psycopg.Error as error: raise InversionError('migration_failed') from error


def grant_roles(conn,*,roles):
    if set(roles)!= {'owner','reader','operator'} or len(set(roles.values()))!=3: raise InversionError('invalid_roles')
    actual=conn.execute("SELECT current_user,pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='inversion'").fetchone()
    if actual!=(roles['owner'],roles['owner']): raise InversionError('invalid_roles')
    for name in roles.values():
        row=conn.execute('SELECT oid,rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=%s',(name,)).fetchone()
        if row is None or row[1] or not row[2]: raise InversionError('invalid_roles')
        if conn.execute('SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=%s OR roleid=%s)',(row[0],row[0])).fetchone()[0]: raise InversionError('invalid_roles')
    conn.execute(sql.SQL((SQL_DIR/'grants.sql').read_text(encoding='utf-8')).format(**{r:sql.Identifier(n) for r,n in roles.items()}))
