"""Private config generation for one loopback engineering review instance."""
import json
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


def runtime_secrets():
    return (*PG_RUNTIME, *MQTT_ACCOUNTS)


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


def write_configs(root, *, ports, credentials, source_ref):
    """Create configs once; no inline passwords and no changes to existing files."""
    root = Path(root).absolute()
    if set(credentials) != set(runtime_secrets()) or not all(credentials.values()):
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
    return paths


def broker_configuration(root, *, mqtt_port):
    root = Path(root).absolute()
    private = root / 'private'
    lines = ['allow_anonymous false', f'listener {mqtt_port} 127.0.0.1',
             f'password_file {private / "mqtt.passwd"}', f'acl_file {private / "mqtt.acl"}',
             'persistence false', 'log_dest file ' + str(root / 'logs' / 'mqtt.log'),
             'message_size_limit 262144', 'max_packet_size 270336']
    acl = ('user review_mqtt_center\ntopic read oilfield/v1/+/+/telemetry\n'
           'user review_mqtt_receipts\ntopic write oilfield/v2/+/+/ingest-ack\n'
           'user review_mqtt_wave_receive\ntopic read oilfield/v2/REVIEW-GW/+/waveform\n'
           'user review_mqtt_wave_publish\ntopic write oilfield/v2/REVIEW-GW/+/waveform-ack\n')
    _write(private / 'mosquitto.conf', '\n'.join(lines) + '\n')
    _write(private / 'mqtt.acl', acl)
    return private / 'mosquitto.conf'
