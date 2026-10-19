"""Only newly owned loopback installations may receive testpack traffic."""
from copy import deepcopy
from datetime import datetime,timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from .contracts import LabHandle
from .storage import load_pack, safe_path, strict_json, generator_fingerprint
from tools import review_deploy as deploy
from tools.review_deploy_config import deployment_fixture, verify_config_hashes
from tools.review_deploy_paths import private_acl, write_json_private, replace_json_private, verify_target
from tools.review_deploy_process import port_available, start_services, stop_services


DEFAULT_PORTS = {'monitoring': {'http': 18774, 'postgres': 56432, 'mqtt': 19884},
                 'review': {'http': 18770, 'postgres': 56433, 'mqtt': 19885}, 'modbus': 15020}
PROTECTED_PORTS = {8765, 8770, 8772, 8774, 1884, 1885, 1888, 55432, 55433, 55436}


def validate_listener_port(port):
    if type(port) is not int or not 0<=port<=65535 or port in PROTECTED_PORTS:
        raise ValueError('unprotected loopback port required')
    return port


def validate_lab_ports(ports, *, probe=True):
    if not isinstance(ports, dict) or set(ports) != {'monitoring', 'review', 'modbus'}:
        raise ValueError('exact loopback lab ports required')
    values = [ports['modbus']]
    for profile in ('monitoring', 'review'):
        if not isinstance(ports[profile], dict) or set(ports[profile]) != {'http', 'postgres', 'mqtt'}:
            raise ValueError('exact component ports required')
        values.extend(ports[profile].values())
    if any(type(v) is not int or not 1024 <= v <= 65535 or v in PROTECTED_PORTS for v in values):
        raise ValueError('protected or invalid port')
    if len(set(values)) != 7 or (probe and not all(port_available(v) for v in values)):
        raise ValueError('all seven loopback ports must be distinct and available')
    return deepcopy(ports)


def validate_lab_target(target):
    target = safe_path(Path(target), missing=True)
    if target.exists() or target == Path(target.anchor) or target == Path.home():
        raise ValueError('new dedicated target required')
    for parent in target.parents:
        if any((parent / name).exists() for name in ('.git', 'private/marker.json', 'private/lab.json')):
            raise ValueError('target cannot be nested in a repository or owned installation')
    return target


def initialize_lab(target, *, pack, source, ports):
    pack = load_pack(pack.root)
    source = safe_path(Path(source))
    target = validate_lab_target(target)
    ports = validate_lab_ports(ports)
    lab_id = str(uuid4())
    manifest_sha = sha256((pack.root / 'manifest.json').read_bytes()).hexdigest()
    marker = {'schema': 'owp.testpack.lab/1', 'lab_id': lab_id, 'root': str(target),
              'state': 'initializing', 'pack_id': pack.manifest['pack_id'], 'pack_sha256': manifest_sha,
              'ports': ports, 'instances': {}}
    target.mkdir(); private_acl(target)
    (target / 'private').mkdir(); private_acl(target / 'private')
    write_json_private(target / 'private/lab.json', marker)
    fixture = {'schema': 'owp.testpack.fixture/1', 'lab_id': lab_id,
               'pack_id': marker['pack_id'], 'pack_sha256': manifest_sha, 'gateway_id': 'REVIEW-GW',
               'device_ids': ['TESTPACK-PUMP-001', 'TESTPACK-PUMP-002'], 'modbus_port': ports['modbus']}
    for profile in ('monitoring', 'review'):
        result = deploy.initialize(target / profile, source=source, ports=ports[profile], profile=profile, fixture=fixture)
        marker['instances'][profile] = result['instance_id']
        replace_json_private(target / 'private/lab.json', marker)
    marker['state'] = 'ready'
    replace_json_private(target / 'private/lab.json', marker)
    return load_lab(target)


