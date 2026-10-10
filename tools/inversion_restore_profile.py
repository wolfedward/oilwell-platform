"""Closed fresh WP05A recovery profile; never provisions, migrates or repairs."""
from copy import deepcopy
import json
import os
from pathlib import Path

EXPECTED_SYSTEM_IDENTIFIER='7688689458460612184'


def add_authority(authority,*,resolve=False):
    from tools.provision_inversion_test import load_sidecar,sidecar_identity
    cluster,parent,sidecar=load_sidecar(expected_system_identifier=EXPECTED_SYSTEM_IDENTIFIER)
    if (cluster!=authority['cluster'] or parent!=authority['parent'] or
        sidecar!=sidecar_identity(parent,EXPECTED_SYSTEM_IDENTIFIER)):
        raise ValueError('Fresh inversion authority differs')
    authority['inversion']=sidecar
    if resolve:
        from backend.secret_store import read_windows_credential
        for role in ('reader','operator'):
            secret=read_windows_credential(sidecar['credential_targets'][role])
            if secret.username!=sidecar['roles'][role]: raise ValueError('Inversion credential identity differs')
            authority['settings']['inversion_'+role]=dict(host='127.0.0.1',port=3822,dbname=sidecar['database'],
                user=secret.username,password=secret.password,sslmode='disable',connect_timeout=3)
    return authority


def schemas(profile):
    if profile=='wp04': return ('waveforms','device_registry')
    if profile=='wp05a': return ('waveforms','device_registry','inversion')
    raise ValueError('Unsupported recovery profile')


def validate_database_acl(rows,authority,profile):
    schemas(profile)
    allowed=set(authority['parent']['roles'].values())|{
        authority['sidecar']['role'],authority['cluster']['roles']['admin'],'PUBLIC'}
    added=set(authority['inversion']['roles'].values()) if profile=='wp05a' else set()
    seen=[]
    for row in rows:
        if not isinstance(row,(list,tuple)) or len(row)!=4: raise ValueError('Invalid database ACL')
        grantor,grantee,privilege,grantable=row
        if grantor not in allowed or grantee not in allowed|added or privilege not in ('CONNECT','CREATE','TEMPORARY'):
            raise ValueError('Unsupported database ACL principal or privilege')
        if type(grantable) is not bool: raise ValueError('Invalid database grant option')
        if grantee in added:
            if (grantor,privilege,grantable)!=(authority['parent']['roles']['migration_owner'],'CONNECT',False):
                raise ValueError('Inversion database ACL exceeds pinned CONNECT contract')
            seen.append(grantee)
    if len(seen)!=len(added) or set(seen)!=added: raise ValueError('Inversion database ACL set differs')


def validate_boundary(description,profile):
    permitted=set(schemas(profile))|{'public'}
    if (set(description)!= {'schemas','relations','unsupported'} or
        set(description['schemas'])!=permitted or len(description['schemas'])!=len(permitted) or
        description['unsupported']):
        raise ValueError('Unsupported application namespace or object')
    for schema,name,kind,extension_member in description['relations']:
        if (schema not in permitted or kind not in ('r','p','S','v','i','I') or
            schema=='public' and extension_member is not True):
            raise ValueError('Unsupported application relation')


def binding(authority,run_id,archive_sha256):
    sidecar=authority['inversion']
    if (sidecar['system_identifier']!=EXPECTED_SYSTEM_IDENTIFIER or
        sidecar['parent_fixture_id']!=authority['parent']['fixture_id'] or
        set(sidecar['roles'])!={'owner','reader','operator'} or len(set(sidecar['roles'].values()))!=3):
        raise ValueError('Unbound inversion sidecar')
    return dict(version=2,profile='wp05a',run_id=run_id,archive_sha256=archive_sha256,
        parent_fixture_id=sidecar['parent_fixture_id'],system_identifier=EXPECTED_SYSTEM_IDENTIFIER,
        roles=deepcopy(sidecar['roles']))


def validate_binding(value,authority,run_id,archive_sha256):
    if value!=binding(authority,run_id,archive_sha256): raise ValueError('Fresh recovery profile binding differs')


