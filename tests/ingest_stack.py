"""Owned loopback integration stack; no existing service or business DB is used."""
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
import base64
import http.client
import importlib
import json
import os
from pathlib import Path
import random
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlencode
from uuid import uuid4

import psycopg
from psycopg import sql

from backend.gateway.contracts import TelemetrySample, UplinkBatch
from backend.gateway.reliable_spool import SpoolLimits
from backend.gateway.spool_migrate import initialize_spool
from backend.ingest_reliability.contracts import Observation
from tests.ingest_broker import Broker
from tests.ingest_support import ROOT, TEST_DATABASE


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def free_browser_http_port():
    # Chromium rejects some OS-assigned ephemeral ports (including 1723).
    # This high range is outside its restricted HTTP port list.
    lower, upper = 49152, 65536
    first = random.randrange(lower, upper)
    for offset in range(upper - lower):
        port = lower + (first - lower + offset) % (upper - lower)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(('127.0.0.1', port))
            return port
        except OSError:
            continue
    raise RuntimeError('No available loopback browser HTTP port in 49152..65535')


def toml_text(sections):
    # Test fixture values are strings, finite numbers, booleans, or string lists.
    return '\n\n'.join('[' + name + ']\n' + '\n'.join(
        key + ' = ' + json.dumps(value, ensure_ascii=False) for key, value in values.items())
        for name, values in sections.items()) + '\n'