def load_lab(root):
    root = safe_path(Path(root))
    path = safe_path(root / 'private/lab.json')
    if path.stat().st_size > 16384:
        raise ValueError('lab marker too large')
    marker = strict_json(path.read_bytes())
    if (set(marker) != {'schema', 'lab_id', 'root', 'state', 'pack_id', 'pack_sha256', 'ports', 'instances'}
            or marker['schema'] != 'owp.testpack.lab/1' or marker['root'] != str(root)
            or marker['state'] != 'ready' or set(marker['instances']) != {'monitoring', 'review'}):
        raise ValueError('unowned or incomplete lab')
    UUID(marker['lab_id'])
    ports = validate_lab_ports(marker['ports'], probe=False)
    for profile in ('monitoring', 'review'):
        child = root / profile
        declaration = verify_target(child, expected_ports=ports[profile])
        if declaration['instance_id'] != marker['instances'][profile] or declaration['profile'] != profile:
            raise ValueError('child installation ownership mismatch')
        verify_config_hashes(child, declaration.get('config_hashes'))
        fixture = deployment_fixture(child)
        if fixture is None or any(fixture[k] != marker[k] for k in ('lab_id', 'pack_id', 'pack_sha256')) or fixture['modbus_port'] != ports['modbus']:
            raise ValueError('child fixture differs from parent lab')
    return LabHandle(root, marker['lab_id'], root / 'monitoring', root / 'review', ports)


def checked_handle(lab):
    loaded = load_lab(lab.root)
    if loaded != lab:
        raise ValueError('lab handle differs from owned marker')
    return loaded


def checked_pack(lab,pack):
    checked_handle(lab)
    pack=load_pack(pack.root)
    declaration=deployment_fixture(lab.monitoring_root)
    if (pack.manifest['pack_id']!=declaration['pack_id'] or
            sha256((pack.root/'manifest.json').read_bytes()).hexdigest()!=declaration['pack_sha256']):
        raise ValueError('pack differs from initialized lab')
    return pack


def run_context(lab,pack):
    return {'run_id':str(uuid4()),'lab_id':lab.lab_id,'pack_id':pack.manifest['pack_id'],
        'pack_sha256':sha256((pack.root/'manifest.json').read_bytes()).hexdigest(),
        'started_at':datetime.now(timezone.utc).isoformat(),'runner':generator_fingerprint(),
        'deployment_commits':{p:deploy._marker(lab.root/p)['source']['commit'] for p in ('monitoring','review')}}


def finish_context(root):
    write_json_private(root/'finished.json',{'finished_at':datetime.now(timezone.utc).isoformat()})


def component_root(lab, profile):
    checked_handle(lab)
    if profile not in ('monitoring', 'review'):
        raise ValueError('unknown lab profile')
    return lab.root / profile


def stop_owned_component(lab, profile, component):
    if component not in ('http', 'center'):
        raise ValueError('only HTTP or center may be individually stopped')
    root, marker = deploy._checked(component_root(lab, profile))
    if marker['state'] != 'running' or component not in marker['processes']:
        raise ValueError('component not recorded as running')
    stop_services(root, {component: marker['processes'][component]})
    del marker['processes'][component]
    deploy._update(root, marker)


def start_owned_component(lab, profile, component):
    if component not in ('http', 'center'):
        raise ValueError('only HTTP or center may be individually started')
    root, marker = deploy._checked(component_root(lab, profile))
    if marker['state'] != 'running' or component in marker['processes']:
        raise ValueError('component must be stopped first')
    def record(name, identity):
        marker['processes'][name] = identity
        deploy._update(root, marker)
    start_services(root, ports=marker['ports'], credentials=deploy._credential_file(root),
                   profile=profile, components={component}, on_start=record)


def restart_owned_component(lab, profile, component):
    stop_owned_component(lab, profile, component)
    start_owned_component(lab, profile, component)


def stop_lab(lab):
    checked_handle(lab)
    # Each child performs exact PID and database system-identifier checks itself.
    for root in (lab.review_root, lab.monitoring_root):
        deploy.stop(root)
