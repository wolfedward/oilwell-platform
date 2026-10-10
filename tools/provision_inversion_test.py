"""Explicit, pinned inversion sidecar. Never provisions a database or starts services."""
import argparse
import json
from pathlib import Path
import re
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg
from psycopg import sql
from backend.secret_store import read_windows_credential
from tools.provision_waveform_test import PINNED_IDENTITY, OUTPUT, TEST_DATABASE, ProvisionError, _verify_live, _credential
from tools.provision_waveform_registry_test import _load_parent, _credential_exists, TEST_LOCK

SIDECAR = OUTPUT / 'inversion-sidecar'
ROLES = ('owner', 'reader', 'operator')


def system_identifier(value):
    if type(value) is not str or not re.fullmatch(r'[1-9][0-9]{0,19}', value):
        raise ProvisionError('Explicit expected cluster system identifier required')
    return value


def sidecar_identity(parent, expected_system_identifier):
    token = parent['fixture_id']
    return dict(version=1, parent_fixture_id=token, database=TEST_DATABASE, host='127.0.0.1', port=3822,
                data=parent['data'], system_identifier=system_identifier(expected_system_identifier),
                roles={r:'inversion_test_'+token[:16]+'_'+r for r in ROLES},
                credential_targets={r:'oilwell-inversion-test-'+token+'-'+r for r in ROLES})


def config_sections(sidecar):
    return {'postgres_'+r:dict(host='127.0.0.1', port=3822, database=TEST_DATABASE,
            user=sidecar['roles'][r], sslmode='disable', password_credential=sidecar['credential_targets'][r]) for r in ROLES}


def verify_live(conn, identity, parent, *, expected_system_identifier):
    # First query: do not learn/adopt the live identifier as expected identity.
    if conn.execute('SELECT system_identifier::text FROM pg_control_system()').fetchone() != (system_identifier(expected_system_identifier),):
        raise ProvisionError('Cluster system identifier mismatch')
    _verify_live(conn, identity, database=TEST_DATABASE)
    if conn.execute('SELECT current_user').fetchone() != (identity['roles']['admin'],):
        raise ProvisionError('Live admin identity mismatch')
    if conn.execute('SELECT rolsuper,rolcanlogin FROM pg_roles WHERE rolname=%s',(identity['roles']['admin'],)).fetchone() != (True,True):
        raise ProvisionError('Pinned admin authority differs')
    actual = conn.execute("SELECT pg_get_userbyid(datdba),shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()").fetchone()
    if actual != (parent['roles']['migration_owner'], 'oilwell-waveform-test:'+parent['fixture_id']):
        raise ProvisionError('Parent database owner or comment mismatch')


def check_roles(conn, sidecar, *, allow_missing=False):
    marker = 'oilwell-inversion-test:'+sidecar['parent_fixture_id']
    result = {}
    for kind, name in sidecar['roles'].items():
        row = conn.execute("SELECT oid,rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls,rolcanlogin,shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
        if row is None:
            if not allow_missing: raise ProvisionError('Missing inversion role')
        else:
            if row[1] or not row[2] or row[3] != marker: raise ProvisionError('Unowned or elevated inversion role')
            if conn.execute('SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=%s OR roleid=%s)', (row[0],row[0])).fetchone()[0]:
                raise ProvisionError('Unexpected inversion role membership')
        result[kind] = row
    owner = conn.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='inversion'").fetchone()
    if owner and owner != (sidecar['roles']['owner'],): raise ProvisionError('Unowned inversion schema')
    return result


def load_sidecar(*, expected_system_identifier=None):
    identity, parent = _load_parent(PINNED_IDENTITY)
    raw = json.loads((SIDECAR/'identity.json').read_text(encoding='utf-8'))
    expected = sidecar_identity(parent, expected_system_identifier or raw.get('system_identifier'))
    if raw != expected: raise ProvisionError('Sidecar identity differs')
    if tomllib.loads((SIDECAR/'inversion-test.local.toml').read_text(encoding='utf-8')) != config_sections(expected):
        raise ProvisionError('Sidecar configuration differs')
    return identity, parent, expected


def provision(identity_path=PINNED_IDENTITY, output=SIDECAR, *, expected_system_identifier):
    expected_system_identifier = system_identifier(expected_system_identifier)
    output = Path(output)
    if not output.is_absolute() or output.resolve() != SIDECAR.resolve(): raise ProvisionError('Unpinned inversion sidecar')
    identity, parent = _load_parent(identity_path)
    sidecar = sidecar_identity(parent, expected_system_identifier)
    identity_file, config_file = output/'identity.json', output/'inversion-test.local.toml'
    existing = identity_file.exists()
    if existing:
        if json.loads(identity_file.read_text(encoding='utf-8')) != sidecar: raise ProvisionError('Sidecar identity differs')
        if config_file.exists() and tomllib.loads(config_file.read_text(encoding='utf-8')) != config_sections(sidecar):
            raise ProvisionError('Sidecar configuration differs')
    elif output.exists() and any(output.iterdir()): raise ProvisionError('Unowned sidecar directory')
    secret = read_windows_credential(identity['credential_targets']['admin'])
    if secret.username != identity['roles']['admin']: raise ProvisionError('Admin credential identity mismatch')
    settings = dict(host='127.0.0.1', port=3822, dbname=TEST_DATABASE, user=secret.username,
                    password=secret.password, sslmode='disable', connect_timeout=3)
    with psycopg.connect(**settings, autocommit=True) as admin:
        verify_live(admin, identity, parent, expected_system_identifier=expected_system_identifier)
        admin.execute("SET statement_timeout='3s'")
        admin.execute("SET lock_timeout='1s'")
        admin.execute('SELECT pg_advisory_lock(%s)', (TEST_LOCK,))
        roles = check_roles(admin, sidecar, allow_missing=True)
        if not existing and any(roles.values()): raise ProvisionError('Unowned sidecar roles')
        for r in ROLES:
            if roles[r] is None and _credential_exists(sidecar['credential_targets'][r]):
                raise ProvisionError('Unowned credential collision')
        # All identity/ownership/role gates completed before filesystem, credential or SQL writes.
        if not existing:
            output.mkdir(exist_ok=True)
            with identity_file.open('x', encoding='utf-8') as handle: json.dump(sidecar, handle, ensure_ascii=False, indent=2)
        with admin.transaction():
            for r in ROLES:
                credential = _credential(sidecar['credential_targets'][r], sidecar['roles'][r], create=roles[r] is None)
                role = sql.Identifier(sidecar['roles'][r])
                if roles[r] is None:
                    admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {} CONNECTION LIMIT 20').format(role,sql.Literal(credential.password)))
                    admin.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(role,sql.Literal('oilwell-inversion-test:'+parent['fixture_id'])))
                admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(TEST_DATABASE),role))
        if not config_file.exists():
            content = '\n\n'.join('['+s+']\n'+'\n'.join(k+' = '+json.dumps(v,ensure_ascii=False) for k,v in values.items()) for s,values in config_sections(sidecar).items())+'\n'
            with config_file.open('x',encoding='utf-8') as handle: handle.write(content)
    return dict(status='prepared', database=TEST_DATABASE, host='127.0.0.1', port=3822, created=not existing)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-system-identifier', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(provision(expected_system_identifier=args.expected_system_identifier)))
        return 0
    except Exception:
        print('{"status":"refused","message":"Pinned inversion sidecar gate failed"}',file=sys.stderr)
        return 1


if __name__ == '__main__': raise SystemExit(main())
