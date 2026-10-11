"""Owned single-host review deployment. Run as: python -m tools.review_deploy."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import uuid

import psycopg

from .review_deploy_backup import count_business_rows, dump_database, restore_database, seal_backup, snapshot_identity, verify_backup
from .review_deploy_config import (PG_OWNERS, PG_RUNTIME, MQTT_ACCOUNTS, GATEWAY,
                                   broker_configuration, env_name, runtime_secrets, write_configs)
from .review_deploy_paths import claim_target, private_acl, replace_json_private, safe_target, verify_target, write_json_private
from .review_deploy_pg import (cluster_running, connection_settings, initialize_cluster,
                               install_schemas, probe_system_identifier, start_cluster, stop_cluster)
from .review_deploy_process import inspect_pid, owned_process, port_available, require_free_ports, start_services, stop_services
from .review_deploy_source import export_committed_source, verify_export


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
    if set(credentials) != set((*PG_OWNERS, *PG_RUNTIME, *MQTT_ACCOUNTS)):
        raise ValueError('private credential inventory mismatch')
    return credentials


def _new_credentials():
    return {name: secrets.token_urlsafe(32) for name in (*PG_OWNERS, *PG_RUNTIME, *MQTT_ACCOUNTS)}


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


def _broker_passwords(root, credentials):
    if not MOSQUITTO_PASSWD.is_file():
        raise RuntimeError('Mosquitto password tool unavailable')
    password_file = Path(root) / 'private' / 'mqtt.passwd'
    with password_file.open('x', encoding='utf-8', newline='\n') as stream:
        for name in MQTT_ACCOUNTS:
            stream.write(f'review_{name}:{credentials[name]}\n')
    private_acl(password_file)
    result = subprocess.run([str(MOSQUITTO_PASSWD), '-U', str(password_file)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('private broker password hashing failed')


def _validate_configs(root, credentials):
    from backend.center.config import load_center_config, load_query_postgres_config
    from backend.registry.config import load_registry_config
    from backend.ingest_reliability.config import load_reliability_config
    from backend.waveforms.config import load_waveform_config
    from backend.inversion.config import load_inversion_config
    private = Path(root) / 'private'
    previous = os.environ.copy()
    try:
        os.environ.update({env_name(role): credentials[role] for role in runtime_secrets()})
        center = load_center_config(private / 'center.toml')
        query = load_query_postgres_config(private / 'center.toml')
        registry = load_registry_config(private / 'center.toml')
        reliability = {role: load_reliability_config(private / 'center.toml', component=role)
                       for role in ('ingestor', 'publisher', 'management')}
        waves = {role: load_waveform_config(private / 'waveform.toml', component=role)
                 for role in ('receive', 'assemble', 'publish', 'management')}
        inversion = load_inversion_config(private / 'inversion.toml')
        if not all((center, query, registry, inversion, *reliability.values(), *waves.values())):
            raise ValueError('review component config disabled')
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _probe_database(port, credentials):
    """Read-only effective capabilities, schema and extension checks."""
    from backend.registry.runtime import probe_privileges
    from backend.ingest_reliability.privileges import probe_role as reliability_probe
    from backend.waveforms.privileges import probe_role as waveform_probe
    from backend.inversion.privileges import probe_role as inversion_probe
    from backend.inversion.metadata import probe_metadata
    def connect(role):
        return lambda: psycopg.connect(**connection_settings(port, role, credentials[role]))
    probe_privileges(connect('registry_writer'), connect('foundation_reader'))
    for role in ('ingestor', 'publisher', 'reader', 'operator'):
        reliability_probe(connect('reliable_' + role), role)
    for role, name in (('receiver', 'wave_receiver'), ('assembler', 'wave_assembler'),
                       ('receipt_publisher', 'wave_publisher'), ('reader', 'wave_reader'),
                       ('operator', 'wave_operator')):
        waveform_probe(connect(name), role)
    for role in ('reader', 'operator'):
        inversion_probe(connect('inversion_' + role), role)
        probe_metadata(connect('inversion_' + role))


def _preflight(target, source, ports):
    source = Path(source).resolve(strict=True)
    target = safe_target(target, forbidden=(source, Path.home()))
    if source in target.parents:
        raise ValueError('deployment target cannot be inside release workspace')
    require_free_ports(ports)
    return target, source


def initialize(target, *, source=DEFAULT_SOURCE, ports=None):
    ports = ports or _ports(8770, 55432, 1884)
    target, source = _preflight(target, source, ports)
    marker = claim_target(target, forbidden=(source, Path.home()))
    marker['ports'] = ports
    write_json_private(target / 'private' / 'marker.json', marker)
    _private_directories(target)
    source_record = export_committed_source(source, target / 'code')
    marker['source'] = source_record
    _update(target, marker)
    _venv(target)
    credentials = _new_credentials()
    write_json_private(target / 'private' / 'credentials.json', credentials)
    from backend.review_runtime import initialize_identity
    accounts = initialize_identity(target / 'private' / 'identity.db')
    private_acl(target / 'private' / 'identity.db')
    write_json_private(target / 'private' / 'accounts.json', accounts)
    source_ref = str(uuid.uuid4())
    write_configs(target, ports=ports, credentials={name: credentials[name] for name in runtime_secrets()},
                  source_ref=source_ref)
    broker_configuration(target, mqtt_port=ports['mqtt'])
    _broker_passwords(target, credentials)
    _validate_configs(target, credentials)
    system_id = initialize_cluster(target, port=ports['postgres'], credentials=credentials)
    install_schemas(port=ports['postgres'], credentials=credentials,
                    center_config=target / 'private' / 'center.toml',
                    waveform_config=target / 'private' / 'waveform.toml',
                    inversion_config=target / 'private' / 'inversion.toml')
    _probe_database(ports['postgres'], credentials)
    stop_cluster(target)
    marker.update(state='ready', system_id=system_id, identity_instance_id=accounts['instance_id'],
                  source_ref=source_ref, processes={})
    _update(target, marker)
    return {'state': 'ready', 'root': str(target), 'instance_id': marker['instance_id'],
            'source_commit': source_record['commit'], 'ports': ports}


def _checked(root, *, require_ready=True, verify_code=True, verify_identity=True):
    root = Path(root).absolute()
    marker = _marker(root)
    if require_ready and marker['state'] not in {'ready', 'running', 'stopped', 'starting', 'start_failed'}:
        raise ValueError('deployment initialization incomplete')
    if verify_code:
        verify_export(root / 'code', marker['source'])
    if verify_identity:
        from backend.review_runtime import check_identity
        identity = check_identity(root / 'private' / 'identity.db')
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
            'source_commit': marker['source']['commit'], 'ports': marker['ports'],
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
    _validate_configs(root, credentials)
    _probe_database(ports['postgres'], credentials)
    marker['state'] = 'starting'
    marker['processes'] = {}
    _update(root, marker)
    def on_start(name, identity):
        marker['processes'][name] = identity
        _update(root, marker)
    try:
        start_services(root, ports=ports, credentials=credentials, on_start=on_start)
        observed = status(root)
        if (not observed['processes'] or any(value != 'running' for value in observed['processes'].values())
                or port_available(ports['mqtt'])):
            raise RuntimeError('one or more review services exited during launch')
    except Exception:
        marker['state'] = 'start_failed'
        _update(root, marker)
        raise
    marker['state'] = 'running'
    _update(root, marker)
    return status(root)


def stop(root):
    root, marker = _checked(root, verify_code=False, verify_identity=False)
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
                    system_id=marker['system_id'], source_ref=marker['source_ref'], row_counts=counts)
        verify_backup(folder)
    finally:
        stop_cluster(root)
    return {'backup': str(folder), 'instance_id': marker['instance_id'],
            'source_commit': marker['source']['commit']}


def restore(backup_path, *, target, source=DEFAULT_SOURCE, ports=None):
    ports = ports or _ports(8771, 55433, 1885)
    target, source = _preflight(target, source, ports)
    backup_manifest = verify_backup(backup_path)
    marker = claim_target(target, forbidden=(source, Path.home()))
    marker['ports'] = ports
    write_json_private(target / 'private' / 'marker.json', marker)
    _private_directories(target)
    source_record = export_committed_source(source, target / 'code')
    if source_record != backup_manifest['source']:
        raise ValueError('restore source commit or file hashes differ')
    marker['source'] = source_record
    _update(target, marker)
    _venv(target)
    credentials = _new_credentials()
    write_json_private(target / 'private' / 'credentials.json', credentials)
    shutil.copyfile(Path(backup_path) / 'identity.sqlite', target / 'private' / 'identity.db')
    private_acl(target / 'private' / 'identity.db')
    shutil.copyfile(Path(backup_path) / 'accounts.json', target / 'private' / 'accounts.json')
    private_acl(target / 'private' / 'accounts.json')
    from backend.review_runtime import check_identity
    identity = check_identity(target / 'private' / 'identity.db')
    if identity['instance_id'] != backup_manifest['instance_id']:
        raise ValueError('restored identity mismatch')
    write_configs(target, ports=ports, credentials={name: credentials[name] for name in runtime_secrets()},
                  source_ref=backup_manifest['source_ref'])
    broker_configuration(target, mqtt_port=ports['mqtt'])
    _broker_passwords(target, credentials)
    _validate_configs(target, credentials)
    system_id = initialize_cluster(target, port=ports['postgres'], credentials=credentials)
    restore_database(backup_path, port=ports['postgres'], password=credentials['bootstrap'])
    counts = count_business_rows(port=ports['postgres'], password=credentials['bootstrap'])
    if counts != backup_manifest['row_counts']:
        raise ValueError('restored PostgreSQL row counts differ')
    _probe_database(ports['postgres'], credentials)
    stop_cluster(target)
    marker.update(state='ready', system_id=system_id, identity_instance_id=identity['instance_id'],
                  source_ref=backup_manifest['source_ref'], processes={},
                  restored_from={'instance_id': backup_manifest['instance_id'],
                                 'system_id': backup_manifest['system_id']})
    _update(target, marker)
    return {'state': 'ready', 'root': str(target), 'instance_id': marker['instance_id'],
            'source_commit': source_record['commit'], 'ports': ports}


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
    for action in ('launch', 'stop', 'status', 'backup'):
        sub.add_parser(action).add_argument('--target', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in {'init', 'restore'}:
            ports = _ports(args.http_port, args.postgres_port, args.mqtt_port)
            result = (initialize(args.target, source=args.source, ports=ports) if args.command == 'init'
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
