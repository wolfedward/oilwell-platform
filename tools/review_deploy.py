"""Owned single-host review deployment. Run as: python -m tools.review_deploy."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import uuid

from .review_deploy_backup import count_business_rows, dump_database, restore_database, seal_backup, snapshot_identity, verify_backup
from .review_deploy_config import (PG_OWNERS, PG_RUNTIME, MQTT_ACCOUNTS, GATEWAY,
                                   broker_configuration, config_hashes, verify_config_hashes,
                                   runtime_secrets, write_configs)
from .review_deploy_config import pg_runtime, mqtt_accounts, checked_profile, deployment_profile, checked_fixture, deployment_fixture
from .review_deploy_paths import claim_target, private_acl, replace_json_private, safe_target, verify_target, write_json_private
from .review_deploy_pg import (cluster_running, connection_settings, initialize_cluster,
                               probe_system_identifier, start_cluster, stop_cluster)
from .review_deploy_process import inspect_pid, owned_process, port_available, require_free_ports, start_services, stop_services
from .review_deploy_ready import COMPONENTS, readiness_complete, wait_for_readiness
from .review_deploy_source import export_committed_source, run_exported_backend, verify_export


BASE_PYTHON = Path('C:/Users/hp/AppData/Local/Programs/Python/Python313/python.exe')
MOSQUITTO_PASSWD = Path('C:/Program Files/Mosquitto/mosquitto_passwd.exe')
DEFAULT_SOURCE = Path(__file__).resolve().parent.parent


def _ports(http, postgres, mqtt):
    return {'http': http, 'postgres': postgres, 'mqtt': mqtt}


def _credential_file(root):
    path = Path(root) / 'private' / 'credentials.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError('private credential file unavailable')
    credentials = json.loads(path.read_text(encoding='utf-8'))
    if set(credentials) != set((*PG_OWNERS, *runtime_secrets(_marker(root).get('profile', 'review'), fixture=deployment_fixture(root)))):
        raise ValueError('private credential inventory mismatch')
    return credentials


def _new_credentials(profile='review', *, fixture=None):
    return {name: secrets.token_urlsafe(32) for name in (*PG_OWNERS, *runtime_secrets(profile, fixture=fixture))}


def _marker(root):
    return verify_target(root)


def _update(root, marker):
    replace_json_private(Path(root) / 'private' / 'marker.json', marker)


def _private_directories(root):
    for relative in ('logs', 'backups', 'private/inversion-staging'):
        path = Path(root) / relative
        path.mkdir()
        private_acl(path)


def _venv(root):
    if not BASE_PYTHON.is_file():
        raise RuntimeError('base Python unavailable')
    root = Path(root)
    python = root / 'venv' / 'Scripts' / 'python.exe'
    if python.exists():
        raise ValueError('dedicated environment already exists')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    with (root / 'logs' / 'venv-install.log').open('ab') as output:
        create = subprocess.run([str(BASE_PYTHON), '-m', 'venv', str(root / 'venv')],
                                stdout=output, stderr=subprocess.STDOUT, creationflags=flags, timeout=120)
        if create.returncode:
            raise RuntimeError('dedicated environment creation failed')
        install = subprocess.run([str(python), '-m', 'pip', 'install', '--no-input',
                                  '--disable-pip-version-check', '--only-binary=:all:',
                                  '-r', str(root / 'code' / 'requirements-production.txt')],
                                 stdout=output, stderr=subprocess.STDOUT, creationflags=flags, timeout=600)
        if install.returncode:
            raise RuntimeError('pinned dependencies installation failed')


def _broker_passwords(root, credentials, profile='review', *, fixture=None):
    if not MOSQUITTO_PASSWD.is_file():
        raise RuntimeError('Mosquitto password tool unavailable')
    password_file = Path(root) / 'private' / 'mqtt.passwd'
    with password_file.open('x', encoding='utf-8', newline='\n') as stream:
        for name in mqtt_accounts(profile, fixture=fixture):
            stream.write(f'review_{name}:{credentials[name]}\n')
    private_acl(password_file)
    result = subprocess.run([str(MOSQUITTO_PASSWD), '-U', str(password_file)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('private broker password hashing failed')


def _validate_configs(root):
    return run_exported_backend(root, 'validate_configs')


def _probe_database(root):
    return run_exported_backend(root, 'probe_database')


def _preflight(target, source, ports):
    source = Path(source).resolve(strict=True)
    target = safe_target(target, forbidden=(source, Path.home()))
    if source in target.parents:
        raise ValueError('deployment target cannot be inside release workspace')
    require_free_ports(ports)
    return target, source


def initialize(target, *, source=DEFAULT_SOURCE, ports=None, profile='review', fixture=None):
    checked_profile(profile)
    checked_fixture(fixture)
    ports = ports or _ports(8770, 55432, 1884)
    if fixture is not None:
        from .testpack.lab import PROTECTED_PORTS
        if any(port in PROTECTED_PORTS or port == fixture['modbus_port'] for port in ports.values()):
            raise ValueError('fixture requires separate non-protected ports')
    target, source = _preflight(target, source, ports)
    marker = claim_target(target, forbidden=(source, Path.home()))
    marker['ports'] = ports
    marker['profile'] = profile
    write_json_private(target / 'private' / 'marker.json', marker)
    _private_directories(target)
    source_record = export_committed_source(source, target / 'code')
    marker['source'] = source_record
    _update(target, marker)
    _venv(target)
    extra = {} if fixture is None else {'fixture': fixture}
    credentials = _new_credentials(profile, **extra)
    write_json_private(target / 'private' / 'credentials.json', credentials)
    accounts = run_exported_backend(target, 'identity_init')
    source_ref = str(uuid.uuid4())
    write_configs(target, ports=ports, credentials={name: credentials[name] for name in runtime_secrets(profile, **extra)},
                  source_ref=source_ref, profile=profile, **extra)
    broker_configuration(target, mqtt_port=ports['mqtt'], profile=profile, **extra)
    _broker_passwords(target, credentials, profile, **extra)
    marker['config_hashes'] = config_hashes(target)
    _validate_configs(target)
    system_id = initialize_cluster(target, port=ports['postgres'], credentials=credentials, profile=profile)
    run_exported_backend(target, 'install_schemas', timeout=600)
    _probe_database(target)
    stop_cluster(target)
    marker.update(state='ready', system_id=system_id, identity_instance_id=accounts['instance_id'],
                  source_ref=source_ref, processes={})
    _update(target, marker)
    return {'state': 'ready', 'root': str(target), 'instance_id': marker['instance_id'],
            'source_commit': source_record['commit'], 'ports': ports, 'profile': profile}


def _checked(root, *, require_ready=True, verify_code=True, verify_identity=True,
             verify_config=True):
    root = Path(root).absolute()
    marker = _marker(root)
    if require_ready and marker['state'] not in {'ready', 'running', 'stopped', 'starting', 'start_failed'}:
        raise ValueError('deployment initialization incomplete')
    if verify_code:
        verify_export(root / 'code', marker['source'])
    if verify_config:
        verify_config_hashes(root, marker.get('config_hashes'))
        deployment_profile(root)
    if verify_identity:
        identity = run_exported_backend(root, 'identity_check')
        if identity['instance_id'] != marker['identity_instance_id']:
            raise ValueError('identity instance mismatch')
    return root, marker


def status(root):
    root, marker = _checked(root)
    processes = {}
    for name, expected in marker.get('processes', {}).items():
        actual = inspect_pid(expected['pid'])
        processes[name] = 'running' if owned_process(expected, actual) else 'missing' if actual is None else 'mismatch'
    return {'state': marker['state'], 'root': str(root), 'instance_id': marker['instance_id'],
            'source_commit': marker['source']['commit'], 'ports': marker['ports'], 'profile': marker.get('profile', 'review'),
            'postgres': 'running' if cluster_running(root) else 'stopped', 'processes': processes}


def launch(root):
    root, marker = _checked(root)
    if marker['state'] not in {'ready', 'stopped'}:
        raise ValueError('deployment must be stopped before launch')
    ports = marker['ports']
    if not port_available(ports['http']) or not port_available(ports['mqtt']):
        raise ValueError('review service port occupied')
    credentials = _credential_file(root)
    if not cluster_running(root):
        if not port_available(ports['postgres']):
            raise ValueError('review PostgreSQL port occupied')
        start_cluster(root, port=ports['postgres'])
    if probe_system_identifier(ports['postgres'], credentials) != marker['system_id']:
        raise ValueError('PostgreSQL cluster identity mismatch')
    _validate_configs(root)
    _probe_database(root)
    marker['state'] = 'starting'
    marker['processes'] = {}
    marker['launch_started_at'] = datetime.now(timezone.utc).isoformat()
    center_log = root / 'logs' / 'center.log'
    marker['center_log_offset'] = center_log.stat().st_size if center_log.is_file() else 0
    for component in ('wave_assembler', 'inversion'):
        log = root / 'logs' / (component + '.log')
        marker[component + '_log_offset'] = log.stat().st_size if log.is_file() else 0
    _update(root, marker)
    def on_start(name, identity):
        marker['processes'][name] = identity
        _update(root, marker)
    evidence = {name: False for name in COMPONENTS}
    try:
        start_services(root, ports=ports, credentials=credentials, on_start=on_start, profile=marker.get('profile', 'review'))
        observed = status(root)
        if (not observed['processes'] or any(value != 'running' for value in observed['processes'].values())
                or port_available(ports['mqtt'])):
            raise RuntimeError('one or more review services exited during launch')
        evidence = wait_for_readiness(root, marker, timeout=60)
        if not readiness_complete(evidence):
            raise RuntimeError('bounded review component readiness was not established')
    except Exception:
        _readiness_receipt(root, marker, evidence, ok=False)
        marker['state'] = 'start_failed'
        _update(root, marker)
        raise
    _readiness_receipt(root, marker, evidence, ok=True)
    marker['state'] = 'running'
    _update(root, marker)
    return status(root)


def _readiness_receipt(root, marker, evidence, *, ok):
    name = 'readiness-' + uuid.uuid4().hex + '.json'
    write_json_private(Path(root) / 'private' / name,
                       {'format': 1, 'instance_id': marker['instance_id'],
                        'launch_started_at': marker['launch_started_at'],
                        'recorded_at': datetime.now(timezone.utc).isoformat(),
                        'ok': ok, 'evidence': {key: evidence.get(key) is True for key in sorted(COMPONENTS)}})
    marker['readiness_receipt'] = name


def stop(root):
    root, marker = _checked(root, verify_code=False, verify_identity=False, verify_config=False)
    if marker['state'] in {'running', 'starting', 'start_failed'}:
        stop_services(root, marker.get('processes', {}))
    elif marker['state'] not in {'ready', 'stopped'}:
        raise ValueError('deployment state cannot be stopped')
    credentials = _credential_file(root)
    if cluster_running(root):
        if probe_system_identifier(marker['ports']['postgres'], credentials) != marker['system_id']:
            raise ValueError('PostgreSQL cluster identity mismatch')
        stop_cluster(root)
    marker['state'] = 'stopped'
    marker['processes'] = {}
    _update(root, marker)
    return {'state': 'stopped', 'root': str(root), 'instance_id': marker['instance_id'],
            'ports': marker['ports'], 'processes': {}}


def backup(root):
    root, marker = _checked(root)
    if deployment_fixture(root) is not None:
        raise ValueError('testpack fixture backup is not supported')
    # This explicit command establishes quiescence before combining stores.
    if marker['state'] != 'stopped':
        stop(root)
        marker = _marker(root)
    if marker['processes']:
        raise ValueError('writer processes remain recorded')
    credentials = _credential_file(root)
    if not port_available(marker['ports']['postgres']):
        raise ValueError('snapshot PostgreSQL port occupied')
    start_cluster(root, port=marker['ports']['postgres'])
    if probe_system_identifier(marker['ports']['postgres'], credentials) != marker['system_id']:
        raise ValueError('snapshot PostgreSQL cluster mismatch')
    folder = root / 'backups' / str(uuid.uuid4())
    folder.mkdir()
    private_acl(folder)
    try:
        dump_database(folder, port=marker['ports']['postgres'], password=credentials['bootstrap'])
        snapshot_identity(root / 'private' / 'identity.db', folder / 'identity.sqlite')
        shutil.copyfile(root / 'private' / 'accounts.json', folder / 'accounts.json')
        private_acl(folder / 'accounts.json')
        counts = count_business_rows(port=marker['ports']['postgres'], password=credentials['bootstrap'])
        seal_backup(folder, instance_id=marker['identity_instance_id'], source=marker['source'],
                    system_id=marker['system_id'], source_ref=marker['source_ref'], row_counts=counts,
                    profile=marker.get('profile', 'review'))
        verify_backup(folder)
    finally:
        stop_cluster(root)
    return {'backup': str(folder), 'instance_id': marker['instance_id'],
            'source_commit': marker['source']['commit'], 'profile': marker.get('profile', 'review'), 'row_counts': counts}


def restore(backup_path, *, target, source=DEFAULT_SOURCE, ports=None):
    ports = ports or _ports(8771, 55433, 1885)
    target, source = _preflight(target, source, ports)
    backup_manifest = verify_backup(backup_path)
    if backup_manifest.get('fixture') is not None:
        raise ValueError('testpack fixture restore is not supported')
    profile = checked_profile(backup_manifest.get('profile', 'review'))
    marker = claim_target(target, forbidden=(source, Path.home()))
    marker['ports'] = ports
    marker['profile'] = profile
    write_json_private(target / 'private' / 'marker.json', marker)
    _private_directories(target)
    source_record = export_committed_source(source, target / 'code')
    if source_record != backup_manifest['source']:
        raise ValueError('restore source commit or file hashes differ')
    marker['source'] = source_record
    _update(target, marker)
    _venv(target)
    credentials = _new_credentials(profile)
    write_json_private(target / 'private' / 'credentials.json', credentials)
    shutil.copyfile(Path(backup_path) / 'identity.sqlite', target / 'private' / 'identity.db')
    private_acl(target / 'private' / 'identity.db')
    shutil.copyfile(Path(backup_path) / 'accounts.json', target / 'private' / 'accounts.json')
    private_acl(target / 'private' / 'accounts.json')
    identity = run_exported_backend(target, 'identity_check')
    if identity['instance_id'] != backup_manifest['instance_id']:
        raise ValueError('restored identity mismatch')
    write_configs(target, ports=ports, credentials={name: credentials[name] for name in runtime_secrets(profile)},
                  source_ref=backup_manifest['source_ref'], profile=profile)
    broker_configuration(target, mqtt_port=ports['mqtt'], profile=profile)
    _broker_passwords(target, credentials, profile)
    marker['config_hashes'] = config_hashes(target)
    _validate_configs(target)
    system_id = initialize_cluster(target, port=ports['postgres'], credentials=credentials, profile=profile)
    restore_database(backup_path, port=ports['postgres'], password=credentials['bootstrap'])
    counts = count_business_rows(port=ports['postgres'], password=credentials['bootstrap'])
    if counts != backup_manifest['row_counts']:
        raise ValueError('restored PostgreSQL row counts differ')
    _probe_database(target)
    stop_cluster(target)
    marker.update(state='ready', system_id=system_id, identity_instance_id=identity['instance_id'],
                  source_ref=backup_manifest['source_ref'], processes={},
                  restored_from={'instance_id': backup_manifest['instance_id'],
                                 'system_id': backup_manifest['system_id']})
    _update(target, marker)
    return {'state': 'ready', 'root': str(target), 'instance_id': marker['instance_id'],
            'source_commit': source_record['commit'], 'ports': ports, 'profile': profile, 'row_counts': counts}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Owned loopback review installation')
    sub = parser.add_subparsers(dest='command', required=True)
    for action in ('init', 'restore'):
        command = sub.add_parser(action)
        command.add_argument('--target', required=True, type=Path)
        command.add_argument('--source', default=DEFAULT_SOURCE, type=Path)
        command.add_argument('--http-port', type=int, default=8770 if action == 'init' else 8771)
        command.add_argument('--postgres-port', type=int, default=55432 if action == 'init' else 55433)
        command.add_argument('--mqtt-port', type=int, default=1884 if action == 'init' else 1885)
        if action == 'restore':
            command.add_argument('--backup', required=True, type=Path)
        else:
            command.add_argument('--profile', choices=('review', 'monitoring'), default='review')
    for action in ('launch', 'stop', 'status', 'backup'):
        sub.add_parser(action).add_argument('--target', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in {'init', 'restore'}:
            ports = _ports(args.http_port, args.postgres_port, args.mqtt_port)
            result = (initialize(args.target, source=args.source, ports=ports, profile=args.profile) if args.command == 'init'
                      else restore(args.backup, target=args.target, source=args.source, ports=ports))
        else:
            result = {'launch': launch, 'stop': stop, 'status': status, 'backup': backup}[args.command](args.target)
        print(json.dumps({'ok': True, **result}, ensure_ascii=False))
        return 0
    except Exception as error:
        # psycopg/OS exceptions may embed a connection string, path or secret.
        print(json.dumps({'ok': False, 'code': type(error).__name__,
                          'message': 'review operation refused or failed; inspect private target artifacts'},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
