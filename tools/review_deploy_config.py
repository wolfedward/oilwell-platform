"""Private config generation for one loopback engineering review instance."""
import json
import hashlib
import tomllib
import re
from uuid import UUID
from pathlib import Path

from .review_deploy_paths import write_json_private, private_acl


PG_RUNTIME = (
    'foundation_ingestor', 'foundation_reader', 'registry_writer',
    'reliable_ingestor', 'reliable_publisher', 'reliable_reader', 'reliable_operator',
    'wave_receiver', 'wave_assembler', 'wave_publisher', 'wave_reader', 'wave_operator',
    'inversion_reader', 'inversion_operator',
)
PG_OWNERS = ('bootstrap', 'foundation_owner', 'registry_owner', 'reliability_owner',
             'waveform_owner', 'inversion_owner')
MQTT_ACCOUNTS = ('mqtt_center', 'mqtt_receipts', 'mqtt_wave_receive', 'mqtt_wave_publish')
DATABASE = 'review_platform'
GATEWAY = 'REVIEW-GW'
PREFIX = 'oilfield'
SEALED_FILES = ('center.toml', 'waveform.toml', 'inversion.toml',
                'mosquitto.conf', 'mqtt.acl', 'mqtt.passwd')


def checked_fixture(fixture):
    if fixture is None:return None
    keys={'schema','lab_id','pack_id','pack_sha256','gateway_id','device_ids','modbus_port'}
    if not isinstance(fixture,dict) or set(fixture)!=keys or fixture['schema']!='owp.testpack.fixture/1':
        raise ValueError('invalid testpack fixture')
    UUID(fixture['lab_id'])
    if (fixture['gateway_id']!='REVIEW-GW' or fixture['device_ids']!=['TESTPACK-PUMP-001','TESTPACK-PUMP-002']
        or not re.fullmatch(r'tp-[0-9a-f]{24}',fixture['pack_id']) or not re.fullmatch(r'[0-9a-f]{64}',fixture['pack_sha256'])
        or type(fixture['modbus_port']) is not int or not 1024<=fixture['modbus_port']<=65535
        or fixture['modbus_port'] in {8765,8770,8772,8774,18882,18883,1884,1885,1888,55432,55433,55436}):
        raise ValueError('unapproved testpack fixture values')
    return fixture


def deployment_fixture(root):
    path=Path(root)/'private/fixture.json'
    if not path.exists():return None
    from .testpack.storage import safe_path,strict_json
    safe_path(path)
    if path.stat().st_size>4096:raise ValueError('fixture too large')
    return checked_fixture(strict_json(path.read_bytes()))


def _sealed_files(root):
    return SEALED_FILES+(('fixture.json','gateway.toml') if deployment_fixture(root) else ())


def config_hashes(root):
    """Seal every generated input consumed by the launched components."""
    private = Path(root) / 'private'
    result = {}
    for name in _sealed_files(root):
        path = private / name
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError('deployment config unavailable or linked')
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def verify_config_hashes(root, expected):
    if not isinstance(expected, dict) or set(expected) != set(_sealed_files(root)):
        raise ValueError('deployment config inventory mismatch')
    if config_hashes(root) != expected:
        raise ValueError('deployment config changed since initialization')


def checked_profile(profile):
    if profile not in ('review', 'monitoring'):
        raise ValueError('unsupported deployment profile')
    return profile


def pg_runtime(profile='review'):
    return PG_RUNTIME + (('monitoring_reader',) if checked_profile(profile) == 'monitoring' else ())


def mqtt_accounts(profile='review',*,fixture=None):
    return MQTT_ACCOUNTS + (('mqtt_monitoring_test',) if checked_profile(profile) == 'monitoring' else ()) + (('mqtt_testpack',) if checked_fixture(fixture) else ())


def runtime_secrets(profile='review',*,fixture=None):
    return (*pg_runtime(profile), *mqtt_accounts(profile,fixture=fixture))


def deployment_profile(root):
    """Legacy sealed configs default to review, without rewriting them."""
    with (Path(root) / 'private/center.toml').open('rb') as stream:
        raw = tomllib.load(stream)
    profile = checked_profile(raw.get('deployment', {}).get('profile', 'review'))
    marker = Path(root) / 'private/marker.json'
    if marker.is_file() and checked_profile(json.loads(marker.read_text(encoding='utf-8')).get('profile', 'review')) != profile:
        raise ValueError('sealed deployment profile differs from marker')
    if (raw.get('monitoring', {}).get('enabled', False) is True) != (profile == 'monitoring'):
        raise ValueError('monitoring profile/config mismatch')
    return profile


def _q(value):
    return json.dumps(value, ensure_ascii=False)