def validate_fingerprint(value,authority,run_id,archive_sha256,profile):
    schemas(profile)
    try:
        expected={'content','security','properties','snapshot','archive_sha256'}
        if profile=='wp05a': expected|={'profile','boundary'}
        if set(value)!=expected or value['archive_sha256']!=archive_sha256:
            raise ValueError('Recovery fingerprint shape or archive differs')
        validate_database_acl(value['security']['database_acl'],authority,profile)
        if profile=='wp05a':
            validate_binding(value['profile'],authority,run_id,archive_sha256)
            validate_boundary(value['boundary'],profile)
            families={'schemas','relations','columns','constraints','indexes','views','triggers','functions','defaults','extension','database_acl','relation_properties'}
            if set(value['security'])!=families: raise ValueError('Incomplete security inventory')
            measured={schema+'.'+name for schema,name,kind,_ in value['boundary']['relations'] if schema!='public' and kind in ('r','p','S')}
            if set(value['content'])!=measured: raise ValueError('Incomplete content inventory')
            extensions=value['security']['extension']
            if (len(extensions)!=1 or list(extensions[0][:3])!=['btree_gist','1.8','public'] or
                extensions[0][3] not in (authority['sidecar']['role'],authority['cluster']['roles']['admin'])):
                raise ValueError('Unsupported source extension identity')
            for name in ('inversion.results','inversion.result_chunks'):
                if value['content'][name]['rows']<1: raise ValueError('Nonempty inversion result required')
    except (KeyError,TypeError,AttributeError) as error:
        raise ValueError('Incomplete recovery fingerprint') from error


def write_target_proof(root,authority,run_id,fingerprint):
    from tools.waveform_restore import digest_file
    root=Path(root)
    validate_fingerprint(fingerprint,authority,run_id,digest_file(root/'source.dump'),'wp05a')
    proof=dict(binding=fingerprint['profile'],fingerprint_sha256=digest_file(root/'source-fingerprint.json'))
    with (root/'recovery-profile.json').open('x',encoding='utf-8') as handle:
        json.dump(proof,handle,sort_keys=True); handle.flush(); os.fsync(handle.fileno())


def read_target_proof(root,authority,run_id):
    from tools.waveform_restore import digest_file
    root=Path(root)
    try:
        proof=json.loads((root/'recovery-profile.json').read_text(encoding='utf-8'))
        fingerprint=json.loads((root/'source-fingerprint.json').read_text(encoding='utf-8'))
        if (set(proof)!={'binding','fingerprint_sha256'} or proof['binding']!=fingerprint['profile'] or
            proof['fingerprint_sha256']!=digest_file(root/'source-fingerprint.json')):
            raise ValueError('Recovery fingerprint binding changed')
        validate_fingerprint(fingerprint,authority,run_id,digest_file(root/'source.dump'),'wp05a')
        return fingerprint
    except (OSError,KeyError,TypeError,AttributeError) as error:
        raise ValueError('Missing or invalid fresh recovery binding') from error


def content_changes(before,after,profile):
    schemas(profile)
    if set(before)!=set(after): raise ValueError('Recovery relation set differs')
    changed={name for name in before if before[name]!=after[name]}
    if profile=='wp05a' and (not any(name.startswith('inversion.') for name in before) or
        any(name.startswith('inversion.') for name in changed)):
        raise ValueError('Inversion content changed during waveform export')
    return changed


