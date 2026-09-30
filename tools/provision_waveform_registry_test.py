"""Explicit owned WP04 registry sidecar; never alters legacy fixture identities."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import tomllib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from backend.secret_store import read_windows_credential
from backend.registry.migrate import migrate as registry_migrate
from backend.waveforms.registry_dependency import grant_registry_history_prerequisites
from tools.provision_waveform_test import (PINNED_IDENTITY,OUTPUT,TEST_DATABASE,
    PROVISION_LOCK,ProvisionError,load_identity,manifest_for,config_sections,_verify_live,_credential)

SIDECAR=OUTPUT/'registry-sidecar'
TEST_LOCK=499001211


def sidecar_identity(parent):
    token=parent['fixture_id']
    return dict(version=1,parent_fixture_id=token,database=TEST_DATABASE,host='127.0.0.1',port=3822,
        data=parent['data'],waveform_owner=parent['roles']['migration_owner'],
        role='waveform_test_'+token[:16]+'_registry_owner',
        credential_target='oilwell-waveform-registry-test-'+token+'-registry-owner')


def _config(sidecar):
    return {'postgres_registry_owner':dict(host='127.0.0.1',port=3822,database=TEST_DATABASE,
        user=sidecar['role'],sslmode='disable',password_credential=sidecar['credential_target'])}


def _load_parent(identity_path):
    identity=load_identity(identity_path)
    if not (OUTPUT/'identity.json').is_file(): raise ProvisionError('Parent waveform fixture must exist')
    parent=manifest_for(OUTPUT,identity,database_exists=True)
    config_path=OUTPUT/'waveform-test.local.toml'
    if tomllib.loads(config_path.read_text(encoding='utf-8'))!=config_sections(identity,parent):
        raise ProvisionError('Parent waveform config changed')
    return identity,parent


def _credential_exists(target):
    """Read only presence: distinguish absent from every OS failure; no adoption."""
    if os.name!='nt': raise ProvisionError('Windows fixture required')
    from ctypes import wintypes
    api=ctypes.WinDLL('Advapi32.dll',use_last_error=True)
    pointer=ctypes.c_void_p()
    api.CredReadW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p)]
    api.CredReadW.restype=wintypes.BOOL
    api.CredFree.argtypes=[ctypes.c_void_p]
    if api.CredReadW(target,1,0,ctypes.byref(pointer)):
        api.CredFree(pointer); return True
    if ctypes.get_last_error()!=1168: raise ProvisionError('Credential presence check unavailable')
    return False


def provision(identity_path=PINNED_IDENTITY):
    identity,parent=_load_parent(identity_path)
    sidecar=sidecar_identity(parent)
    expected_dir=OUTPUT.resolve()/'registry-sidecar'
    if SIDECAR.resolve()!=expected_dir: raise ProvisionError('Unpinned sidecar path')
    marker='oilwell-waveform-registry-test:'+parent['fixture_id']
    identity_file=SIDECAR/'identity.json'; config_file=SIDECAR/'registry-test.local.toml'
    existing=identity_file.exists()
    if existing:
        if json.loads(identity_file.read_text(encoding='utf-8'))!=sidecar: raise ProvisionError('Sidecar identity differs')
        if config_file.exists() and tomllib.loads(config_file.read_text(encoding='utf-8'))!=_config(sidecar):
            raise ProvisionError('Sidecar config differs')
    elif SIDECAR.exists() and any(SIDECAR.iterdir()): raise ProvisionError('Unowned sidecar directory')
    # Credential resolution is only in this pinned provision helper.
    admin_secret=read_windows_credential(identity['credential_targets']['admin'])
    if admin_secret.username!=identity['roles']['admin']: raise ProvisionError('Admin identity mismatch')
    admin_settings=dict(host='127.0.0.1',port=3822,dbname=TEST_DATABASE,user=admin_secret.username,
                        password=admin_secret.password,sslmode='disable',connect_timeout=3)
    with psycopg.connect(**admin_settings,autocommit=True) as admin:
        _verify_live(admin,identity,database=TEST_DATABASE)
        actual=admin.execute("SELECT pg_get_userbyid(datdba),shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()").fetchone()
        if actual!=(parent['roles']['migration_owner'],'oilwell-waveform-test:'+parent['fixture_id']):
            raise ProvisionError('Parent database ownership differs')
        admin.execute("SET statement_timeout='15s'")
        admin.execute('SELECT pg_advisory_lock(%s)',(TEST_LOCK,))
        admin.execute('SELECT pg_advisory_lock(%s)',(PROVISION_LOCK,))
        role=admin.execute("SELECT oid,rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,rolcanlogin,shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s",(sidecar['role'],)).fetchone()
        if role:
            if not existing or role[1] or not role[2] or role[3]!=marker: raise ProvisionError('Unowned registry role')
            if admin.execute('SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=%s OR roleid=%s)',(role[0],role[0])).fetchone()[0]:
                raise ProvisionError('Unexpected registry membership')
        schema_owner=admin.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='device_registry'").fetchone()
        if schema_owner and (not existing or schema_owner!=(sidecar['role'],)): raise ProvisionError('Unowned registry schema')
        if role is None and _credential_exists(sidecar['credential_target']): raise ProvisionError('Unowned credential collision')
        if not existing:
            SIDECAR.mkdir(exist_ok=True)
            with identity_file.open('x',encoding='utf-8') as handle: json.dump(sidecar,handle,ensure_ascii=False,indent=2)
        credential=_credential(sidecar['credential_target'],sidecar['role'],create=role is None)
        if role is None:
            with admin.transaction():
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {} CONNECTION LIMIT 20').format(sql.Identifier(sidecar['role']),sql.Literal(credential.password)))
                admin.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(sql.Identifier(sidecar['role']),sql.Literal(marker)))
        if not config_file.exists():
            fields=_config(sidecar)['postgres_registry_owner']
            with config_file.open('x',encoding='utf-8') as handle:
                handle.write('[postgres_registry_owner]\n'+'\n'.join(k+' = '+json.dumps(v) for k,v in fields.items())+'\n')
        settings=dict(admin_settings,user=sidecar['role'],password=credential.password)
        database=sql.Identifier(TEST_DATABASE); owner=sql.Identifier(sidecar['role'])
        try:
            admin.execute(sql.SQL('GRANT CONNECT,CREATE ON DATABASE {} TO {}').format(database,owner))
            registry_migrate(settings,expected_database=TEST_DATABASE)
            with psycopg.connect(**settings) as conn:
                grant_registry_history_prerequisites(conn,registry_owner=sidecar['role'],waveform_owner=parent['roles']['migration_owner'])
        finally:
            admin.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM {}').format(database,owner))
    return dict(status='prepared',database=TEST_DATABASE,host='127.0.0.1',port=3822)


def owner_settings():
    """Resolve only a preprovisioned, exactly matching owned sidecar credential."""
    identity,parent=_load_parent(PINNED_IDENTITY)
    expected=sidecar_identity(parent)
    if json.loads((SIDECAR/'identity.json').read_text(encoding='utf-8'))!=expected: raise ProvisionError('Sidecar identity mismatch')
    if tomllib.loads((SIDECAR/'registry-test.local.toml').read_text(encoding='utf-8'))!=_config(expected): raise ProvisionError('Sidecar config mismatch')
    credential=_credential(expected['credential_target'],expected['role'],create=False)
    return dict(host='127.0.0.1',port=3822,dbname=TEST_DATABASE,user=credential.username,
                password=credential.password,sslmode='disable',connect_timeout=3)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--identity',required=True)
    args=parser.parse_args()
    try: print(json.dumps(provision(args.identity))); return 0
    except Exception:
        print('{"status":"refused","message":"Owned registry sidecar setup failed"}',file=sys.stderr); return 1


if __name__=='__main__': raise SystemExit(main())