def env_name(role):
    return f'REVIEW_{role.upper()}_PASSWORD'


def _pg_section(section, role, port):
    return (f'[{section}]\nhost = "127.0.0.1"\nport = {port}\n'
            f'database = "{DATABASE}"\nuser = "review_{role}"\n'
            f'password_env = "{env_name(role)}"\nsslmode = "disable"\n\n')


def _mqtt_section(section, role, port, client, *, session=None):
    result = (f'[{section}]\nhost = "127.0.0.1"\nport = {port}\n'
              f'client_id = {_q(client)}\nusername = "review_{role}"\n'
              f'password_env = "{env_name(role)}"\nkeepalive_seconds = 30\n'
              'connect_timeout_seconds = 3\n')
    if session is not None:
        result += f'session_expiry_seconds = {session}\n'
    return result + '\n'


def _write(path, content):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(content)
    private_acl(path)


def write_configs(root, *, ports, credentials, source_ref, profile='review', fixture=None):
    """Create configs once; no inline passwords and no changes to existing files."""
    root = Path(root).absolute()
    checked_fixture(fixture)
    if set(credentials) != set(runtime_secrets(profile,fixture=fixture)) or not all(credentials.values()):
        raise ValueError('complete distinct runtime secrets required')
    if len(set(credentials.values())) != len(credentials):
        raise ValueError('runtime secrets must be distinct')
    private = root / 'private'
    origin = f'http://127.0.0.1:{ports["http"]}'
    center = ('[center]\ntopic_prefix = "oilfield"\n\n'
              + _mqtt_section('mqtt', 'mqtt_center', ports['mqtt'], 'review-center', session=604800)
              + _pg_section('postgres', 'foundation_ingestor', ports['postgres'])
              + _pg_section('postgres_query', 'foundation_reader', ports['postgres'])
              + _pg_section('postgres_registry', 'registry_writer', ports['postgres'])
              + f'[registry]\nenabled = true\nallowed_origins = [{_q(origin)}]\n\n'
              + '[reliability]\nenabled = true\ntotal_bytes = 1073741824\n'
                'quarantine_bytes = 268435456\nreceipt_gateways = ["REVIEW-GW"]\n'
              + f'allowed_origins = [{_q(origin)}]\n\n')
    for section, role in (('postgres_reliable_ingest', 'reliable_ingestor'),
                          ('postgres_receipt_publisher', 'reliable_publisher'),
                          ('postgres_ingest_reader', 'reliable_reader'),
                          ('postgres_ingest_operator', 'reliable_operator')):
        center += _pg_section(section, role, ports['postgres'])
    center += _mqtt_section('mqtt_receipt_publisher', 'mqtt_receipts', ports['mqtt'], 'review-receipts')
    center += 'publish_timeout_seconds = 10\n'
    if profile == 'monitoring':
        center += ('\n[deployment]\nprofile = "monitoring"\n\n'
                   '[monitoring]\nenabled = true\nenvironment = "test"\n'
                   f'source_ref = {_q(source_ref)}\n\n'
                   + _pg_section('postgres_monitoring', 'monitoring_reader', ports['postgres']))

    quota = ('raw_bytes = 268435456\nfacts = 25000\nattachment_bytes = 33554432\n'
             'incomplete_captures = 4\norphan_bytes = 2097152\nquarantine_bytes = 4194304\n'
             'metadata_rows = 250000\norphan_bytes_per_capture = 524288\n')
    global_quota = ('raw_bytes = 1073741824\nfacts = 100000\nattachment_bytes = 134217728\n'
                    'incomplete_captures = 64\norphan_bytes = 33554432\nquarantine_bytes = 16777216\n'
                    'metadata_rows = 1000000\norphan_bytes_per_capture = 524288\n')
    wave = ('[waveforms]\nenabled = true\ntopic_prefix = "oilfield"\n'
            'assembly_workers = 2\nmax_db_connections = 4\n\n'
            '[capacity.global]\n' + global_quota + '\n'
            f'[gateways.{GATEWAY}]\nsource_ref = {_q(source_ref)}\n\n'
            f'[gateways.{GATEWAY}.quota]\n' + quota + '\n'
            + _mqtt_section(f'gateways.{GATEWAY}.mqtt_receive', 'mqtt_wave_receive', ports['mqtt'],
                            'review-wave-receive', session=604800)
            + _mqtt_section(f'gateways.{GATEWAY}.mqtt_publish', 'mqtt_wave_publish', ports['mqtt'],
                            'review-wave-publish', session=0))
    for section, role in (('postgres_receiver', 'wave_receiver'), ('postgres_assembler', 'wave_assembler'),
                          ('postgres_receipt_publisher', 'wave_publisher'), ('postgres_reader', 'wave_reader'),
                          ('postgres_operator', 'wave_operator')):
        wave += _pg_section(section, role, ports['postgres'])
    wave += f'[management]\nshared_accounting_bucket = "{GATEWAY}"\n'

    inversion = ('[inversion]\nenabled = true\n'
                 f'identity_db_path = {_q(str(private / "identity.db"))}\n'
                 f'waveform_config_path = {_q(str(private / "waveform.toml"))}\n'
                 f'temp_root = {_q(str(private / "inversion-staging"))}\n'
                 'total_storage_bytes = 2147483648\n\n'
                 + _pg_section('postgres_reader', 'inversion_reader', ports['postgres'])
                 + _pg_section('postgres_operator', 'inversion_operator', ports['postgres']))
    paths = {'center': private / 'center.toml', 'waveform': private / 'waveform.toml',
             'inversion': private / 'inversion.toml'}
    for key, content in (('center', center), ('waveform', wave), ('inversion', inversion)):
        _write(paths[key], content)
    if fixture is not None:
        write_json_private(private/'fixture.json',fixture)
        _write(private/'gateway.toml',_gateway_fixture_config(root,ports,fixture))
    return paths


