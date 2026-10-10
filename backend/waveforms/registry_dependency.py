"""Fixed offline history grants and the reviewed PG18 btree_gist1.8 policy."""
import json
from pathlib import Path
from psycopg import sql
from .errors import WaveformError

PROJECTIONS = {
    'registry_devices':('devices',('id','gateway_id','device_id','created_at')),
    'registry_device_states':('device_state_periods',('id','device_id','status','valid_from','valid_to','audit_event_id')),
    'registry_well_bindings':('well_bindings',('id','device_id','well_id','valid_from','valid_to','audit_event_id')),
    'registry_wells':('wells',('id','area_code','well_code','created_at')),
    'registry_head':('registry_head',('singleton','revision')),
    'registry_point_maps':('point_map_versions',('device_id','version','transport','canonical_rule','content','sha256','status','created_at','reviewed_at','created_event_id','review_event_id')),
    'registry_map_assignments':('point_map_assignments',('id','device_id','map_version','valid_from','valid_to','audit_event_id')),
}


def _fail():
    raise WaveformError('waveform.registry_dependency_unsafe','Unsupported or unsafe registry dependency')


def expected_functions():
    data=json.loads(Path(__file__).with_name('btree_gist_18_1_8.json').read_text(encoding='utf-8'))
    return {(name,tuple(args)):(returns,symbol,parallel) for name,args,returns,symbol,parallel in data['functions']}


def verified_extension_functions(conn, reachable):
    """Return allowed OIDs only after exact bidirectional source-manifest match."""
    extension=conn.execute("SELECT e.oid,e.extversion,n.nspname,e.extowner,r.rolsuper,(SELECT nspowner FROM pg_namespace WHERE nspname='device_registry') FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace JOIN pg_roles r ON r.oid=e.extowner WHERE e.extname='btree_gist'").fetchone()
    if extension is None: return set()
    oid,version,schema,owner,superuser,registry_owner=extension
    if conn.info.server_version//10000!=18 or version!='1.8' or schema!='public' or owner in reachable:
        _fail()
    if not superuser and owner!=registry_owner: _fail()
    rows=conn.execute("""SELECT p.oid,p.proname,
        ARRAY(SELECT CASE WHEN tn.nspname='pg_catalog' THEN t.typname ELSE tn.nspname||'.'||t.typname END
              FROM unnest(p.proargtypes) WITH ORDINALITY a(oid,n) JOIN pg_type t ON t.oid=a.oid
              JOIN pg_namespace tn ON tn.oid=t.typnamespace ORDER BY a.n),
        CASE WHEN rn.nspname='pg_catalog' THEN rt.typname ELSE rn.nspname||'.'||rt.typname END,
        p.prosrc,p.probin,p.prokind,l.lanname,p.proisstrict,p.provolatile,p.proparallel,
        p.proretset,p.prosecdef,p.proleakproof,p.proconfig,p.pronargdefaults,p.provariadic,
        p.prosupport::oid,p.proargnames,p.proargmodes,p.proallargtypes,p.procost,p.prorows,
        n.nspname,p.proowner,r.rolsuper
        FROM pg_proc p JOIN pg_depend d ON d.classid='pg_proc'::regclass AND d.objid=p.oid
        AND d.refclassid='pg_extension'::regclass AND d.refobjid=%s AND d.deptype='e'
        JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
        JOIN pg_type rt ON rt.oid=p.prorettype JOIN pg_namespace rn ON rn.oid=rt.typnamespace
        JOIN pg_roles r ON r.oid=p.proowner""",(oid,)).fetchall()
    expected=expected_functions(); actual={}; permitted=set()
    fixed=('$libdir/btree_gist','f','c',True,'i','s',False,False,False,None,0,0,0,None,None,None,1.0,0.0,'public')
    for row in rows:
        function_id,name,args,returns,symbol,*rest=row
        parallel=rest[5]
        normalized=list(rest[:-2]); normalized[5]='s'
        if tuple(normalized)!=fixed or not rest[-1] or rest[-2] in reachable: _fail()
        key=(name,tuple(args))
        if key in actual: _fail()
        actual[key]=(returns,symbol,parallel)
        for identity in reachable:
            if conn.execute("SELECT has_function_privilege(%s,%s,'EXECUTE WITH GRANT OPTION')",(identity,function_id)).fetchone()[0]: _fail()
        permitted.add(function_id)
    if actual!=expected: _fail()
    return permitted


def grant_registry_history_prerequisites(conn, *, registry_owner, waveform_owner):
    """Explicit offline, cross-owner grants. Trusted caller pins BOTH owners."""
    if not registry_owner or not waveform_owner or registry_owner==waveform_owner: _fail()
    actual=conn.execute("SELECT current_user,(SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='device_registry'),(SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='waveforms')").fetchone()
    if actual!=(registry_owner,registry_owner,waveform_owner): _fail()
    conn.execute(sql.SQL('GRANT USAGE ON SCHEMA device_registry TO {}').format(sql.Identifier(waveform_owner)))
    for table,columns in PROJECTIONS.values():
        owner=conn.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=to_regclass(%s)",('device_registry.'+table,)).fetchone()
        if owner!=(registry_owner,): _fail()
        conn.execute(sql.SQL('GRANT SELECT ({}) ON device_registry.{} TO {}').format(
            sql.SQL(',').join(map(sql.Identifier,columns)),sql.Identifier(table),sql.Identifier(waveform_owner)))


def check_history_views(conn):
    """Readiness queries real owner-based views; no DDL or repairs."""
    owners=conn.execute("SELECT nspname,nspowner FROM pg_namespace WHERE nspname IN ('waveforms','device_registry')").fetchall()
    owner_map=dict(owners)
    if len(owner_map)!=2 or owner_map['waveforms']==owner_map['device_registry']: _fail()
    for view,(table,columns) in PROJECTIONS.items():
        row=conn.execute("SELECT c.relowner,c.relkind,c.reloptions FROM pg_class c WHERE c.oid=to_regclass(%s)",('waveforms.'+view,)).fetchone()
        if row!=(owner_map['waveforms'],'v',None): _fail()
        actual=conn.execute("SELECT attname FROM pg_attribute WHERE attrelid=to_regclass(%s) AND attnum>0 AND NOT attisdropped ORDER BY attnum",('waveforms.'+view,)).fetchall()
        if tuple(r[0] for r in actual)!=columns: _fail()
        conn.execute(sql.SQL('SELECT {} FROM waveforms.{} LIMIT 1').format(sql.SQL(',').join(map(sql.Identifier,columns)),sql.Identifier(view))).fetchall()
