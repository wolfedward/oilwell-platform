"""One-shot backend actions executed by the target's own Python and code tree."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import threading
import uuid


def _credentials(root):
    from .review_deploy_config import PG_OWNERS, runtime_secrets, deployment_profile, deployment_fixture
    path = root / 'private' / 'credentials.json'
    if path.is_symlink() or path.stat().st_nlink != 1:
        raise ValueError('credential file unavailable')
    credentials = json.loads(path.read_text(encoding='utf-8'))
    if set(credentials) != set((*PG_OWNERS, *runtime_secrets(deployment_profile(root), fixture=deployment_fixture(root)))):
        raise ValueError('credential inventory mismatch')
    return credentials


def _with_runtime_env(credentials, action, profile='review', *, fixture=None):
    from .review_deploy_config import env_name, runtime_secrets
    previous = os.environ.copy()
    try:
        os.environ.update({env_name(role): credentials[role] for role in runtime_secrets(profile, fixture=fixture)})
        return action()
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _validate_configs(root, credentials):
    from .review_deploy_config import deployment_profile, deployment_fixture
    profile = deployment_profile(root)
    fixture = deployment_fixture(root)
    from backend.center.config import load_center_config, load_query_postgres_config
    from backend.registry.config import load_registry_config
    from backend.ingest_reliability.config import load_reliability_config
    from backend.waveforms.config import load_waveform_config
    from backend.inversion.config import load_inversion_config
    private = root / 'private'
    def check():
        components = [load_center_config(private / 'center.toml'),
                      load_query_postgres_config(private / 'center.toml'),
                      load_registry_config(private / 'center.toml'),
                      *[load_reliability_config(private / 'center.toml', component=role)
                        for role in ('ingestor', 'publisher', 'management')],
                      *[load_waveform_config(private / 'waveform.toml', component=role)
                        for role in ('receive', 'assemble', 'publish', 'management')],
                      load_inversion_config(private / 'inversion.toml')]
        if not all(components):
            raise ValueError('review component disabled')
        if fixture is not None:
            from backend.gateway.config import load_gateway_config
            gateway = load_gateway_config(private / 'gateway.toml')
            if gateway.confirmation_mode != 'application' or gateway.mqtt.host != '127.0.0.1':
                raise ValueError('fixture gateway configuration mismatch')
        if profile == 'monitoring':
            from backend.monitoring.config import load_monitoring_config
            if load_monitoring_config(private / 'center.toml') is None:
                raise ValueError('monitoring component disabled')
    _with_runtime_env(credentials, check, profile, fixture=fixture)
    return {'validated': True}


def _probe_database(root, credentials):
    import psycopg
    from .review_deploy_pg import connection_settings
    from backend.registry.runtime import probe_privileges
    from backend.ingest_reliability.privileges import probe_role as reliability_probe
    from backend.waveforms.privileges import probe_role as waveform_probe
    from backend.inversion.privileges import probe_role as inversion_probe
    from backend.inversion.metadata import probe_metadata
    port = json.loads((root / 'private' / 'marker.json').read_text(encoding='utf-8'))['ports']['postgres']
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
    from .review_deploy_config import deployment_profile
    if deployment_profile(root) == 'monitoring':
        from backend.monitoring.privileges import probe_reader
        probe_reader(connect('monitoring_reader'))
    return {'probed': True}


def mqtt_reason_ok(reason):
    """MQTT 5 CONNACK success is exactly 0x00."""
    value = getattr(reason, 'value', reason)
    return type(value) is int and value == 0


def mqtt_puback_ok(reason):
    # OASIS MQTT 5, section 3.4.2.1: accepted without matching subscribers
    # (0x10) is also success; never conflate it with the CONNACK contract.
    value = getattr(reason, 'value', reason)
    return type(value) is int and value in (0, 16)


def _receiver_ready(root, credentials):
    import psycopg
    from .review_deploy_config import GATEWAY
    from .review_deploy_pg import connection_settings
    marker = json.loads((root / 'private' / 'marker.json').read_text(encoding='utf-8'))
    started = datetime.fromisoformat(marker['launch_started_at'])
    if started.tzinfo is None:
        return {'ready': False}
    with psycopg.connect(**connection_settings(marker['ports']['postgres'], 'wave_receiver',
                                               credentials['wave_receiver'])) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        row = conn.execute("SELECT last_seen,error_code FROM waveforms.worker_health "
                           "WHERE component='receiver' AND gateway_id=%s", (GATEWAY,)).fetchone()
    return {'ready': bool(row and row[0] and row[0] >= started and row[1] is None)}


def _mqtt_publish_probe(port, *, role, password, topic):
    """Publish only to a private review health topic, requiring accepted PUBACK."""
    import paho.mqtt.client as mqtt
    connected = threading.Event()
    published = threading.Event()
    result = {'connect': False, 'publish': False}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id='review-health-' + uuid.uuid4().hex,
                         protocol=mqtt.MQTTv5, reconnect_on_failure=False)
    client.username_pw_set('review_' + role, password)
    def on_connect(_client, _userdata, _flags, reason, _properties):
        result['connect'] = mqtt_reason_ok(reason)
        connected.set()
    def on_publish(_client, _userdata, _mid, reason, _properties):
        result['publish'] = mqtt_puback_ok(reason)
        published.set()
    client.on_connect = on_connect
    client.on_publish = on_publish
    try:
        if client.connect('127.0.0.1', port, 10, clean_start=True) != mqtt.MQTT_ERR_SUCCESS:
            return False
        client.loop_start()
        if not connected.wait(5) or not result['connect']:
            return False
        info = client.publish(topic, b'review-readiness', qos=1, retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS or not published.wait(5):
            return False
        return result['publish']
    except (OSError, ValueError):
        return False
    finally:
        try:
            client.disconnect()
            client.loop_stop()
        except (OSError, RuntimeError):
            pass


def _publisher_probe(root, credentials):
    port = json.loads((root / 'private' / 'marker.json').read_text(encoding='utf-8'))['ports']['mqtt']
    return {'reliability': _mqtt_publish_probe(port, role='mqtt_receipts',
            password=credentials['mqtt_receipts'], topic='oilfield/review/health/reliability'),
            'waveform': _mqtt_publish_probe(port, role='mqtt_wave_publish',
            password=credentials['mqtt_wave_publish'], topic='oilfield/review/health/waveform')}


def _dispatch(action, root):
    if action == 'fingerprint':
        from backend.review_runtime import _source_hashes
        return {'schema_sha256': _source_hashes()[0]}
    if action == 'identity_init':
        from backend.review_runtime import initialize_identity
        from .review_deploy_paths import private_acl, write_json_private
        accounts = initialize_identity(root / 'private' / 'identity.db')
        private_acl(root / 'private' / 'identity.db')
        write_json_private(root / 'private' / 'accounts.json', accounts)
        return {'instance_id': accounts['instance_id']}
    if action == 'identity_check':
        from backend.review_runtime import check_identity
        return check_identity(root / 'private' / 'identity.db')
    if action in {'validate_configs', 'install_schemas', 'probe_database', 'receiver_ready', 'publisher_probe'}:
        credentials = _credentials(root)
        if action == 'validate_configs':
            return _validate_configs(root, credentials)
        if action == 'probe_database':
            return _probe_database(root, credentials)
        if action == 'receiver_ready':
            return _receiver_ready(root, credentials)
        if action == 'publisher_probe':
            return _publisher_probe(root, credentials)
        from .review_deploy_pg import install_schemas
        from .review_deploy_config import deployment_profile
        port = json.loads((root / 'private' / 'marker.json').read_text(encoding='utf-8'))['ports']['postgres']
        private = root / 'private'
        install_schemas(port=port, credentials=credentials,
                        center_config=private / 'center.toml',
                        waveform_config=private / 'waveform.toml',
                        inversion_config=private / 'inversion.toml', profile=deployment_profile(root))
        return {'installed': True}
    raise ValueError('unknown exported backend action')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--action', required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--nonce', required=True)
    args = parser.parse_args(argv)
    root = args.root.absolute()
    code = root / 'code'
    receipt = args.receipt.absolute()
    if (Path(__file__).resolve().parent.parent != code.resolve() or Path.cwd().resolve() != code.resolve()
            or receipt.parent != root / 'private' or not receipt.is_file()
            or receipt.name != 'backend-' + args.nonce + '.json'):
        return 2
    try:
        data = _dispatch(args.action, root)
        payload = {'ok': True, 'nonce': args.nonce, 'action': args.action, 'data': data}
        status = 0
    except Exception as error:
        payload = {'ok': False, 'nonce': args.nonce, 'action': args.action,
                   'code': type(error).__name__}
        status = 1
    with receipt.open('w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
