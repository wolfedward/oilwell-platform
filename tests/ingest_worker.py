"""Test-only process entry points and crash boundaries; never installed as a service."""
import argparse
import base64
import json
import os
from pathlib import Path
import sys
import tomllib


def event(name, **fields):
    print(json.dumps({'event': name, **fields}, ensure_ascii=True), flush=True)


def gateway(path):
    from backend.gateway.mqtt_uplink import MqttConfig
    from backend.gateway.reliable_spool import ReliableSpool, SpoolLimits
    from backend.gateway.reliable_uplink import ReliableUplink
    from backend.ingest_reliability.errors import ReliabilityError
    values = json.loads(Path(path).read_text(encoding='utf-8'))
    mqtt = MqttConfig(host='127.0.0.1', port=values['port'], client_id=values['client_id'],
                      username='gateway-GW-001', password=os.environ['OILWELL_WP03_TEST_MQTT'],
                      connect_timeout_seconds=1, publish_timeout_seconds=1, keepalive_seconds=10)
    spool = ReliableSpool(values['spool'], limits=SpoolLimits(**values['limits']),
                          receipt_timeout_seconds=2, base_backoff_seconds=.25, max_backoff_seconds=2)
    uplink = ReliableUplink(mqtt, spool, gateway_id='GW-001', prefix='oilfield')
    try:
        uplink.start()
        event('gateway_ready')
        for line in sys.stdin:
            body = json.loads(line)
            if body['action'] == 'stop':
                break
            try:
                if body['action'] == 'enqueue':
                    result = spool.enqueue(body['message_id'], body['topic'], base64.b64decode(body['payload'], validate=True),
                                           confirmation_mode='application')
                    uplink.wake()
                    event('command', key=body['key'], ok=True, inserted=result)
                elif body['action'] == 'status':
                    event('command', key=body['key'], ok=True, status=uplink.snapshot())
                else:
                    raise ValueError('unknown test command')
            except ReliabilityError as error:
                event('command', key=body['key'], ok=False, code=error.code)
    finally:
        uplink.close()
        spool.close()


def center(path, fault):
    import backend.center.__main__ as main
    if fault == 'before_ack':
        create = main.create_paho_client
        def create_with_crash(*args, **kwargs):
            client = create(*args, **kwargs)
            def crash(_mid, _qos):
                event('fault_exit', boundary='committed_before_ack')
                os._exit(71)
            client.ack = crash
            return client
        main.create_paho_client = create_with_crash
    return main.main(['--config', path])


def publisher(path, fault):
    import backend.ingest_reliability.__main__ as main
    from backend.ingest_reliability.outbox import OutboxRepository
    if fault == 'before_publish':
        claim = OutboxRepository.claim
        def claim_then_crash(self, *args, **kwargs):
            result = claim(self, *args, **kwargs)
            if result:
                event('fault_exit', boundary='claimed_before_publish')
                os._exit(72)
            return result
        OutboxRepository.claim = claim_then_crash
    elif fault == 'after_publish':
        finish = OutboxRepository.finish
        def finish_or_crash(self, lease, *, success, **kwargs):
            if success:
                event('fault_exit', boundary='broker_ack_before_finish')
                os._exit(72)
            return finish(self, lease, success=success, **kwargs)
        OutboxRepository.finish = finish_or_crash
    return main.main(['--config', path])


def http(path):
    from backend.http_server import create_server
    options = tomllib.loads(Path(path).read_text(encoding='utf-8'))['test_http']
    server = create_server('127.0.0.1', options['port'], options['database'], center_config_path=path)
    if server.registry_runtime.status != 'ready' or server.ingest_runtime.status != 'ready':
        event('http_failed', registry=server.registry_runtime.status, ingest=server.ingest_runtime.status,
              registry_code=server.registry_runtime.code, ingest_code=server.ingest_runtime.code)
        server.server_close()
        return 2
    event('http_ready', port=server.server_port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def legacy(path):
    from backend.gateway.spool import DurableSpool
    from backend.ingest_reliability.errors import ReliabilityError
    try:
        spool = DurableSpool(path)
    except ReliabilityError as error:
        event('legacy_refused', code=error.code)
        return 2
    spool.close()
    event('legacy_opened')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('gateway', 'center', 'publisher', 'http', 'legacy'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--fault', default='')
    args = parser.parse_args()
    if args.mode in ('center', 'publisher'):
        result = globals()[args.mode](args.config, args.fault)
    else:
        result = globals()[args.mode](args.config)
    raise SystemExit(result or 0)