def catalog_boundary(connection,profile):
    """Reject unmeasured dump objects; extension-owned public objects stay in scope."""
    schemas(profile)
    namespace="n.nspname NOT IN ('pg_catalog','pg_toast','information_schema') AND n.nspname !~ '^pg_(toast_)?temp_[0-9]+$'"
    extension="EXISTS(SELECT 1 FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid WHERE d.classid=%s::regclass AND d.objid=%s AND d.deptype='e' AND e.extname='btree_gist')"
    description=dict(schemas=[r[0] for r in connection.execute(
        'SELECT n.nspname FROM pg_namespace n WHERE '+namespace+' ORDER BY 1').fetchall()],
        relations=connection.execute("SELECT n.nspname,c.relname,c.relkind,"+
            extension.replace('%s::regclass',"'pg_class'::regclass").replace('%s','c.oid')+
            ' FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE '+namespace+' ORDER BY 1,2').fetchall(),
        unsupported=[])
    # These object classes are not part of the fixed migrations or btree_gist.
    checks={
        'policy':'SELECT EXISTS(SELECT 1 FROM pg_policy)',
        'event_trigger':'SELECT EXISTS(SELECT 1 FROM pg_event_trigger)',
        'large_object':'SELECT EXISTS(SELECT 1 FROM pg_largeobject_metadata)',
        'foreign_server':'SELECT EXISTS(SELECT 1 FROM pg_foreign_server)',
        'foreign_wrapper':'SELECT EXISTS(SELECT 1 FROM pg_foreign_data_wrapper)',
        'publication':'SELECT EXISTS(SELECT 1 FROM pg_publication)',
        'subscription':'SELECT EXISTS(SELECT 1 FROM pg_subscription)',
        'security_label':'SELECT EXISTS(SELECT 1 FROM pg_seclabel)',
        'language':"SELECT EXISTS(SELECT 1 FROM pg_language WHERE lanname NOT IN ('internal','c','sql','plpgsql'))",
        'extended_statistics':"SELECT EXISTS(SELECT 1 FROM pg_statistic_ext)",
        'rewrite_rule':"SELECT EXISTS(SELECT 1 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class JOIN pg_namespace n ON n.oid=c.relnamespace WHERE "+namespace+" AND r.rulename<>'_RETURN')",
        'transform':"SELECT EXISTS(SELECT 1 FROM pg_transform)",
        'cast':"SELECT EXISTS(SELECT 1 FROM pg_cast c JOIN pg_type s ON s.oid=c.castsource JOIN pg_type t ON t.oid=c.casttarget WHERE s.typnamespace<>'pg_catalog'::regnamespace OR t.typnamespace<>'pg_catalog'::regnamespace)",
        'extension':"SELECT EXISTS(SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE (e.extname,e.extversion,n.nspname) NOT IN (('plpgsql','1.0','pg_catalog'),('btree_gist','1.8','public')))",
        'user_type':"SELECT EXISTS(SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE "+namespace+
            " AND t.typrelid=0 AND t.typelem=0 AND NOT "+extension.replace('%s::regclass',"'pg_type'::regclass").replace('%s','t.oid')+')',
        'function':"SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE "+namespace+
            " AND (p.prokind<>'f' OR n.nspname='public' AND NOT "+extension.replace('%s::regclass',"'pg_proc'::regclass").replace('%s','p.oid')+'))',
    }
    for catalog,namespace_column in (('pg_operator','oprnamespace'),('pg_opclass','opcnamespace'),
        ('pg_opfamily','opfnamespace'),('pg_collation','collnamespace'),('pg_conversion','connamespace'),
        ('pg_ts_config','cfgnamespace'),('pg_ts_dict','dictnamespace'),('pg_ts_parser','prsnamespace'),('pg_ts_template','tmplnamespace')):
        checks[catalog]='SELECT EXISTS(SELECT 1 FROM '+catalog+' o JOIN pg_namespace n ON n.oid=o.'+namespace_column+' WHERE '+namespace+' AND NOT '+extension.replace('%s::regclass',"'"+catalog+"'::regclass").replace('%s','o.oid')+')'
    for name,query in checks.items():
        if connection.execute(query).fetchone()[0]: description['unsupported'].append(name)
    validate_boundary(description,profile)
    # The registry migration predates structure digests; close its object names
    # explicitly rather than trusting its source-hash-only schema check.
    tables=set('schema_migrations wells devices audit_events device_state_periods well_bindings point_map_versions point_map_assignments operation_receipts registry_head'.split())
    indexes={name+'_pkey' for name in tables}|set('wells_area_code_well_code_key devices_gateway_id_device_id_key registry_audit_object_time registry_audit_time registry_binding_well_time state_no_overlap binding_no_overlap assignment_no_overlap'.split())
    actual={(name,kind) for schema,name,kind,_ in description['relations'] if schema=='device_registry'}
    if actual!={(name,'r') for name in tables}|{(name,'i') for name in indexes}:
        raise ValueError('Unsupported registry relation inventory')
    functions=connection.execute("SELECT p.proname,pg_get_function_identity_arguments(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='device_registry' ORDER BY 1,2").fetchall()
    if {tuple(row) for row in functions}!={(name,'') for name in ('close_period_only','immutable_row','review_once','device_identity_fixed','well_identity_fixed')}:
        raise ValueError('Unsupported registry function inventory')
    return description


def source_preflight(connection,authority,profile):
    """Run under the caller's guarded, quiescent read-only source snapshot."""
    boundary=catalog_boundary(connection,profile)
    from backend.waveforms.migrate import check_schema as check_waveforms
    from backend.registry.migrate import check_schema as check_registry
    check_waveforms(connection,expected_database=authority['parent']['database'])
    check_registry(connection,expected_database=authority['parent']['database'])
    if profile=='wp05a':
        from tools.provision_inversion_test import verify_live,check_roles
        from backend.inversion.migrate import check_schema
        verify_live(connection,authority['cluster'],authority['parent'],expected_system_identifier=EXPECTED_SYSTEM_IDENTIFIER)
        check_roles(connection,authority['inversion'])
        check_schema(connection,expected_database=authority['parent']['database'])
        if connection.execute("SELECT queued,running,reserved_bytes FROM inversion.capacity WHERE singleton=1").fetchone()!=(0,0,0):
            raise ValueError('Inversion work is not quiescent')
        if connection.execute("SELECT EXISTS(SELECT 1 FROM inversion.run_state WHERE state IN ('queued','running','cancel_requested'))").fetchone()[0]:
            raise ValueError('Inversion work is not terminal')
    return boundary
