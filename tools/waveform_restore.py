"""Exact owned WP04 recovery target; no generic DSN, provisioning or reset API."""
from datetime import datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import time
import tomllib
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
SOURCE='oilwell_waveforms_test'
PINNED=Path('C:/Users/hp/Documents/外专双百/data/waveform-test')
ROLES=('migration_owner','receiver','assembler','receipt_publisher','reader','operator')
RETAINED_RUN_ID='6afabaf181a24bfcb0011da6fd3a5ee2'
RETAINED_REPORT_SHA256='cee024938dc961999c5b429225cfcb7e7badcd19e11decde4844a3a650eb80c9'
RETAINED_SOURCE_SHA256='d4c6d2a504778de18ced0ffaa5b390b83b109804e45ffea1345aae977834be3c'
RETAINED_ARCHIVE_SHA256='1ae54091e670ed66ebd789ba897cb22a590f5fd68a5f8576db85d9ccce3b0041'
RETAINED_ARCHIVE_SIZE=4044815


def catalog_value(value):
    """Only normalize persisted/live sequence representation; never drop values."""
    return json.loads(json.dumps(value,ensure_ascii=False,allow_nan=False))


def _bound_file(root,item):
    relative=Path(item['relative_path']); path=Path(root)/relative
    if (relative.is_absolute() or '..' in relative.parts or path==root or path.resolve()!=path or
        not path.is_relative_to(root) or type(item['size']) is not int or item['size']<0 or
        path.stat().st_size!=item['size'] or digest_file(path)!=item['sha256']):
        raise ValueError('Retained artifact mismatch')
    return path


def retained_references(root,recovery,origin,index):
    """Authenticate the entire immutable source export set, not failed outputs."""
    from uuid import UUID
    from backend.waveforms.commands import Actor
    from tests.waveform_resource_scenarios import validate_recovery_measurement
    try:
        measured=recovery['recovery_downloads']
        validate_recovery_measurement(measured,run_id=RETAINED_RUN_ID,source_sha256=RETAINED_SOURCE_SHA256)
        prepared=measured['prepared']; records=recovery['records']; references={}; files=[]
        if len(records)!=4 or len(measured['downloads'])!=1107: raise ValueError('Retained export cardinality')
        if records!=[{k:p[k] for k in ('version_id','export_id','actor')} for p in prepared]: raise ValueError('Retained actors or exports differ')
        for p in prepared:
            version=p['version_id']
            if str(UUID(version))!=version or str(UUID(p['export_id']))!=p['export_id']: raise ValueError('Retained UUID mismatch')
            Actor(**p['actor'])
            directory=Path(recovery['references'][version]).relative_to(origin)
            if directory.is_absolute() or '..' in directory.parts: raise ValueError('Retained path escape')
            names=set()
            for f in (f for f in measured['downloads'] if f['version_id']==version):
                relative=(directory/f['name']).as_posix()
                if f['relative_path']!=relative or not re.fullmatch(r'(?:manifest\.json|[0-9a-f-]{36}\.owpwave)',f['name']):
                    raise ValueError('Retained file path mismatch')
                item=dict(relative_path=relative,size=f['size'],sha256=f['sha256'])
                if index[relative]!=item: raise ValueError('Retained file unbound')
                _bound_file(root,item); files.append(item); names.add(f['name'])
            actual=Path(root)/directory
            if {p.name for p in actual.iterdir()}-{'download-progress.jsonl'}!=names: raise ValueError('Unexpected retained file')
            references[version]=str(actual)
        if set(references)!=set(recovery['references']): raise ValueError('Unexpected retained version')
        return dict(references=references,files=files)
    except (KeyError,TypeError,AttributeError,OSError) as error:
        raise ValueError('Invalid retained references') from error