def broker_configuration(root, *, mqtt_port, profile='review',fixture=None):
    checked_profile(profile)
    checked_fixture(fixture)
    root = Path(root).absolute()
    private = root / 'private'
    lines = ['allow_anonymous false', f'listener {mqtt_port} 127.0.0.1',
             f'password_file {private / "mqtt.passwd"}', f'acl_file {private / "mqtt.acl"}',
             'persistence false', 'log_dest file ' + str(root / 'logs' / 'mqtt.log'),
             'message_size_limit 262144', 'max_packet_size 270336']
    acl = ('user review_mqtt_center\ntopic read oilfield/v1/+/+/telemetry\n'
           'user review_mqtt_receipts\ntopic write oilfield/v2/+/+/ingest-ack\n'
           'topic write oilfield/review/health/reliability\n'
           'user review_mqtt_wave_receive\ntopic read oilfield/v2/REVIEW-GW/+/waveform\n'
           'user review_mqtt_wave_publish\ntopic write oilfield/v2/REVIEW-GW/+/waveform-ack\n'
           'topic write oilfield/review/health/waveform\n')
    if profile == 'monitoring':
        acl += ('user review_mqtt_monitoring_test\n'
                'topic write oilfield/v1/REVIEW-GW/MONITORING-TEST-1/telemetry\n'
                'topic write oilfield/v1/REVIEW-GW/MONITORING-TEST-2/telemetry\n')
    if fixture is not None:
        acl+='user review_mqtt_testpack\n'
        for device in fixture['device_ids']:
            acl+=f'topic write oilfield/v1/REVIEW-GW/{device}/telemetry\ntopic read oilfield/v2/REVIEW-GW/{device}/ingest-ack\n'
    _write(private / 'mosquitto.conf', '\n'.join(lines) + '\n')
    _write(private / 'mqtt.acl', acl)
    return private / 'mosquitto.conf'


def _gateway_fixture_config(root,ports,fixture):
    from .testpack.history import telemetry_point_map
    text=('[gateway]\nid = "REVIEW-GW"\nconfirmation_mode = "application"\n'
          f'spool_database = {_q(str(root/"private/testpack-spool.db"))}\n'
          'poll_interval_seconds = 1\nreceipt_timeout_seconds = 2\nbase_backoff_seconds = 0.2\nmax_backoff_seconds = 2\n'
          '[gateway.spool_limits]\nmax_messages = 10000\nmax_payload_bytes = 67108864\nmin_free_bytes = 67108864\n\n'
          +_mqtt_section('mqtt','mqtt_testpack',ports['mqtt'],'testpack-'+fixture['lab_id'][:8])
          +'topic_prefix = "oilfield"\npublish_timeout_seconds = 3\n')
    for unit,device in enumerate(fixture['device_ids'],1):
        text+=f'\n[[modbus.devices]]\ndevice_id = {_q(device)}\ntransport = "tcp"\nhost = "127.0.0.1"\nport = {fixture["modbus_port"]}\ntimeout_seconds = 1\nretries = 0\n'
        for point in telemetry_point_map(unit):
            text+=f'\n[[modbus.devices.points]]\npoint_code = {_q(point.point_code)}\nunit_id = {unit}\nfunction_code = 4\naddress = {point.address}\ndata_type = "float32"\nbyte_order = "big"\nword_order = "big"\nunit = {_q(point.unit)}\n'
    return text
