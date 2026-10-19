"""Exercise the actual socket, MQTT, application receipt and HTTP read path."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import queue
import sqlite3
import threading
import time
from urllib.parse import urlencode
from uuid import uuid4

from .contracts import CheckResult
from .gateway_adapter import SoftwareFixtureModbusPoller
from .history import POINTS, telemetry_point_map
from .lab import checked_handle, component_root, stop_owned_component, start_owned_component, restart_owned_component
from .lab_http import LabHttp, LabHttpError
from .modbus_device import ReadOnlyModbusDevice
from .storage import canonical_bytes, load_pack, safe_path, strict_json
from tools import review_deploy as deploy
from tools.review_deploy_config import deployment_fixture, env_name
from tools.review_deploy_paths import private_acl, write_json_private, replace_json_private


STEPS = ('registry', 'poll', 'stored', 'history', 'duplicate', 'id_conflict',
         'unknown_identity', 'permission_denied', 'modbus_disconnect', 'center_outage', 'restart')


def utc_now():
    return datetime.now(timezone.utc)


def stored_complete(status, count):
    return status.get('counts', {}).get('stored') == count and status.get('pending_application') == 0


def measurement_mapping(scenario, index, simulated_at, measured_at, row, registered_at):
    if datetime.fromisoformat(measured_at.replace('Z', '+00:00')) < registered_at:
        raise ValueError('actual measurement predates completed registry binding')
    return {'scenario': scenario, 'index': index, 'simulated_at': simulated_at, 'measured_at': measured_at,
            'message_id': row['message_id'], 'payload_sha256': row['payload_sha256']}


def run_steps(driver, root):
    checks = []
    failed = False
    path = Path(root) / 'telemetry-checks.json'
    for name in STEPS:
        if failed:
            actual, status = {'reason': 'prior_step_failed'}, 'not_run'
        else:
            try:
                actual = driver.step(name)
                status = 'passed' if actual.get('passed') is True else 'failed'
            except Exception as exc:
                # Exception text can contain credentials or private request data.
                actual, status = {'error_type': type(exc).__name__}, 'failed'
                if isinstance(exc, LabHttpError):
                    actual.update(http_status=exc.status, error_code=exc.code)
            failed = status != 'passed'
        checks.append(CheckResult('telemetry-' + name, 'network', status,
                                  {'real_evidence_required': True}, actual))
        output = {'schema': 'owp.testpack.telemetry-checks/1', 'checks': [asdict(c) for c in checks],
                  'field_validation': False, 'broker_restart_tested': False}
        (replace_json_private if path.exists() else write_json_private)(path, output)
    return tuple(checks)


def wait_until(check, *, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        threading.Event().wait(.1)
    raise TimeoutError('bounded software lab wait expired')


def _assert(value):
    if not value:
        raise AssertionError('observed software evidence did not satisfy declared check')


class _MqttPeer:
    def __init__(self, config, *, username=None, password=None):
        import paho.mqtt.client as mqtt
        self.connected = threading.Event()
        self.published = queue.Queue()
        self.reason = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='testpack-probe-'+uuid4().hex,
                                  protocol=mqtt.MQTTv5, reconnect_on_failure=False)
        self.client.username_pw_set(username or config.username, config.password if password is None else password)
        def connected(_c, _u, _f, reason, _p):
            self.reason = reason.value
            self.connected.set()
        self.client.on_connect = connected
        self.client.on_publish = lambda _c, _u, mid, reason, _p: self.published.put((mid, reason.value))
        try:
            self.client.connect('127.0.0.1', config.port, 20)
            self.client.loop_start()
            if not self.connected.wait(5):
                raise TimeoutError('MQTT CONNACK timeout')
        except BaseException:
            self.close(); raise

    def publish(self, topic, payload):
        if self.reason != 0:
            raise ValueError('MQTT peer not authenticated')
        info = self.client.publish(topic, payload, qos=1, retain=False)
        mid, reason = self.published.get(timeout=5)
        _assert(mid == info.mid)
        return reason

    def close(self):
        self.client.disconnect(); self.client.loop_stop()


class TelemetrySession:
    def __init__(self, lab, pack, root):
        self.lab, self.pack, self.root = lab, pack, root
        self.http = LabHttp(lab, 'monitoring')
        from backend.gateway.config import load_gateway_config
        credentials = deploy._credential_file(lab.monitoring_root)
        self.config = load_gateway_config(lab.monitoring_root / 'private/gateway.toml',
                    env={env_name('mqtt_testpack'): credentials['mqtt_testpack']})
        self.runtime = self.device = self.peer = None
        self.center_stopped = False
        self.registered = []
        self.inputs = [[strict_json(line) for line in (pack.root / f'history/history-{i:03d}.jsonl').read_bytes().splitlines()[:8]] for i in (1, 2)]
        if any(len(rows) != 8 for rows in self.inputs):
            raise ValueError('two complete eight-frame input scenarios required')

    def get(self, path, **filters):
        if filters:
            path += '?' + urlencode(filters)
        return self.http.request('GET', path, actor='administrator')['data']

    def post(self, path, *, actor='administrator', **fields):
        body = {'idempotency_key': str(uuid4()), 'reason': '可追溯软件链路测试', **fields}
        # Existing registry creates return 200, not a fabricated local receipt.
        return self.http.request('POST', path, body, actor=actor)['data']['entity']

    def step(self, name):
        return getattr(self, name)()

    def registry(self):
        for unit in (1, 2):
            well = self.post('/api/registry/wells', area_code='TESTPACK', well_code=f'TESTPACK-WELL-{unit:03d}', name=f'软件测试井 {unit:03d}')
            device = self.post('/api/registry/devices', gateway_id='REVIEW-GW', device_id=f'TESTPACK-PUMP-{unit:03d}',
                               name=f'只读软件终端 {unit:03d}', transport='modbus_tcp', usage='commissioning')
            path = '/api/registry/devices/' + device['id']
            mapping = self.post(path+'/point-maps', expected_version=device['version'], parent_version=None,
                                points=[asdict(p) for p in telemetry_point_map(unit)])
            checked = self.post(path+'/point-maps/'+mapping['map_version']+'/review', actor='production_engineer',
                                expected_version=mapping['device_version'], sha256=mapping['sha256'], decision='approved')
            bound = self.post(path+'/bindings', expected_version=checked['device_version'], well_id=well['id'])
            assigned = self.post(path+'/point-map-assignment', expected_version=bound['version'], map_version=mapping['map_version'])
            active = self.post(path+'/state', actor='reviewer', expected_version=assigned['version'], status='active', check_note='软件设备入库链路核查')
            self.registered.append({'well_id': well['id'], 'device_id': device['id'], 'code': device['device_id'],
                                    'map_version': mapping['map_version'], 'active_version': active['version']})
        self.registered_at = utc_now()
        write_json_private(self.root/'registry.json', {'entities': self.registered, 'activation_observed_at': self.registered_at.isoformat()})
        return {'passed': True, 'wells': 2, 'devices': 2, 'points_per_device': 12, 'activation_observed_at': self.registered_at.isoformat()}

    def rows(self):
        with closing(sqlite3.connect(self.config.spool_database.as_uri()+'?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute('SELECT message_id,topic,payload,payload_sha256,state,receipt_id FROM uplink_queue_v2 ORDER BY created_at,message_id')]

    def poll(self):
        from backend.gateway.spool_migrate import initialize_spool
        from backend.gateway.runtime import GatewayRuntime
        initialize_spool(self.config.spool_database, limits=self.config.spool_limits)
        self.device = ReadOnlyModbusDevice({i: telemetry_point_map(i) for i in (1, 2)}, host='127.0.0.1', port=self.lab.ports['modbus'])
        self.device.start()
        self.runtime = GatewayRuntime(self.config, poller_factory=SoftwareFixtureModbusPoller)
        mappings = []
        for index in range(8):
            before = {r['message_id'] for r in self.rows()}
            for unit in (1, 2):
                frame = self.inputs[unit-1][index]
                self.device.set_frame(unit, {s['point']: s['value'] for s in frame['samples']})
            actual = self.runtime.run_cycle()
            _assert(actual.devices_polled == 2 and actual.samples_collected == 24 and actual.messages_enqueued == 2 and not actual.poll_errors)
            for row in self.rows():
                if row['message_id'] in before:
                    continue
                raw = strict_json(row['payload'])
                unit = int(raw['device_id'][-3:])
                mapping = measurement_mapping(f'history-{unit:03d}', index, self.inputs[unit-1][index]['samples'][0]['measured_at'],
                                               raw['samples'][0]['measured_at'], row, self.registered_at)
                mappings.append(mapping)
        self.baseline = self.rows()
        _assert(len(self.baseline) == 16 and len(mappings) == 16)
        self.first_time = min(m['measured_at'] for m in mappings)
        self.last_time = utc_now().isoformat()
        write_json_private(self.root/'time-mapping.json', {'source_kind': 'synthetic', 'mapping': mappings})
        write_json_private(self.root/'modbus-exchanges.json', {'exchanges': list(self.device.exchanges)})
        for row in self.baseline:
            with (self.root/(row['message_id']+'.payload.json')).open('xb') as output:
                output.write(row['payload'])
        self.peer = _MqttPeer(self.config.mqtt)
        _assert(self.peer.reason == 0)
        return {'passed': True, 'baseline_messages': 16, 'baseline_samples': 192, 'socket_exchanges': len(self.device.exchanges)}

    def message(self, row):
        result = self.get('/api/production-monitoring/acquisition/messages/'+row['message_id'])
        _assert(result['payload_sha256'] == row['payload_sha256'] and result['sample_count'] == '12'
                and result['ingest_status'] == 'stored' and result['sources'] == ['software-fixture-modbus-tcp']
                and result['processing']['receipt_status'] == 'stored' and result['unassigned_count'] == '0')
        return result

    def stored(self):
        wait_until(lambda: stored_complete(self.runtime.spool.status(), 16))
        records = [self.message(row) for row in self.baseline]
        write_json_private(self.root/'baseline-http.json', {'messages': records})
        return {'passed': True, 'stored_messages': len(records), 'stored_samples': sum(int(r['sample_count']) for r in records),
                'pending_application': self.runtime.spool.status()['pending_application']}

    def history(self):
        totals = []
        for binding in self.registered:
            for point in POINTS:
                result = self.get('/api/production-monitoring/wells/'+binding['well_id']+'/history',
                         registry_device_id=binding['device_id'], point_code=point, from_time=self.first_time,
                         to_time=self.last_time, page_size=200)
                _assert(int(result['total']) == 8)
                totals.append({'well_id': binding['well_id'], 'point': point, 'total': int(result['total'])})
        _assert(sum(r['total'] for r in totals) == 192)
        return {'passed': True, 'point_history_totals': totals, 'baseline_messages': 16, 'baseline_samples': 192}

    def duplicate(self):
        row = self.baseline[0]
        before = int(self.message(row)['processing']['duplicate_count'])
        _assert(self.peer.publish(row['topic'], row['payload']) in (0, 16))
        wait_until(lambda: int(self.message(row)['processing']['duplicate_count']) > before)
        self.history()
        for binding in self.registered:
            result = self.get('/api/production-monitoring/acquisition/messages', gateway_id='REVIEW-GW', device_id=binding['code'], page_size=200)
            _assert(int(result['total']) == 8)
        return {'passed': True, 'message_id': row['message_id'], 'payload_sha256': row['payload_sha256'],
                'duplicate_count_before': before, 'duplicate_count_after': int(self.message(row)['processing']['duplicate_count']), 'samples_after': 192}

    def id_conflict(self):
        row = self.baseline[0]
        changed = strict_json(row['payload'])
        changed['samples'][0]['value'] += 1
        payload = canonical_bytes(changed)
        digest = sha256(payload).hexdigest()
        _assert(self.peer.publish(row['topic'], payload) in (0, 16))
        def find():
            cases = self.get('/api/ingest-reliability/cases', message_id=row['message_id'])['items']
            return next((c for c in cases if c['payload_sha256'] == digest and c['reason_code'] == 'idempotency_conflict'), None)
        case = wait_until(find)
        receipt = self.get('/api/ingest-reliability/receipts/'+case['receipt_id'])['entity']
        _assert(receipt['status'] == 'quarantined')
        raw = self.http.request('GET', '/api/ingest-reliability/facts/'+case['fact_id']+'/raw', actor='administrator', raw=True)
        _assert(raw == payload)
        self.message(row); self.history()
        write_json_private(self.root/'conflict.json', {'case': case, 'receipt': receipt})
        return {'passed': True, 'original_sha256': row['payload_sha256'], 'conflicting_sha256': digest,
                'reason': 'idempotency_conflict', 'receipt_id': case['receipt_id'], 'baseline_samples_unchanged': 192}

    def unknown_identity(self):
        peer = _MqttPeer(self.config.mqtt, username='unregistered-testpack-'+uuid4().hex, password='invalid-software-probe')
        try:
            _assert(peer.reason in (134, 135))
            return {'passed': True, 'rejection_layer': 'MQTT CONNACK', 'reason_code': peer.reason}
        finally:
            peer.close()

    def permission_denied(self):
        result = self.http.request('POST', '/api/registry/wells',
                   {'idempotency_key': str(uuid4()), 'reason': '无权角色拒绝测试', 'area_code': 'TESTPACK',
                    'well_code': 'DENIED-'+uuid4().hex, 'name': '不得创建'}, actor='algorithm_engineer', expected=403)
        return {'passed': result.get('ok') is False, 'rejection_layer': 'HTTP authorization', 'http_status': 403, 'error_code': result['error']['code']}

    def modbus_disconnect(self):
        self.device.stop()
        before = len(self.rows())
        actual = self.runtime.run_cycle()
        codes = [e.code for e in actual.poll_errors]
        _assert(len(codes) == 2 and set(codes) <= {'connection_failed', 'read_failed', 'client_error'}
                and actual.samples_collected == 0 and len(self.rows()) == before)
        self.device.start()
        return {'passed': True, 'observed_error_codes': codes, 'false_samples_created': 0}

    def center_outage(self):
        stop_owned_component(self.lab, 'monitoring', 'center'); self.center_stopped = True
        before = {r['message_id'] for r in self.rows()}
        actual = self.runtime.run_cycle()
        _assert(actual.messages_enqueued == 2 and actual.samples_collected == 24)
        rows = [r for r in self.rows() if r['message_id'] not in before]
        wait_until(lambda: all(self.runtime.spool.get(r['message_id'])['state'] == 'awaiting_receipt' for r in rows), timeout=15)
        _assert(not stored_complete(self.runtime.spool.status(), 18))
        start_owned_component(self.lab, 'monitoring', 'center'); self.center_stopped = False
        self.runtime.uplink.wake()
        wait_until(lambda: stored_complete(self.runtime.spool.status(), 18))
        for row in rows:
            self.message(row)
        self.recovered = rows
        return {'passed': True, 'queued_during_outage': 2, 'recovered': 2, 'baseline_messages': 16,
                'additional_fault_test_messages': 2, 'broker_kept_running': True}

    def restart(self):
        from backend.gateway.runtime import GatewayRuntime
        before = {r['message_id']: r['payload_sha256'] for r in self.rows()}
        self.runtime.close()
        restart_owned_component(self.lab, 'monitoring', 'http')
        restart_owned_component(self.lab, 'monitoring', 'center')
        self.http = LabHttp(self.lab, 'monitoring')
        self.runtime = GatewayRuntime(self.config, poller_factory=SoftwareFixtureModbusPoller)
        wait_until(lambda: stored_complete(self.runtime.spool.status(), 18))
        _assert(before == {r['message_id']: r['payload_sha256'] for r in self.rows()})
        for row in self.baseline + self.recovered:
            self.message(row)
        self.history()
        return {'passed': True, 'retained_messages': 18, 'baseline_samples': 192, 'additional_fault_samples': 24,
                'original_hashes_unchanged': True, 'components': ['http', 'center', 'gateway_queue'], 'broker_restart_tested': False}

    def close(self):
        if self.peer:
            self.peer.close()
        if self.runtime:
            self.runtime.close()
        if self.device:
            self.device.stop()
        if self.center_stopped:
            start_owned_component(self.lab, 'monitoring', 'center')
            self.center_stopped = False


def run_telemetry(lab, pack, run_root):
    checked_handle(lab)
    pack = load_pack(pack.root)
    declaration = deployment_fixture(lab.monitoring_root)
    if sha256((pack.root/'manifest.json').read_bytes()).hexdigest() != declaration['pack_sha256']:
        raise ValueError('pack differs from initialized lab')
    root = safe_path(Path(run_root), missing=True)
    if root.exists() or root.parent != lab.root:
        raise ValueError('run must be a new direct child of the owned lab')
    root.mkdir(); private_acl(root)
    write_json_private(root/'run.json', {'schema': 'owp.testpack.run/1', 'run_id': str(uuid4()), 'lab_id': lab.lab_id,
                       'pack_id': pack.manifest['pack_id'], 'source_kind': 'synthetic', 'started_at': utc_now().isoformat(),
                       'deployment_commits': {p: deploy._marker(lab.root/p)['source']['commit'] for p in ('monitoring','review')}})
    session = TelemetrySession(lab, pack, root)
    try:
        return run_steps(session, root)
    finally:
        session.close()