def retained_bundle(root,*,authenticate_all=False):
    """Fixed R90 report is the trust root; no credentials, process or DB access."""
    root=Path(root)
    if digest_file(root/'report.json')!=RETAINED_REPORT_SHA256: raise ValueError('Retained report pin mismatch')
    report=json.loads((root/'report.json').read_text(encoding='utf-8'))
    inventory=report['source']['files_sha256']
    digest=sha256(json.dumps(inventory,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')).hexdigest()
    if (report['run_id']!=RETAINED_RUN_ID or report['status']!='partial' or report['evidence_kind']!='actual' or
        report['evidence_mode']!='three_segment' or digest!=RETAINED_SOURCE_SHA256 or report.get('tests') or report.get('restore') or
        report['source']['unchanged'] is not True or report['protection']['unchanged'] is not True or
        report['cleanup']!=dict(complete=True,residue=[]) or report['identity']['verified'] is not True):
        raise ValueError('Invalid retained completion boundary')
    index={a['relative_path']:a for a in report['artifacts']}
    if len(index)!=len(report['artifacts']): raise ValueError('Duplicate retained artifact')
    for name,item in index.items():
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts or not name or relative.as_posix()!=name:
            raise ValueError('Invalid retained artifact path')
        if authenticate_all: _bound_file(root,item)
    # Exclude ancestors (separately pinned), unused population inputs, and all
    # failed target output bytes. Preserve measurement/lease diagnostics intact.
    closure={n:a for n,a in index.items() if not n.startswith(('prior-','restored-exports/','reference-','prelude-original-'))}
    for item in closure.values(): _bound_file(root,item)
    def read(name):
        if name not in closure: raise ValueError('Missing retained closure')
        return json.loads((root/name).read_text(encoding='utf-8'))
    marker=read('run-identity.json'); origin=ROOT/'logs'/'waveforms-wp04'/('20261007T050743Z-'+RETAINED_RUN_ID)
    if marker!=dict(version=1,run_id=RETAINED_RUN_ID,root=str(origin)): raise ValueError('Retained origin differs')
    recovery=read('recovery-source.json'); measured=read('recovery-downloads.json')
    if measured!=report['recovery_downloads'] or measured!=recovery['recovery_downloads']: raise ValueError('Retained handoff differs')
    refs=retained_references(root,recovery,origin,index)
    if read('lease-measurement.json')!=report['lease_measurement']: raise ValueError('Retained lease differs')
    fingerprint=read('source-fingerprint.json'); restored=read('restore-fingerprint.json')
    if (set(fingerprint)!= {'content','security','snapshot','archive_sha256','properties'} or
        fingerprint['archive_sha256']!=RETAINED_ARCHIVE_SHA256 or not fingerprint['snapshot'] or
        not fingerprint['content'].get('waveforms.facts',{}).get('rows') or
        restored!=dict(content=fingerprint['content'],security=fingerprint['security']) or
        index['source.dump']!=dict(relative_path='source.dump',size=RETAINED_ARCHIVE_SIZE,sha256=RETAINED_ARCHIVE_SHA256) or
        read('dump.json')['exit_code']!=0): raise ValueError('Retained archive binding differs')
    restriction=recovery['restriction']; first=recovery['records'][0]
    if (restriction['version_id']!=first['version_id'] or restriction['actor']!=first['actor'] or
        restriction['evidence_export_id']!=first['export_id'] or restriction['research_export_id']==first['export_id'] or
        restriction['code']!='waveform.dataset_export_denied'): raise ValueError('Retained restriction differs')
    for count,p in zip((1,100,1000),measured['prepared'][1:]):
        envelope=read(f'dataset-{count}.json')
        if (envelope['status']!='passed' or envelope['exit_code']!=0 or envelope['owned_cleanup']['complete'] is not True or
            any(envelope['result'][k]!=v for k,v in p.items())): raise ValueError('Retained dataset phase incomplete')
    failure=read('measurement-3b2cfda7a22c4488aa3df6a08a073202/result.json')
    if (failure['status']!='partial' or failure['reason']!='safety.host_reserve' or failure['error_type']!='RuntimeError' or
        failure['owned_cleanup']['complete'] is not True): raise ValueError('Retained failure boundary differs')
    return dict(report=report,recovery=recovery,fingerprint=fingerprint,archive=root/'source.dump',
        references=refs['references'],files=refs['files'],closure=list(closure.values()),origin_root=str(origin),
        failed_target='oilwell_waveforms_restore_'+RETAINED_RUN_ID,failure_reason=failure['reason'])


def validate_retained_copy(root,source_sha256=None):
    root=validate_run_root(root,existing=True)
    binding=json.loads((root/'retained-source-binding.json').read_text(encoding='utf-8'))
    digest=source_sha256 if source_sha256 is not None else _current_source_sha256()
    if (binding['schema']!='waveform.retained-source/1' or binding['run_id']!=root.name[-32:] or
        binding['run_id']==RETAINED_RUN_ID or binding['source_sha256']!=digest or
        binding['origin_report_sha256']!=RETAINED_REPORT_SHA256 or binding['origin_run_id']!=RETAINED_RUN_ID or
        binding['origin_source_sha256']!=RETAINED_SOURCE_SHA256): raise ValueError('Retained suffix binding differs')
    bundle=retained_bundle(root/'prior-r90')
    mapping=[dict(item,copied_relative_path='prior-r90/'+item['relative_path']) for item in bundle['closure']]
    if binding['files']!=mapping: raise ValueError('Retained copy mapping differs')
    return bundle


def digest_file(path):
    digest=sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''): digest.update(block)
    return digest.hexdigest()


def target_identity(run_id,parent,registry_owner):
    if not isinstance(run_id,str) or not re.fullmatch('[0-9a-f]{32}',run_id): raise ValueError('Invalid run identity')
    if (parent.get('database')!=SOURCE or parent.get('host')!='127.0.0.1' or type(parent.get('port')) is not int or
        parent['port']!=3822 or set(parent.get('roles',{}))!=set(ROLES) or not registry_owner or
        registry_owner in parent['roles'].values()): raise ValueError('Invalid source identity')
    return dict(version=1,run_id=run_id,source_fixture_id=parent['fixture_id'],database='oilwell_waveforms_restore_'+run_id,
        host='127.0.0.1',port=3822,data=str(parent['data']),owner=parent['roles']['migration_owner'],
        registry_owner=registry_owner,roles=dict(parent['roles']),
        marker='oilwell-waveform-restore:'+run_id+':'+parent['fixture_id'])


def validate_target_identity(value,run_id,parent,registry_owner):
    if value!=target_identity(run_id,parent,registry_owner): raise ValueError('Recovery identity mismatch')


def validate_run_root(path,*,existing=False):
    path=Path(path)
    base=ROOT/'logs'/'waveforms-wp04'
    if (not path.is_absolute() or path.parent!=base or path.resolve()!=path or
        not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{32}',path.name) or base.resolve()!=base):
        raise ValueError('Unowned evidence directory')
    if existing:
        value=json.loads((path/'run-identity.json').read_text(encoding='utf-8'))
        if value!=dict(version=1,run_id=path.name[-32:],root=str(path)): raise ValueError('Run marker mismatch')
    elif path.exists(): raise ValueError('Evidence directory already exists')
    return path


def create_run_root(path):
    path=validate_run_root(path)
    path.mkdir(parents=True,exist_ok=False)
    run_id=path.name[-32:]
    with (path/'run-identity.json').open('x',encoding='utf-8') as handle:
        json.dump(dict(version=1,run_id=run_id,root=str(path)),handle)
    return path,run_id


def load_source_authority(config_path,*,resolve=False):
    """Pure complete disk validation precedes optional credential resolution."""
    from tools.provision_waveform_test import PINNED_IDENTITY,load_identity,config_sections
    from tools.provision_waveform_registry_test import sidecar_identity,_config
    path=Path(config_path)
    if not path.is_absolute() or path.resolve()!=(PINNED/'waveform-test.local.toml').resolve():
        raise ValueError('Unpinned source config')
    cluster=load_identity(PINNED_IDENTITY)
    parent=json.loads((PINNED/'identity.json').read_text(encoding='utf-8'))
    token=parent.get('fixture_id')
    if not isinstance(token,str) or not re.fullmatch('[0-9a-f]{32}',token): raise ValueError('Invalid source fixture')
    expected=dict(version=1,fixture_id=token,database=SOURCE,host='127.0.0.1',port=3822,data=str(cluster['data']),
        roles={role:'waveform_test_'+token[:16]+'_'+role for role in ROLES},
        credential_targets={role:'oilwell-waveform-test-'+token+'-'+role for role in ROLES},
        admin_target=cluster['credential_targets']['admin'])
    if parent!=expected: raise ValueError('Source manifest mismatch')
    raw=tomllib.loads(path.read_text(encoding='utf-8'))
    if raw!=config_sections(cluster,parent): raise ValueError('Source config mismatch')
    sidecar=sidecar_identity(parent)
    sidepath=PINNED/'registry-sidecar'
    if json.loads((sidepath/'identity.json').read_text(encoding='utf-8'))!=sidecar: raise ValueError('Registry identity mismatch')
    if tomllib.loads((sidepath/'registry-test.local.toml').read_text(encoding='utf-8'))!=_config(sidecar):
        raise ValueError('Registry config mismatch')
    result={'cluster':cluster,'parent':parent,'sidecar':sidecar,'settings':{},'raw_config':raw}
    if resolve:
        from backend.secret_store import read_windows_credential
        references={role:(parent['roles'][role],parent['credential_targets'][role]) for role in ROLES}
        references.update(registry_owner=(sidecar['role'],sidecar['credential_target']),
                          admin=(cluster['roles']['admin'],cluster['credential_targets']['admin']))
        for role,(user,target) in references.items():
            secret=read_windows_credential(target)
            if secret.username!=user: raise ValueError('Credential identity mismatch')
            result['settings'][role]=dict(host='127.0.0.1',port=3822,dbname=SOURCE,user=user,
                password=secret.password,sslmode='disable',connect_timeout=3)
    return result


def connect_source(authority,role='admin',*,_created=None):
    import psycopg
    connection=psycopg.connect(**authority['settings'][role])
    try:
        if _created is not None: _created(connection)
        if connection.execute('SELECT current_database(),host(inet_server_addr()),inet_server_port(),current_user').fetchone()!= (
            SOURCE,'127.0.0.1',3822,authority['settings'][role]['user']): raise ValueError('Source endpoint mismatch')
        if role=='admin':
            from tools.provision_ingest_test import _verify_live
            _verify_live(connection,authority['cluster'],database=SOURCE)
            expected=(authority['parent']['roles']['migration_owner'],'oilwell-waveform-test:'+authority['parent']['fixture_id'])
            actual=connection.execute("SELECT pg_get_userbyid(datdba),shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()").fetchone()
            if actual!=expected: raise ValueError('Source ownership mismatch')
        connection.rollback(); return connection
    except BaseException as error:
        try: connection.close()
        except BaseException as cleanup_error: raise error from cleanup_error
        raise


class RecoveryTarget:
    """Non-resetting connections can never silently connect to the source."""
    def __init__(self,authority,identity,root):
        self.authority,self.identity,self.root=authority,identity,Path(root)
        self.run_id=identity['run_id']
        self.connections=None
        validate_target_identity(identity,identity['run_id'],authority['parent'],authority['sidecar']['role'])

    def connect(self,role='reader'):
        import psycopg
        validate_target_identity(self.identity,self.run_id,self.authority['parent'],self.authority['sidecar']['role'])
        config=dict(self.authority['settings'][role],dbname=self.identity['database'])
        connection=psycopg.connect(**config)
        if self.connections is not None: self.connections.append(connection)
        try:
            row=connection.execute('SELECT current_database(),host(inet_server_addr()),inet_server_port(),current_user').fetchone()
            if row!=(self.identity['database'],'127.0.0.1',3822,config['user']): raise ValueError('Restore endpoint mismatch')
            owner=connection.execute("SELECT pg_get_userbyid(datdba),shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()").fetchone()
            if owner!=(self.identity['owner'],self.identity['marker']): raise ValueError('Restore marker mismatch')
            if role=='admin':
                from tools.provision_ingest_test import _verify_live
                _verify_live(connection,self.authority['cluster'],database=self.identity['database'])
            connection.rollback(); return connection
        except BaseException as error:
            try: connection.close()
            except BaseException as cleanup_error: raise error from cleanup_error
            raise


def source_state(connection):
    """Full read-only snapshot; the caller already owns both session guards."""
    connection.rollback(); first=None
    try:
        connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        if connection.info.server_version//10000!=18: raise ValueError('PG18 source required')
        properties=connection.execute("SELECT pg_encoding_to_char(encoding),datcollate,datctype,datlocprovider FROM pg_database WHERE datname=current_database()").fetchone()
        if connection.execute('SELECT setconfig FROM pg_db_role_setting WHERE setdatabase=(SELECT oid FROM pg_database WHERE datname=current_database())').fetchall():
            raise ValueError('Unreviewed source database settings')
        return catalog_value(dict(content=content_inventory(connection),security=security_inventory(connection),properties=properties))
    except BaseException as error:
        first=error; raise
    finally:
        try: connection.rollback()
        except BaseException as cleanup_error:
            if first is not None: raise first from cleanup_error
            raise


class RetainedSource:
    """No reset/provisioning API: one verified guard and a thin Stack context."""
    def __init__(self,authority,raw_config,run_id,fingerprint):
        self.authority=authority; self.raw_config=raw_config; self.task17_run_id=run_id
        self.historical_archive=catalog_value({key:fingerprint[key] for key in ('content','security','properties')})
        self.connections=[]; self.guard=None; self.acquired=[]; self.before=None
        self.receipt=dict(complete=False,unlock_attempts=[],unlocked=[],closed=[],errors=[])

    def guard_alive(self):
        conn=self.guard; authority=self.authority
        if conn is None or conn.closed: raise ValueError('Source guard closed')
        endpoint=conn.execute('SELECT current_database(),host(inet_server_addr()),inet_server_port(),current_user').fetchone()
        if endpoint!=(SOURCE,'127.0.0.1',3822,authority['settings']['admin']['user']): raise ValueError('Source guard endpoint mismatch')
        owner=conn.execute("SELECT pg_get_userbyid(datdba),shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()").fetchone()
        if owner!=(authority['parent']['roles']['migration_owner'],'oilwell-waveform-test:'+authority['parent']['fixture_id']):
            raise ValueError('Source guard identity mismatch')
        held=conn.execute("SELECT objid FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory' AND granted AND objid IN (499001211,499001213)").fetchall()
        if {r[0] for r in held}!={499001211,499001213}: raise ValueError('Source ownership lock lost')

    def assert_live(self):
        self.guard_alive()
        state=source_state(self.guard)
        if any(catalog_value(state[key])!=self.historical_archive[key] for key in ('security','properties')):
            raise ValueError('Live source security/properties differ from retained archive fingerprint')
        return state

    def __enter__(self):
        try:
            self.guard=connect_source(self.authority,_created=self.connections.append)
            if not any(c is self.guard for c in self.connections): self.connections.append(self.guard)
            self.guard.autocommit=True
            self.guard.execute("SET statement_timeout='30s'")
            verify_principals(self.guard,self.authority)
            for key in (499001211,499001213):
                self.guard.execute('SELECT pg_advisory_lock(%s)',(key,)); self.acquired.append(key)
            self.before=self.assert_live()
            return self
        except BaseException as error:
            self._close(error); raise

    def _close(self,primary):
        first=primary
        def remember(error):
            nonlocal first
            if first is None: first=error
            self.receipt['errors'].append(type(error).__name__)
        for key in reversed(self.acquired):
            self.receipt['unlock_attempts'].append(key)
            try:
                row=self.guard.execute('SELECT pg_advisory_unlock(%s)',(key,)).fetchone()
                if row!=(True,): raise ValueError('Source guard unlock failed')
                self.receipt['unlocked'].append(key)
            except BaseException as error: remember(error)
        for connection in self.connections:
            try: connection.close()
            except BaseException as error: remember(error)
            self.receipt['closed'].append(bool(connection.closed))
        self.receipt['first_error']=type(first).__name__ if first is not None else None
        self.receipt['complete']=(first is None and all(self.receipt['closed']) and self.receipt['unlocked']==list(reversed(self.acquired)))
        if primary is None and first is not None: raise first

    def __exit__(self,kind,error,traceback):
        first=error
        try:
            after=self.assert_live()
            self.receipt['source_before']=self.before; self.receipt['source_after']=after
            self.receipt['source_unchanged']=after==self.before
            if not self.receipt['source_unchanged']: raise ValueError('Source changed during retained recovery')
        except BaseException as failure:
            self.receipt['errors'].append(type(failure).__name__)
            if first is None: first=failure
        self._close(first)
        if error is None and first is not None: raise first
        return False


def _hash_rows(rows):
    digest=sha256(); count=0
    for row in rows:
        content=json.dumps(row,ensure_ascii=False,separators=(',',':'),default=str).encode()
        digest.update(len(content).to_bytes(8,'big')); digest.update(content); count+=1
    return dict(rows=count,sha256=digest.hexdigest())


def content_inventory(connection):
    """Caller owns one RR snapshot. Stream stable rows including exact BYTEA JSON."""
    from psycopg import sql
    tables=connection.execute("SELECT n.nspname,c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('waveforms','device_registry') AND c.relkind IN ('r','p','S') ORDER BY 1,2").fetchall()
    result={}
    for schema,name,kind in tables:
        key=schema+'.'+name
        if kind=='S':
            result[key]=_hash_rows(connection.execute(sql.SQL('SELECT last_value,is_called FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(name))).fetchall()); continue
        columns=connection.execute("SELECT a.attname FROM pg_index i CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,seq) JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum WHERE i.indrelid=to_regclass(%s) AND i.indisprimary ORDER BY k.seq",(key,)).fetchall()
        if not columns: raise ValueError('Application table lacks stable recovery ordering')
        with connection.cursor(name='wp04_inventory_'+uuid4().hex) as cursor:
            cursor.itersize=16
            cursor.execute(sql.SQL('SELECT row_to_json(t)::text FROM {}.{} t ORDER BY {}').format(
                sql.Identifier(schema),sql.Identifier(name),sql.SQL(',').join(sql.Identifier(c[0]) for c in columns)))
            result[key]=_hash_rows(cursor)
    return result


def security_inventory(connection):
    queries={
      'schemas':"SELECT nspname,pg_get_userbyid(nspowner),nspacl::text FROM pg_namespace WHERE nspname IN ('waveforms','device_registry','public') ORDER BY 1",
      'relations':"""SELECT n.nspname,c.relname,c.relkind,pg_get_userbyid(c.relowner),
        (SELECT COALESCE(jsonb_agg(jsonb_build_array(pg_get_userbyid(x.grantor),
          CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,x.privilege_type,x.is_grantable)
          ORDER BY pg_get_userbyid(x.grantor),CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,x.privilege_type,x.is_grantable),'[]'::jsonb)
         FROM aclexplode(NULLIF(COALESCE(c.relacl,CASE WHEN c.relkind='S' THEN acldefault('s',c.relowner)
           WHEN c.relkind IN ('r','p','v','m','f') THEN acldefault('r',c.relowner) ELSE NULL::aclitem[] END),'{}'::aclitem[])) x),c.reloptions
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('waveforms','device_registry','public') ORDER BY 1,2""",
      'columns':"SELECT n.nspname,c.relname,a.attname,a.attacl::text,a.attnum,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE n.nspname IN ('waveforms','device_registry') AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1,2,3",
      'constraints':"SELECT n.nspname,c.relname,k.conname,k.contype,k.condeferrable,k.condeferred,k.convalidated,k.conenforced,pg_get_constraintdef(k.oid,true) FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('waveforms','device_registry') ORDER BY 1,2,3",
      'indexes':"SELECT n.nspname,c.relname,pg_get_indexdef(i.indexrelid),i.indisvalid,i.indisready FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('waveforms','device_registry') ORDER BY 1,2",
      'views':"SELECT n.nspname,c.relname,pg_get_viewdef(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('waveforms','device_registry') AND c.relkind IN ('v','m') ORDER BY 1,2",
      'triggers':"SELECT n.nspname,c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('waveforms','device_registry') AND NOT t.tgisinternal ORDER BY 1,2,3",
      'functions':"SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),pg_get_userbyid(p.proowner),p.proacl::text,pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN ('waveforms','device_registry','public') ORDER BY 1,2,3",
      'defaults':"SELECT pg_get_userbyid(d.defaclrole),n.nspname,d.defaclobjtype,d.defaclacl::text FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace ORDER BY 1,2,3",
      'extension':"SELECT e.extname,e.extversion,n.nspname,pg_get_userbyid(e.extowner) FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='btree_gist'",
      'database_acl':"SELECT pg_get_userbyid(x.grantor),CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,x.privilege_type,x.is_grantable FROM pg_database d CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) x WHERE d.datname=current_database() ORDER BY 1,2,3",
    }
    return {name:connection.execute(query).fetchall() for name,query in queries.items()}


def restore_arguments(executable,database,user,archive):
    if not re.fullmatch('oilwell_waveforms_restore_[0-9a-f]{32}',database): raise ValueError('Unowned restore target')
    return [str(executable),'--host=127.0.0.1','--port=3822','--username='+user,'--dbname='+database,
            '--single-transaction','--exit-on-error','--no-password',str(archive)]


def create_target(admin,identity,source_properties,*,guard=lambda:None):
    from psycopg import sql
    if admin.execute('SELECT datname FROM pg_database WHERE datname=%s',(identity['database'],)).fetchone():
        raise ValueError('Refusing existing recovery database')
    if source_properties['provider']!='c': raise ValueError('Unverified source locale provider')
    guard()
    admin.execute(sql.SQL('CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING {} LC_COLLATE {} LC_CTYPE {} LOCALE_PROVIDER libc').format(
        sql.Identifier(identity['database']),sql.Identifier(identity['owner']),sql.Literal(source_properties['encoding']),
        sql.Literal(source_properties['collate']),sql.Literal(source_properties['ctype'])))
    guard(); admin.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(sql.Identifier(identity['database']),sql.Literal(identity['marker'])))
    guard()
    admin.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(identity['database'])))


def verify_principals(connection,authority):
    marker='oilwell-waveform-test:'+authority['parent']['fixture_id']
    principals={name:marker for name in authority['parent']['roles'].values()}
    principals[authority['sidecar']['role']]='oilwell-waveform-registry-test:'+authority['parent']['fixture_id']
    for name,comment in principals.items():
        row=connection.execute("SELECT oid,rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,rolcanlogin,shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s",(name,)).fetchone()
        if row is None or row[1:]!=(False,True,comment): raise ValueError('Unowned or elevated recovery principal')
        if connection.execute('SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=%s OR roleid=%s)',(row[0],row[0])).fetchone()[0]:
            raise ValueError('Unexpected recovery role membership')


def _tool(executable,args,authority,root,label,timeout=120):
    began=time.monotonic()
    result=subprocess.run([str(executable),*args],env=dict(os.environ,PGPASSWORD=authority['settings']['admin']['password']),
        capture_output=True,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    # Tool diagnostics can reveal role/SQL details; retained locally, not stdout.
    with (root/(label+'.stdout')).open('xb') as handle: handle.write(result.stdout)
    with (root/(label+'.stderr')).open('xb') as handle: handle.write(result.stderr)
    record=dict(tool=executable.name,executable_sha256=digest_file(executable),exit_code=result.returncode,
        duration_seconds=time.monotonic()-began,args=args)
    with (root/(label+'.json')).open('x',encoding='utf-8') as handle: json.dump(record,handle,indent=2)
    if result.returncode: raise RuntimeError('Recovery tool failed: '+label)
    return record


def _restore_archive(authority,root,run_id,*,archive,fingerprint,guard,records=None,connections=None):
    """Shared fresh-target tail: never adopts a target or dumps the source."""
    from psycopg import sql
    from backend.waveforms.migrate import check_schema
    from backend.waveforms.privileges import probe_role
    from backend.waveforms.registry_dependency import check_history_views
    root=Path(root); tools=Path('C:/Program Files/PostgreSQL/18/bin')
    identity=target_identity(run_id,authority['parent'],authority['sidecar']['role'])
    target=RecoveryTarget(authority,identity,root); target.connections=connections
    records=[] if records is None else records
    source_content=fingerprint['content']; source_security=fingerprint['security']; properties=fingerprint['properties']
    if digest_file(archive)!=fingerprint['archive_sha256']: raise ValueError('Restore archive changed')
    def source_admin():
        connection=connect_source(authority,_created=connections.append if connections is not None else None)
        return connection
    with (root/'restore-identity.json').open('x',encoding='utf-8') as handle: json.dump(identity,handle,indent=2)
    guard()
    with source_admin() as admin:
        admin.autocommit=True
        create_target(admin,identity,dict(zip(('encoding','collate','ctype','provider'),properties)),guard=guard)
        allowed=set(identity['roles'].values())|{identity['registry_owner'],authority['settings']['admin']['user'],'PUBLIC'}
        for grantor,grantee,privilege,grantable in source_security['database_acl']:
            if grantee not in allowed or grantor not in allowed or privilege not in ('CONNECT','CREATE','TEMPORARY'):
                raise ValueError('Unknown database ACL principal or privilege')
            if grantee==identity['owner']: continue
            grantee_sql=sql.SQL('PUBLIC') if grantee=='PUBLIC' else sql.Identifier(grantee)
            guard()
            admin.execute(sql.SQL('GRANT {} ON DATABASE {} TO {} {}').format(sql.SQL(privilege),sql.Identifier(identity['database']),
                grantee_sql,sql.SQL('WITH GRANT OPTION' if grantable else '')))
    guard()
    with target.connect('admin') as conn:
        extensions=source_security['extension']
        if len(extensions)!=1 or catalog_value(extensions[0][:3])!=['btree_gist','1.8','public']: raise ValueError('Unsupported source extension')
        owner=extensions[0][3]
        owner_row=conn.execute('SELECT rolsuper FROM pg_roles WHERE rolname=%s',(owner,)).fetchone()
        if owner!=identity['registry_owner'] and owner_row!=(True,): raise ValueError('Unsupported extension owner')
        # One transaction: uncertainty rolls grants/creation back together.
        guard(); conn.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO {}').format(sql.Identifier(identity['database']),sql.Identifier(owner)))
        guard(); conn.execute(sql.SQL('GRANT USAGE,CREATE ON SCHEMA public TO {}').format(sql.Identifier(owner)))
        guard(); conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(owner)))
        guard(); conn.execute("CREATE EXTENSION btree_gist WITH SCHEMA public VERSION '1.8'")
        guard(); conn.execute('RESET ROLE')
        guard(); conn.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM {}').format(sql.Identifier(identity['database']),sql.Identifier(owner)))
        guard(); conn.execute(sql.SQL('REVOKE USAGE,CREATE ON SCHEMA public FROM {}').format(sql.Identifier(owner)))
        guard(); conn.commit()
    guard()
    records.append(_tool(tools/'pg_restore.exe',['--list',str(archive)],authority,root,'archive-list'))
    args=restore_arguments(tools/'pg_restore.exe',identity['database'],authority['settings']['admin']['user'],archive)
    guard()
    records.append(_tool(Path(args[0]),args[1:],authority,root,'restore'))
    guard()
    with target.connect('admin') as conn:
        guard(); conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        restored_content=content_inventory(conn); restored_security=security_inventory(conn)
        check_schema(conn,expected_database=identity['database']); check_history_views(conn)
        if restored_content!=source_content: raise AssertionError('Restored application bytes/history differ')
        if catalog_value(restored_security)!=catalog_value(source_security): raise AssertionError('Restored owners/ACL/native definitions differ')
    for role in ROLES[1:]: probe_role(lambda role=role:target.connect(role),role)
    with (root/'restore-fingerprint.json').open('x',encoding='utf-8') as handle:
        json.dump(dict(content=restored_content,security=restored_security),handle,default=str,indent=2)
    return target,dict(snapshot=True,content=True,schema=True,acl=True,extension=True,exports=False,restrictions=False,
        target=identity['database'],tools=records,archive_sha256=digest_file(archive),retained=True)



def restore_retained(source,root,run_id):
    bundle=validate_retained_copy(root)
    source.assert_live()  # Identity/security/properties match; content has its own fresh baseline.
    tools=Path('C:/Program Files/PostgreSQL/18/bin'); records=[]
    for name in ('pg_dump.exe','pg_restore.exe'):
        version=subprocess.run([str(tools/name),'--version'],stdin=subprocess.DEVNULL,capture_output=True,check=True,timeout=5).stdout.decode('ascii').strip()
        if not re.search(r'\b18(?:\.|\b)',version): raise ValueError('PG18 recovery tools required')
        records.append(dict(tool=name,version=version,sha256=digest_file(tools/name)))
    return _restore_archive(source.authority,root,run_id,archive=bundle['archive'],fingerprint=bundle['fingerprint'],
        guard=source.guard_alive,records=records,connections=source.connections)


def _unlock_backup_guard(fixture,primary):
    try:
        if not fixture.guard.closed: fixture.guard.execute('SELECT pg_advisory_unlock(%s)',(499001213,))
    except BaseException as failure:
        if primary is None: raise
        primary.add_note('Backup source guard unlock cleanup: '+type(failure).__name__)


def backup_restore(fixture,config_path,root,run_id):
    """Caller has quiesced its writers and still owns the source reset guard.

    Neither fixture teardown nor target export is performed here. Target records
    and archive are intentionally retained on every exit, including failure.
    """
    from psycopg import sql
    from backend.waveforms.migrate import check_schema
    from backend.waveforms.privileges import probe_role
    from backend.waveforms.registry_dependency import check_history_views
    authority=load_source_authority(config_path,resolve=True)
    identity=target_identity(run_id,authority['parent'],authority['sidecar']['role'])
    root=Path(root)
    def guard_alive():
        fixture._check_endpoint(fixture.guard)
        held=fixture.guard.execute("SELECT objid FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory' AND granted AND objid IN (499001211,499001213)").fetchall()
        if {r[0] for r in held}!={499001211,499001213}: raise ValueError('Source ownership lock lost')
    fixture.guard.execute('SELECT pg_advisory_lock(%s)',(499001213,))
    tools=Path('C:/Program Files/PostgreSQL/18/bin')
    records=[]
    try:
        guard_alive()
        with connect_source(authority) as admin:
            verify_principals(admin,authority)
            properties=admin.execute("SELECT pg_encoding_to_char(encoding),datcollate,datctype,datlocprovider FROM pg_database WHERE datname=current_database()").fetchone()
            settings=admin.execute('SELECT setconfig FROM pg_db_role_setting WHERE setdatabase=(SELECT oid FROM pg_database WHERE datname=current_database())').fetchall()
            if settings: raise ValueError('Unreviewed source database settings')
            unknown=admin.execute("SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND pid NOT IN (%s,%s) AND state IS DISTINCT FROM 'idle'",(admin.info.backend_pid,fixture.guard.info.backend_pid)).fetchall()
            if unknown: raise ValueError('Source writers are not quiescent')
            admin.rollback()
            admin.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
            snapshot=admin.execute('SELECT pg_export_snapshot()').fetchone()[0]
            source_content=content_inventory(admin); source_security=security_inventory(admin)
            if not source_content.get('waveforms.facts',{}).get('rows'): raise ValueError('Populated recovery facts required')
            if admin.info.server_version//10000!=18: raise ValueError('PG18 source required')
            for name in ('pg_dump.exe','pg_restore.exe'):
                version=subprocess.run([str(tools/name),'--version'],capture_output=True,check=True,timeout=5).stdout.decode('ascii').strip()
                if not re.search(r'\b18(?:\.|\b)',version): raise ValueError('PG18 recovery tools required')
                records.append(dict(tool=name,version=version,sha256=digest_file(tools/name)))
            archive=root/'source.dump'
            dump_args=['--host=127.0.0.1','--port=3822','--username='+authority['settings']['admin']['user'],
                '--dbname='+SOURCE,'--format=custom','--snapshot='+snapshot,'--file='+str(archive),'--no-password']
            records.append(_tool(tools/'pg_dump.exe',dump_args,authority,root,'dump'))
            source_fingerprint=dict(content=source_content,security=source_security,snapshot=snapshot,
                archive_sha256=digest_file(archive),properties=properties)
            with (root/'source-fingerprint.json').open('x',encoding='utf-8') as handle: json.dump(source_fingerprint,handle,default=str,indent=2)
            admin.rollback()
        return _restore_archive(authority,root,run_id,archive=archive,fingerprint=source_fingerprint,guard=guard_alive,records=records)
    finally:
        import sys
        _unlock_backup_guard(fixture,sys.exc_info()[1])


class _ExportClock:
    def __init__(self,*,until=None,emit=lambda *a,**k:None):
        self.until=min(time.monotonic()+1800,until) if until is not None else time.monotonic()+1800
        self.emit=emit; self.sequence=0; self.started=None

    def check(self):
        now=time.monotonic()
        if now>=self.until or (self.started is not None and now-self.started>=120):
            raise RuntimeError('safety.restore_export_deadline')
        return now

    def operation(self,name):
        from contextlib import contextmanager
        @contextmanager
        def run():
            self.check()
            if self.started is not None: raise ValueError('Nested target export operation')
            self.sequence+=1; self.started=time.monotonic()
            self.emit('target_operation_started',sequence=self.sequence,operation=name,started=self.started)
            try:
                yield
                completed=self.check()
                self.emit('target_operation_complete',sequence=self.sequence,operation=name,started=self.started,completed=completed)
            except BaseException as error:
                try: self.emit('target_operation_failed',sequence=self.sequence,operation=name,started=self.started,error_type=type(error).__name__)
                except BaseException as failure: error.add_note('Operation evidence failure: '+type(failure).__name__)
                raise
            finally: self.started=None
        return run()


def export_existing(connect,records,root,*,limits,reference=None,_clock=None):
    """Stream prior exports; target mode compares actual bytes, not just digests."""
    from uuid import UUID
    from backend.waveforms.commands import Actor
    from backend.waveforms.contracts import CaptureKey
    from backend.waveforms.export import DatasetExporter
    exporter=DatasetExporter(lambda:connect('operator'),limits=limits,shared_accounting_bucket='GW',read_connect=lambda:connect('reader'))
    clock=_clock or _ExportClock()
    root=Path(root); output=[]
    with clock.operation('export_setup'): root.mkdir(parents=True,exist_ok=False)
    for record in records:
        actor=Actor(**record['actor']); version=UUID(record['version_id']); export=UUID(record['export_id'])
        directory=root/str(version)
        with clock.operation('describe:'+str(version)):
            directory.mkdir()
            metadata=exporter.describe(version,purpose='evidence',actor=actor)
        def stream(name,expected_sha,expected_bytes,key=None):
          with clock.operation('file:'+str(version)+'/'+name):
            digest=sha256(); length=0; context=iterator=prior=handle=None
            try:
                context=exporter.open(export,actor=actor)
                iterator=context.stream_manifest() if key is None else context.stream_capture(key)
                clock.check()
                prior=(Path(reference[record['version_id']])/name).open('rb') if reference else None
                handle=(directory/name).open('xb')
                for block in iterator:
                    if prior and prior.read(len(block))!=block: raise AssertionError('Recovered export byte differs')
                    handle.write(block); digest.update(block); length+=len(block)
                    clock.check()
                if prior and prior.read(1): raise AssertionError('Recovered export truncated')
                if digest.hexdigest()!=expected_sha or length!=expected_bytes: raise AssertionError('Recovered canonical descriptor differs')
            finally:
                import sys
                active=sys.exc_info()[1]; first=active
                for resource in (iterator,context,prior,handle):
                    if resource is None: continue
                    try: resource.close()
                    except BaseException as error:
                        if first is None: first=error
                        else: first.add_note('Recovered export close failure: '+type(error).__name__)
                if first is not None and active is None: raise first
            return dict(relative_path=str(Path(str(version))/name),bytes=length,sha256=digest.hexdigest())
        manifest=stream('manifest.json',metadata.dataset_content_sha256,metadata.manifest_bytes)
        descriptors=[]; files=[]; offset=0
        while True:
            with clock.operation('page:'+str(version)+':'+str(offset)):
                page=exporter.files(version,actor=actor,offset=offset,limit=100)
            if not page: break
            for item in page:
                key=dict(item['key']); key['capture_id']=UUID(key['capture_id'])
                files.append(stream(str(item['capture_pk'])+'.owpwave',item['file_sha256'],item['content_length'],CaptureKey(**key)))
                descriptors.append(dict(capture_pk=str(item['capture_pk']),file_sha256=item['file_sha256'],bytes=item['content_length']))
            offset+=len(page)
        output.append(dict(version_id=str(version),export_id=str(export),manifest=manifest,files=files,descriptors=descriptors))
    return output


def _current_source_sha256():
    result=subprocess.run(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT,stdin=subprocess.DEVNULL,capture_output=True,check=True,timeout=15)
    files={name:digest_file(ROOT/name) for name in sorted(set(result.stdout.decode('utf-8').split('\0'))) if name and (ROOT/name).is_file()}
    return sha256(json.dumps(files,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')).hexdigest()


def target_export_request(root,run_id,source_sha256,records,references):
    root=validate_run_root(root,existing=True)
    retained=validate_retained_copy(root,source_sha256) if (root/'retained-source-binding.json').exists() else None
    recovery=retained['recovery'] if retained else json.loads((root/'recovery-source.json').read_text(encoding='utf-8'))
    prepared=recovery['recovery_downloads']['prepared']
    if retained and references!=retained['references']: raise ValueError('Retained reference mapping differs')
    refs={p['version_id']:dict(relative_path=Path(references[p['version_id']]).relative_to(root).as_posix(),
        files=[{k:f[k] for k in ('name','size','sha256')} for f in p['expected']]) for p in prepared}
    request=dict(kind='target_exports',run_root=str(root),run_id=run_id,source_sha256=source_sha256,records=records,references=refs)
    _target_export_inputs(request)
    return request


def _target_export_inputs(request):
    """Complete disk/source/run validation; never resolve a credential here."""
    from uuid import UUID
    import math
    try:
        if type(request) is not dict or set(request)-{'kind','run_root','run_id','source_sha256','records','references','preparation_deadline'}: raise ValueError()
        if 'preparation_deadline' in request and (type(request['preparation_deadline']) not in (int,float) or not math.isfinite(request['preparation_deadline'])): raise ValueError()
        root=validate_run_root(request['run_root'],existing=True)
        if request['kind']!='target_exports' or request['run_id']!=root.name[-32:]: raise ValueError()
        digest=request['source_sha256']
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest) or _current_source_sha256()!=digest: raise ValueError()
        retained=validate_retained_copy(root,digest) if (root/'retained-source-binding.json').exists() else None
        recovery=retained['recovery'] if retained else json.loads((root/'recovery-source.json').read_text(encoding='utf-8'))
        measured=recovery['recovery_downloads']
        expected_run=RETAINED_RUN_ID if retained else request['run_id']
        expected_source=RETAINED_SOURCE_SHA256 if retained else digest
        if (measured['status']!='passed' or measured['monitor_cleanup'] is not True or measured['run_id']!=expected_run or
            measured['source_sha256']!=expected_source or recovery['records']!=request['records']): raise ValueError()
        records=request['records']; refs=request['references']
        if type(records) is not list or not 1<=len(records)<=4 or type(refs) is not dict: raise ValueError()
        versions=[]; references={}; expected={}
        prepared={p['version_id']:p for p in measured['prepared']}
        if len(prepared)!=len(measured['prepared']): raise ValueError()
        for record in records:
            if set(record)!= {'version_id','export_id','actor'}: raise ValueError()
            version=record['version_id']
            if str(UUID(version))!=version or str(UUID(record['export_id']))!=record['export_id'] or version in versions: raise ValueError()
            if any(record[k]!=prepared[version][k] for k in ('export_id','actor')): raise ValueError()
            from backend.waveforms.commands import Actor
            Actor(**record['actor'])
            versions.append(version); reference=refs[version]
            if set(reference)!= {'relative_path','files'}: raise ValueError()
            relative=Path(reference['relative_path']); directory=root/relative
            if relative.is_absolute() or '..' in relative.parts or directory.resolve()!=directory or not directory.is_relative_to(root): raise ValueError()
            if str(directory)!=(retained['references'] if retained else recovery['references'])[version]: raise ValueError()
            files=reference['files']
            if files!=[{k:f[k] for k in ('name','size','sha256')} for f in prepared[version]['expected']]: raise ValueError()
            if not 1<=len(files)<=1001: raise ValueError()
            names=set()
            for file in files:
                name=file['name']
                if (name in names or not re.fullmatch(r'(?:manifest\.json|[0-9a-f-]{36}\.owpwave)',name) or
                    type(file['size']) is not int or file['size']<=0 or not re.fullmatch('[0-9a-f]{64}',file['sha256'])): raise ValueError()
                path=directory/name
                if path.resolve()!=path or path.stat().st_size!=file['size'] or digest_file(path)!=file['sha256']: raise ValueError()
                names.add(name); expected[version+'/'+name]=file
            if 'manifest.json' not in names or {p.name for p in directory.iterdir()}-{'download-progress.jsonl'}!=names: raise ValueError()
            journal=directory/'download-progress.jsonl'
            if journal.exists() and (not journal.is_file() or journal.resolve()!=journal): raise ValueError()
            references[version]=directory
        if set(refs)!=set(versions) or set(prepared)!=set(versions): raise ValueError()
        authority=load_source_authority(PINNED/'waveform-test.local.toml',resolve=False)
        identity=json.loads((root/'restore-identity.json').read_text(encoding='utf-8'))
        validate_target_identity(identity,request['run_id'],authority['parent'],authority['sidecar']['role'])
        return authority,identity,references,expected
    except (KeyError,TypeError,AttributeError,OSError) as error:
        raise ValueError('Invalid target export request') from error


def _verify_target_exports(request,result,*,operations=None):
    """Authenticate the exact output set and bytes before accepting child evidence."""
    _,identity,references,expected=_target_export_inputs(request)
    root=Path(request['run_root'])/'restored-exports'; files={}; names=['identity','export_setup']
    if (type(result) is not dict or result.get('run_id')!=request['run_id'] or result.get('source_sha256')!=request['source_sha256'] or
        result.get('target')!=identity['database'] or len(result.get('exports',[]))!=len(request['records'])): raise ValueError('Unbound target export output')
    for record,output in zip(request['records'],result['exports']):
        version=record['version_id']
        if output['version_id']!=version or output['export_id']!=record['export_id']: raise ValueError('Target export identity differs')
        names.extend(['describe:'+version,'file:'+version+'/manifest.json'])
        captures=output['files']; descriptors=output['descriptors']
        if len(captures)!=len(descriptors): raise ValueError('Target descriptor set differs')
        for offset in range(0,len(captures),100):
            names.append('page:'+version+':'+str(offset))
            names.extend('file:'+str(f['relative_path']).replace('\\','/') for f in captures[offset:offset+100])
        names.append('page:'+version+':'+str(len(captures)))
        for file in [output['manifest'],*captures]:
            relative=str(file['relative_path']).replace('\\','/')
            if relative in files or relative not in expected: raise ValueError('Unexpected target output')
            original=expected[relative]; path=root/relative
            if file['sha256']!=original['sha256'] or file['bytes']!=original['size'] or path.resolve()!=path: raise ValueError('Target file identity differs')
            digest=sha256(); length=0
            with path.open('rb') as actual,(references[version]/original['name']).open('rb') as prior:
                while block:=actual.read(1024*1024):
                    if prior.read(len(block))!=block: raise ValueError('Target bytes differ')
                    digest.update(block); length+=len(block)
                if prior.read(1): raise ValueError('Target bytes truncated')
            if length!=original['size'] or digest.hexdigest()!=original['sha256']: raise ValueError('Target digest differs')
            files[relative]=file
        for file,descriptor in zip(captures,descriptors):
            if (Path(file['relative_path']).name!=descriptor['capture_pk']+'.owpwave' or
                file['sha256']!=descriptor['file_sha256'] or file['bytes']!=descriptor['bytes']): raise ValueError('Target descriptor differs')
    names.append('finalization')
    expected_paths=set(expected)|{record['version_id'] for record in request['records']}
    if set(files)!=set(expected) or {p.relative_to(root).as_posix() for p in root.rglob('*')}!=expected_paths: raise ValueError('Incomplete target export set')
    if operations is not None and operations!=names: raise ValueError('Incomplete target operation set')
    return result


def run_target_exports(request,emit):
    from tests.waveform_support import LIMITS
    clock=_ExportClock(until=request['preparation_deadline'],emit=emit)
    with clock.operation('identity'):
        _,identity,references,_=_target_export_inputs(request)
        authority=load_source_authority(PINNED/'waveform-test.local.toml',resolve=True)
        target=RecoveryTarget(authority,identity,Path(request['run_root']))
    exports=export_existing(target.connect,request['records'],Path(request['run_root'])/'restored-exports',limits=LIMITS,reference=references,_clock=clock)
    result=dict(run_id=request['run_id'],source_sha256=request['source_sha256'],target=identity['database'],exports=exports)
    with clock.operation('finalization'): _verify_target_exports(request,result)
    return result


def verify_export_deltas(target,before,records,root,*,limits,references,_execute=None):
    """Only exact service-produced audits and their per-bucket charges may change."""
    from collections import Counter
    from uuid import UUID
    from backend.waveforms.commands import Actor
    from backend.waveforms.export import DatasetExporter
    from backend.waveforms.errors import WaveformError
    with target.connect('admin') as connection:
        capacity={row['bucket']:row for (row,) in connection.execute('SELECT row_to_json(c) FROM waveforms.capacity c').fetchall()}
        audit_ids={r[0] for r in connection.execute('SELECT audit_id FROM waveforms.audit').fetchall()}
        business_ids={r[0] for r in connection.execute('SELECT audit_id FROM waveforms.business_audit').fetchall()}
        expected=Counter(); gateways={r[0]:r[1] for r in connection.execute('SELECT fact_id,intake_gateway_id FROM waveforms.facts').fetchall()}
        for record in records:
            for (plan,) in connection.execute('SELECT plan FROM waveforms.dataset_files WHERE version_id=%s',(UUID(record['version_id']),)).fetchall():
                expected.update(UUID(f['fact_id']) for f in plan)
    output=(_execute or export_existing)(target.connect,records,root,limits=limits,reference=references)
    expected_business=sum(1+len(row['files']) for row in output)
    charges=Counter({'GW':expected_business})
    for fact,count in expected.items(): charges[gateways[fact]]+=count
    with target.connect('admin') as connection:
        connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        after=content_inventory(connection)
        changed={name for name in before if before[name]!=after[name]}
        if changed!={'waveforms.audit','waveforms.business_audit','waveforms.capacity'}: raise AssertionError('Unexpected post-export table changes')
        actual=Counter(r[1] for r in connection.execute('SELECT audit_id,fact_id,action FROM waveforms.audit').fetchall() if r[0] not in audit_ids and r[2]=='raw.read')
        if actual!=expected or after['waveforms.audit']['rows']-before['waveforms.audit']['rows']!=sum(expected.values()): raise AssertionError('Post-export raw audit delta')
        new_business=[r for r in connection.execute('SELECT audit_id,action FROM waveforms.business_audit').fetchall() if r[0] not in business_ids]
        if len(new_business)!=expected_business or any(r[1]!='exports.download' for r in new_business): raise AssertionError('Post-export business audit delta')
        for (row,) in connection.execute('SELECT row_to_json(c) FROM waveforms.capacity c').fetchall():
            original=dict(capacity[row['bucket']]); original['used_metadata_rows']+=charges[row['bucket']]
            if original!=row: raise AssertionError('Post-export exact metadata charge delta')
    return dict(exports=output,raw_audits=sum(expected.values()),business_audits=expected_business,metadata_charges=dict(charges),unchanged_other_tables=True)
