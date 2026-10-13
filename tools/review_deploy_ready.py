"""Bounded, source-tied readiness evidence for one owned review launch."""
import http.client
import json
from pathlib import Path
import time

from .review_deploy_process import inspect_pid, owned_process
from .review_deploy_source import run_exported_backend


COMPONENTS = frozenset({'http', 'center_subscription', 'wave_receiver',
                        'reliability_publisher', 'wave_publisher',
                        'wave_assembler', 'inversion_worker'})
TOPIC = 'oilfield/v1/+/+/telemetry'


def center_subscribed(log_path, offset):
    """Accept only the latest successful SUBACK from this launch's log bytes."""
    log_path = Path(log_path)
    if type(offset) is not int or offset < 0 or not log_path.is_file():
        return False
    with log_path.open('rb') as stream:
        stream.seek(0, 2)
        end = stream.tell()
        if end < offset or end - offset > 1024 * 1024:
            return False
        stream.seek(offset)
        payload = stream.read(end - offset)
    state = False
    for line in payload.splitlines():
        try:
            event = json.loads(line.decode('utf-8'))
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('event')
        if kind == 'mqtt_subscribed':
            state = event.get('topic') == TOPIC and event.get('qos') == 1
        elif kind in {'mqtt_subscribe_rejected', 'mqtt_subscribe_failed',
                      'mqtt_connection_rejected', 'mqtt_retry_scheduled', 'ingest_startup_failed'}:
            state = False
    return state


def worker_started(log_path, offset, component):
    """Require a successful worker-loop event after this launch's log offset."""
    if component not in {'wave_assembler', 'inversion'}:
        return False
    log_path = Path(log_path)
    if type(offset) is not int or offset < 0 or not log_path.is_file():
        return False
    with log_path.open('rb') as stream:
        stream.seek(0, 2)
        end = stream.tell()
        if end < offset or end - offset > 1024 * 1024:
            return False
        stream.seek(offset)
        lines = stream.read(end - offset).splitlines()
    ready = False
    for line in lines:
        try:
            event = json.loads(line.decode('utf-8'))
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict) or event.get('component') != component:
            continue
        if event.get('event') == 'review_worker_ready':
            ready = True
        elif event.get('event') == 'review_worker_failed':
            ready = False
    return ready


def readiness_complete(evidence):
    return isinstance(evidence, dict) and all(evidence.get(name) is True for name in COMPONENTS)


def http_capabilities(port, accounts, profile='review'):
    """Use an actual login to verify the review HTTP and module capabilities."""
    try:
        admin = next(row for row in accounts['accounts'] if row['username'] == 'administrator')
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        conn.request('GET', '/api/health')
        response = conn.getresponse()
        health = json.loads(response.read(65536)) if response.status == 200 else {}
        conn.close()
        data = health.get('data', {})
        if (data.get('runtime_mode') != 'review' or data.get('database') != 'ok' or
                data.get('simulator') != 'disabled' or data.get('control_enabled') is not False):
            return False
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        body = json.dumps({'username': admin['username'], 'password': admin['password']})
        conn.request('POST', '/api/auth/login', body, {'Content-Type': 'application/json', 'Origin':f'http://127.0.0.1:{port}'})
        response = conn.getresponse()
        response.read(65536)
        cookie = response.getheader('Set-Cookie', '').split(';', 1)[0]
        status = response.status
        conn.close()
        cookie_name = 'oilwell_monitoring_session' if profile == 'monitoring' else 'platform_session'
        if status != 200 or not cookie.startswith(cookie_name + '='):
            return False
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        conn.request('GET', '/api/auth/me', headers={'Cookie': cookie})
        response = conn.getresponse()
        me = json.loads(response.read(65536)) if response.status == 200 else {}
        conn.close()
        capabilities = me.get('data', {}).get('capabilities', {})
        if profile == 'monitoring':
            return (data.get('ui_mode') == 'monitoring' and me.get('data', {}).get('ui_mode') == 'monitoring'
                    and all(capabilities.get(name, {}).get('status') == 'ready'
                            for name in ('registry', 'monitoring', 'ingest_reliability')))
        return (me.get('data', {}).get('runtime_mode') == 'review' and
                all(capabilities.get(name, {}).get('status') == 'ready' for name in
                    ('registry', 'production_studies', 'production_inversion',
                     'waveforms', 'ingest_reliability')))
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return False


def launch_evidence(root, marker):
    root = Path(root).absolute()
    processes = marker.get('processes', {})
    names = ('http', 'center', 'wave_receiver', 'wave_assembler', 'wave_publisher',
             'reliability_publisher', 'inversion', 'broker')
    if any(name not in processes or not owned_process(processes[name], inspect_pid(processes[name]['pid']))
           for name in names):
        return {name: False for name in COMPONENTS}
    account_path = root / 'private' / 'accounts.json'
    if account_path.is_symlink() or account_path.stat().st_nlink != 1:
        return {name: False for name in COMPONENTS}
    accounts = json.loads(account_path.read_text(encoding='utf-8'))
    center = center_subscribed(root / 'logs' / 'center.log', marker['center_log_offset'])
    http = http_capabilities(marker['ports']['http'], accounts, marker.get('profile', 'review'))
    try:
        receiver = run_exported_backend(root, 'receiver_ready', timeout=20)['ready'] is True
        publishers = run_exported_backend(root, 'publisher_probe', timeout=25)
    except RuntimeError:
        receiver, publishers = False, {}
    return {'http': http, 'center_subscription': center, 'wave_receiver': receiver,
            'reliability_publisher': publishers.get('reliability') is True,
            'wave_publisher': publishers.get('waveform') is True,
            'wave_assembler': worker_started(root / 'logs' / 'wave_assembler.log',
                                             marker['wave_assembler_log_offset'], 'wave_assembler'),
            'inversion_worker': worker_started(root / 'logs' / 'inversion.log',
                                                marker['inversion_log_offset'], 'inversion')}


def wait_for_readiness(root, marker, *, timeout=60):
    deadline = time.monotonic() + timeout
    evidence = {name: False for name in COMPONENTS}
    while time.monotonic() < deadline:
        evidence = launch_evidence(root, marker)
        if readiness_complete(evidence):
            return evidence
        time.sleep(1)
    return evidence