class Stack:
    def __init__(self, fixture, *, http=False, max_messages=5000):
        self.fixture = fixture
        self.with_http = http
        self.http_port = free_browser_http_port()
        self.temp = tempfile.TemporaryDirectory(prefix='wp03-stack-')
        self.root = Path(self.temp.name)
        self.broker = Broker()
        self.spool = self.root / 'gateway-v2.db'
        self.limits = SpoolLimits(max_messages, 8 * 1024 * 1024, 1024 * 1024)
        self.url = f'http://127.0.0.1:{self.http_port}'
        self.platform_db = self.root / 'platform.db'
        self.config = self.root / 'center.toml'
        self.gateway_config = self.root / 'gateway.json'
        self.processes = {}
        self.logs = {}
        self.handles = []
        self.tokens = {}
        self.run_id = uuid4().hex
        self.restarts = []
        self.evidence = []

    def __enter__(self):
        try:
            initialize_spool(self.spool, limits=self.limits)
            self.sections = self._sections()
            if self.with_http:
                self._prepare_registry()
            self.config.write_text(toml_text(self.sections), encoding='utf-8')
            self.gateway_config.write_text(json.dumps({
                'port': self.broker.port, 'client_id': 'wp03-gateway-' + self.run_id,
                'spool': str(self.spool), 'limits': asdict(self.limits)}), encoding='utf-8')
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, kind, value, traceback):
        try:
            if kind is not None:
                # Only explicit worker/browser logs; never credentials/configs or raw payloads.
                destination = os.environ.get('OILWELL_INGEST_EVIDENCE_DIR')
                if destination:
                    folder = Path(destination) / ('failure-' + self.run_id)
                    folder.mkdir(parents=True, exist_ok=False)
                    for name, path in self.logs.items():
                        shutil.copyfile(path, folder / (name + '.log'))
        finally:
            self.close()

    def close(self):
        for name in reversed(list(self.processes)):
            self.stop_process(name)
        for handle in self.handles:
            handle.close()
        self.broker.close()
        self.temp.cleanup()

    def _sections(self):
        mqtt = {'host': '127.0.0.1', 'port': self.broker.port,
                'client_id': 'wp03-center-' + self.run_id, 'username': 'platform-ingestor',
                'password_env': 'OILWELL_WP03_TEST_MQTT', 'keepalive_seconds': 10,
                'session_expiry_seconds': 3600, 'connect_timeout_seconds': 1, 'publish_timeout_seconds': 1}
        sections = {'center': {'topic_prefix': 'oilfield'}, 'mqtt': mqtt,
                    'mqtt_receipt_publisher': {**{key: value for key, value in mqtt.items() if key != 'session_expiry_seconds'},
                                               'client_id': 'wp03-publisher-' + self.run_id,
                                               'username': 'platform-receipt-publisher'},
                    'reliability': {'enabled': True, 'total_bytes': 16 * 1024 * 1024,
                                    'quarantine_bytes': 8 * 1024 * 1024, 'receipt_gateways': ['GW-001'],
                                    'receive_maximum': 20, 'publish_batch_size': 20, 'lease_seconds': 5,
                                    'retry_max_seconds': 2, 'health_stale_seconds': 5,
                                    'allowed_origins': [self.url]},
                    'registry': {'enabled': self.with_http}}
        for role, name in [('ingestor', 'postgres_reliable_ingest'), ('publisher', 'postgres_receipt_publisher'),
                           ('reader', 'postgres_ingest_reader'), ('operator', 'postgres_ingest_operator')]:
            sections[name] = dict(self.fixture.raw_config['postgres_' + role])
        sections['postgres'] = {'host': '127.0.0.1', 'port': 3822, 'database': TEST_DATABASE,
                                'user': 'unused_test_legacy', 'password_env': 'OILWELL_TEST_UNUSED_MQTT',
                                'sslmode': 'disable'}
        if self.with_http:
            sections['registry']['allowed_origins'] = [self.url]
            sections['test_http'] = {'port': self.http_port, 'database': str(self.platform_db)}
        return sections

    def _prepare_registry(self):
        # Separate existing TEST identities are granted only on the WP-03 TEST database.
        # Do not give the reliability metadata reader raw telemetry SELECT privileges.
        source = Path(os.environ['OILWELL_REGISTRY_TEST_CONFIG'])
        raw = tomllib.loads(source.read_text(encoding='utf-8'))
        users = {}
        for role, section in [('writer', 'postgres_registry'), ('reader', 'postgres_query')]:
            values = dict(raw['postgres_' + role])
            if ((values['host'], values['port'], values['database']) !=
                    ('127.0.0.1', 3822, 'oilwell_registry_test') or not values['user'].startswith('registry_test_')):
                raise AssertionError('Refusing non-test registry identity')
            values['database'] = TEST_DATABASE
            self.sections[section] = values
            users[role] = values['user']
        with self.fixture.admin() as conn:
            for user in users.values():
                conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                    sql.Identifier(TEST_DATABASE), sql.Identifier(user)))
        migration = importlib.import_module('backend.registry.migrate')
        migration.migrate(self.fixture.settings['migrator'], expected_database=TEST_DATABASE)
        with self.connection('migrator') as conn:
            migration.grant_roles(conn, **users)
        with self.connection('migrator') as conn:
            conn.execute('''TRUNCATE device_registry.operation_receipts,
                device_registry.point_map_assignments, device_registry.point_map_versions,
                device_registry.well_bindings, device_registry.device_state_periods,
                device_registry.audit_events, device_registry.devices, device_registry.wells''')
            conn.execute('UPDATE device_registry.registry_head SET revision=1 WHERE singleton=1')

    @contextmanager
    def connection(self, role='reader'):
        # Fresh bounded connections close on every observation (including a 30-minute soak).
        conn = psycopg.connect(**self.fixture.settings[role])
        try:
            if conn.execute('SELECT current_database()').fetchone()[0] != TEST_DATABASE:
                raise AssertionError('Refusing non-test database')
            conn.execute("SET statement_timeout='3s'")
            conn.execute("SET lock_timeout='1s'")
            conn.commit()
            with conn:
                yield conn
        finally:
            conn.close()

    def scalar(self, statement, params=(), role='reader'):
        with self.connection(role) as conn:
            row = conn.execute(statement, params).fetchone()
            return row[0] if row else None

    def launch(self, name, *, path=None, fault=''):
        old = self.processes.get(name)
        if old is not None and old.poll() is None:
            raise AssertionError('Refusing duplicate owned process')
        path = path or (self.gateway_config if name == 'gateway' else self.config)
        logfile = self.root / (name + '.log')
        output = logfile.open('ab', buffering=0)
        self.handles.append(output)
        self.logs[name] = logfile
        env = dict(os.environ, PYTHONUTF8='1', NO_BROWSER='1',
                   OILWELL_WP03_TEST_MQTT=self.broker.password,
                   PLATFORM_DATABASE_PATH=str(self.platform_db), PLATFORM_LOG_PATH=str(self.root / 'platform-logs'),
                   OILWELL_TEST_UNUSED_MQTT='not-connected-test-value')
        executable = sys.executable
        base_executable = getattr(sys, '_base_executable', executable)
        if os.name == 'nt' and os.path.normcase(executable) != os.path.normcase(base_executable):
            # Match CPython multiprocessing: own the interpreter, not the venv
            # redirector whose exit can precede its worker's handle release.
            env['__PYVENV_LAUNCHER__'] = executable
            executable = base_executable
        process = subprocess.Popen([executable, '-u', '-m', 'tests.ingest_worker', name,
                                    '--config', str(path), '--fault', fault], cwd=ROOT, env=env,
                                   stdin=subprocess.PIPE, stdout=output, stderr=output,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.processes[name] = process
        return process

    def start(self, *, receipt_delivery=True, database_refused=False, center_fault='', publisher_fault=''):
        # Broker owns startup in its constructor; only restart after an explicit stop.
        if database_refused:
            refused = {name: dict(values) for name, values in self.sections.items()}
            port = free_port()
            for name, values in refused.items():
                if name.startswith('postgres'):
                    values['port'] = port
            path = self.root / 'refused.toml'
            path.write_text(toml_text(refused), encoding='utf-8')
            self.launch('center', path=path)
            self.await_process_exit('center', 2)
        else:
            self.launch('center', fault=center_fault)
            self.await_process_event('center', 'mqtt_subscribed')
        self.launch('gateway')
        self.await_process_event('gateway', 'gateway_ready')
        if receipt_delivery:
            self.launch('publisher', fault=publisher_fault)
            self.await_process_event('publisher', 'publisher_iteration')
        if self.with_http:
            self.launch('http')
            self.await_process_event('http', 'http_ready')

    def stop_process(self, name, *, kill=False):
        process = self.processes.get(name)
        if process is None:
            return
        if process.poll() is None:
            if not kill and name == 'gateway':
                try:
                    process.stdin.write(b'{"action":"stop"}\n')
                    process.stdin.flush()
                    process.wait(timeout=5)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    process.kill()
            elif kill:
                process.kill()
            else:
                process.terminate()
            process.wait(timeout=5)
        if process.stdin:
            process.stdin.close()

    def restart_gateway(self):
        previous = self.processes['gateway'].pid
        self.stop_process('gateway', kill=True)
        offset = len(self.process_log('gateway'))
        self.launch('gateway')
        self.await_process_event('gateway', 'gateway_ready', offset=offset)
        self.restarts.append({'process': 'gateway', 'old_pid': previous, 'new_pid': self.processes['gateway'].pid})

    def restart_center(self):
        previous = self.processes['center'].pid
        self.stop_process('center', kill=True)
        offset = len(self.process_log('center'))
        self.launch('center')
        self.await_process_event('center', 'mqtt_subscribed', offset=offset)
        self.restarts.append({'process': 'center', 'old_pid': previous, 'new_pid': self.processes['center'].pid})

    def enable_receipt_delivery(self):
        self.stop_process('publisher')
        offset = len(self.process_log('publisher'))
        self.launch('publisher')
        self.await_process_event('publisher', 'publisher_iteration', offset=offset)

    def restart_broker(self):
        previous = self.broker.process.pid
        offset = len(self.process_log('center'))
        self.broker.stop(kill=True)
        self.broker.start()
        self.await_process_event('center', 'mqtt_subscribed', offset=offset)
        self.restarts.append({'process': 'broker', 'old_pid': previous, 'new_pid': self.broker.process.pid})

    def process_log(self, name):
        path = self.logs.get(name)
        return path.read_text(encoding='utf-8', errors='replace') if path and path.exists() else ''

    def events(self, name, offset=0):
        result = []
        for line in self.process_log(name)[offset:].splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(value)
            except ValueError:
                pass
        return result

    def await_condition(self, predicate, label, timeout=30):
        if not 0 < timeout <= 30:
            raise ValueError('Every wait must be bounded at 30 seconds')
        deadline = time.monotonic() + timeout
        while True:
            result = predicate()
            if result:
                return result
            if time.monotonic() >= deadline:
                diagnostics = {name: {'exit': process.poll(), 'events': [item.get('event') for item in self.events(name)[-5:]]}
                               for name, process in self.processes.items()}
                raise AssertionError(f'Timeout: {label}; owned processes: {diagnostics}')
            time.sleep(.05)

    def await_process_event(self, name, event, offset=0):
        def matching():
            found = next((item for item in reversed(self.events(name, offset)) if item.get('event') == event), None)
            if found:
                return found
            if self.processes[name].poll() is not None:
                raise AssertionError(f'{name} exited {self.processes[name].returncode}: {self.process_log(name)[-3000:]}')
        return self.await_condition(matching, name + ':' + event)

    def await_process_exit(self, name, code):
        self.await_condition(lambda: self.processes[name].poll() is not None, name + ' exit')
        if self.processes[name].returncode != code:
            raise AssertionError(f'{name}: expected exit {code}: {self.process_log(name)[-3000:]}')

    def command(self, **body):
        key = str(uuid4())
        body['key'] = key
        process = self.processes['gateway']
        process.stdin.write((json.dumps(body) + '\n').encode())
        process.stdin.flush()
        return self.await_condition(lambda: next((item for item in reversed(self.events('gateway'))
                                   if item.get('key') == key), None), 'gateway command response')

    def observation(self, identifier='M-1', *, device='D', quality='good', point='load', value=41.0):
        now = datetime.now(timezone.utc)
        sample = TelemetrySample(gateway_id='GW-001', device_id=device, point_code=point,
                                 measured_at=now, sequence=1, value=value, quality=quality, unit='kN',
                                 source='software-test')
        batch = UplinkBatch.create('GW-001', device, 'BOOT-' + self.run_id, [sample], message_id=identifier, created_at=now)
        return Observation(batch.topic('oilfield'), batch.to_json().encode('utf-8'), 1, False, False, now)

    def enqueue_one(self, observation=None, *, expect_error=False):
        observation = observation or self.observation()
        result = self.command(action='enqueue', message_id=json.loads(observation.payload)['message_id'],
                              topic=observation.topic, payload=base64.b64encode(observation.payload).decode())
        if expect_error:
            if result['ok']:
                raise AssertionError('Expected queue rejection, got acceptance')
            return result
        if not result['ok']:
            raise AssertionError('Gateway rejected test input: ' + result['code'])
        return observation

    def gateway_record(self, identifier='M-1'):
        with closing(sqlite3.connect(self.spool.as_uri() + '?mode=ro', uri=True, timeout=3)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT * FROM uplink_queue_v2 WHERE message_id=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def await_gateway_state(self, state, identifier='M-1'):
        return self.await_condition(lambda: (self.gateway_record(identifier) or {}).get('state') == state,
                                    'gateway ' + identifier + ' ' + state)

    def await_gateway_error(self, code):
        return self.await_condition(lambda: (self.gateway_record() or {}).get('last_error_code') == code,
                                    'gateway refusal ' + code)

    def database_counts(self):
        with self.connection('migrator') as conn:
            return dict(zip(('messages', 'samples', 'facts', 'receipts'), conn.execute('''SELECT
                (SELECT count(*) FROM public.ingest_messages), (SELECT count(*) FROM public.telemetry_samples),
                (SELECT count(*) FROM ingest_reliability.facts), (SELECT count(*) FROM ingest_reliability.receipts)''').fetchone()))

    def await_counts(self, **counts):
        return self.await_condition(lambda: all(self.database_counts()[key] == value for key, value in counts.items()),
                                    'database counts ' + str(counts))

    def fact_payload(self, identifier=None):
        return bytes(self.scalar('SELECT raw_payload FROM ingest_reliability.facts' +
                                (' WHERE message_id=%s' if identifier else ' ORDER BY received_at LIMIT 1'),
                                (identifier,) if identifier else (), 'ingestor'))

    def receipt(self, identifier=None):
        return json.loads(bytes(self.scalar('SELECT payload FROM ingest_reliability.receipts' +
                                            (' WHERE message_id=%s' if identifier else ' ORDER BY recorded_at LIMIT 1'),
                                            (identifier,) if identifier else (), 'publisher')))

    def delivery_state(self, identifier):
        return self.scalar('''SELECT CASE WHEN requested_generation<=sent_generation THEN 'broker_confirmed'
            WHEN lease_until>clock_timestamp() THEN 'inflight' ELSE 'pending' END
            FROM ingest_reliability.receipt_metadata WHERE message_id=%s''', (identifier,))

    def duplicate_observations(self):
        return self.scalar('SELECT coalesce(sum(duplicate_count),0) FROM ingest_reliability.fact_observations', role='ingestor')

    def message_sample_count(self, identifier):
        return self.scalar('SELECT count(*) FROM public.telemetry_samples WHERE message_id=%s', (identifier,), 'ingestor')

    def case_state(self):
        return self.scalar('SELECT state FROM ingest_reliability.case_metadata ORDER BY received_at LIMIT 1')

    def audit_actions(self):
        with self.connection() as conn:
            return [row[0] for row in conn.execute('SELECT action FROM ingest_reliability.audit_metadata ORDER BY recorded_at,id')]

    @contextmanager
    def reject_fact_transaction(self):
        with self.connection('migrator') as conn:
            conn.execute('''CREATE FUNCTION ingest_reliability.test_reject_fact() RETURNS trigger LANGUAGE plpgsql
                AS $$ BEGIN RAISE EXCEPTION 'injected test transaction failure'; END $$;
                CREATE TRIGGER test_reject_fact BEFORE INSERT ON ingest_reliability.facts
                FOR EACH ROW EXECUTE FUNCTION ingest_reliability.test_reject_fact()''')
        try:
            yield
        finally:
            with self.connection('migrator') as conn:
                conn.execute('DROP TRIGGER test_reject_fact ON ingest_reliability.facts; '
                             'DROP FUNCTION ingest_reliability.test_reject_fact()')

    def http(self, method, route, body=None, *, role='administrator'):
        headers = {'Content-Type': 'application/json'}
        if role:
            if role not in self.tokens:
                passwords = {'administrator': 'Admin@2026', 'maintenance_engineer': 'Maintenance@2026'}
                result = self.http('POST', '/api/auth/login', {'username': role, 'password': passwords[role]}, role=None)
                if result['status'] != 200:
                    raise AssertionError('Test login failed')
                self.tokens[role] = result['body']['data']['token']
            headers['Authorization'] = 'Bearer ' + self.tokens[role]
        conn = http.client.HTTPConnection('127.0.0.1', self.http_port, timeout=6)
        try:
            conn.request(method, route, body=json.dumps(body).encode('utf-8') if body is not None else None, headers=headers)
            response = conn.getresponse()
            return {'status': response.status, 'body': json.loads(response.read())}
        finally:
            conn.close()

    def registry_command(self, route, **values):
        result = self.http('POST', '/api/registry' + route,
                           {'reason': '独立软件验证', 'idempotency_key': str(uuid4()), **values})
        expected_status = 201 if route in ('/wells', '/devices') or route.endswith('/point-maps') else 200
        if result['status'] != expected_status:
            raise AssertionError('Registry command rejected: ' + str(result))
        return result['body']['data']['entity']

    def register_device(self):
        well = self.registry_command('/wells', area_code='TEST', well_code='WP03-' + self.run_id, name='软件测试井')
        device = self.registry_command('/devices', gateway_id='GW-001', device_id='D', name='软件测试终端',
                                       transport='mqtt_direct', usage='commissioning')
        route = '/devices/' + device['id']
        mapping = self.registry_command(route + '/point-maps', expected_version=device['version'], parent_version=None,
                                        points=[{'point_code': 'load', 'unit': 'kN', 'engineering_min': 0, 'engineering_max': 80}])
        reviewed = self.registry_command(route + '/point-maps/' + str(mapping['map_version']) + '/review',
                                         expected_version=mapping['device_version'], sha256=mapping['sha256'], decision='approved')
        bound = self.registry_command(route + '/bindings', expected_version=reviewed['device_version'], well_id=well['id'])
        assigned = self.registry_command(route + '/point-map-assignment', expected_version=bound['version'], map_version=mapping['map_version'])
        return self.registry_command(route + '/state', expected_version=assigned['version'], status='active', check_note='软件隔离测试核对依据')

    def resolved_samples(self, device):
        result = self.http('GET', '/api/registry/resolved-telemetry?' + urlencode({'gateway_id': 'GW-001', 'device_id': device}))
        if result['status'] != 200:
            raise AssertionError(str(result))
        return result['body']['data']['items']

    def create_rejected_case(self):
        obs = self.observation()
        body = json.loads(obs.payload)
        body['samples'] = []
        self.enqueue_one(replace(obs, payload=json.dumps(body).encode()))
        self.await_condition(lambda: self.case_state() == 'open', 'open case')
        self.stop_process('gateway')

    def close_case_as_maintenance(self):
        with self.connection() as conn:
            case_id, version = conn.execute('SELECT id,version FROM ingest_reliability.case_metadata LIMIT 1').fetchone()
        operation = {'route': f'/api/ingest-reliability/cases/{case_id}/close',
                     'body': {'expected_version': str(version), 'reason': '核对软件测试记录', 'idempotency_key': str(uuid4())}}
        result = self.repeat_operation(operation)
        if result['status'] != 200:
            raise AssertionError(str(result))
        return operation

    def repeat_operation(self, operation):
        return self.http('POST', operation['route'], operation['body'], role='maintenance_engineer')

    def revoke_permission(self, permission):
        with closing(sqlite3.connect(self.platform_db)) as conn, conn:
            conn.execute('DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code=?) '
                         'AND permission_id=(SELECT id FROM permissions WHERE code=?)', ('maintenance_engineer', permission))

    def preview_as_maintenance(self):
        fact = self.scalar('SELECT fact_id FROM ingest_reliability.case_metadata LIMIT 1')
        return self.http('GET', f'/api/ingest-reliability/facts/{fact}/preview', role='maintenance_engineer')

    def attempt_legacy_open(self):
        process = self.launch('legacy', path=self.spool)
        self.await_condition(lambda: process.poll() is not None, 'legacy queue refusal')
        if process.returncode != 2:
            raise AssertionError('Legacy process did not refuse v2 queue')
        return self.events('legacy')[-1]

    def browser_case_flow(self):
        node_root = Path(os.environ.get('USERPROFILE', '')) / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
        node = shutil.which('node') or str(node_root / 'bin/node.exe')
        destination = Path(os.environ.get('OILWELL_INGEST_EVIDENCE_DIR', self.root / 'browser'))
        destination.mkdir(parents=True, exist_ok=True)
        output = self.root / 'browser.log'
        with output.open('wb') as handle:
            self.logs['browser'] = output
            process = subprocess.Popen([node, str(ROOT / 'tests/fixtures/ingest_live_check.cjs'), self.url, str(destination)],
                                       cwd=ROOT, env=dict(os.environ, NODE_PATH=str(node_root / 'node_modules')),
                                       stdout=handle, stderr=handle, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.processes['browser'] = process
            self.await_condition(lambda: process.poll() is not None, 'real browser case flow')
        if process.returncode:
            raise AssertionError(output.read_text(encoding='utf-8')[-4000:])
        return json.loads(output.read_text(encoding='utf-8').splitlines()[-1])
