"""Own a fresh cluster per integration fixture; never adopt or reset an old DB."""
from pathlib import Path
import secrets
import socket
import tempfile
from uuid import UUID, uuid4
from datetime import datetime, timezone

import psycopg
from psycopg import sql

from tools.review_deploy_config import PG_OWNERS, PG_RUNTIME, MQTT_ACCOUNTS, DATABASE, runtime_secrets, write_configs
from tools.review_deploy_paths import claim_target, private_acl
from tools.review_deploy_pg import (initialize_cluster, install_schemas, stop_cluster,
    cluster_running, probe_system_identifier, connection_settings)
from backend.review_runtime import initialize_identity


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class MonitoringDatabase:
    def __init__(self, *, root=None):
        self.root = Path(root) if root is not None else Path(tempfile.gettempdir()) / ('oilwell-monitoring-test-' + uuid4().hex)
        self.system_id = None
        self.port = None
        self.credentials = {}

    def __enter__(self):
        # claim_target performs the NEW/exact/absolute/no-links checks before any DDL.
        claim_target(self.root, forbidden=(Path.cwd(), Path.home()))
        for name in ('logs', 'private/inversion-staging'):
            (self.root / name).mkdir(exist_ok=True)
            private_acl(self.root / name)
        initialize_identity(self.root / 'private/identity.db')
        self.port = free_port()
        self.credentials = {name: secrets.token_urlsafe(32) for name in (*PG_OWNERS, *PG_RUNTIME, *MQTT_ACCOUNTS)}
        self.source_ref = str(uuid4())
        write_configs(self.root, ports={'http': free_port(), 'postgres': self.port, 'mqtt': free_port()},
                      credentials={key: self.credentials[key] for key in runtime_secrets()}, source_ref=self.source_ref)
        try:
            self.system_id = initialize_cluster(self.root, port=self.port, credentials=self.credentials)
            install_schemas(port=self.port, credentials=self.credentials,
                center_config=self.root / 'private/center.toml', waveform_config=self.root / 'private/waveform.toml',
                inversion_config=self.root / 'private/inversion.toml')
            self.credentials['monitoring_reader'] = secrets.token_urlsafe(32)
            with self.connect('bootstrap') as conn:
                conn.execute(sql.SQL('CREATE ROLE review_monitoring_reader LOGIN PASSWORD {} NOINHERIT NOSUPERUSER '
                    'NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS').format(sql.Literal(self.credentials['monitoring_reader'])))
                conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO review_monitoring_reader').format(sql.Identifier(DATABASE)))
                conn.execute('GRANT USAGE ON SCHEMA public TO review_monitoring_reader')
            from backend.monitoring.privileges import grant_reader
            for schema, role in (('public', 'foundation_owner'), ('device_registry', 'registry_owner'),
                                 ('ingest_reliability', 'reliability_owner')):
                with self.connect(role) as conn:
                    grant_reader(conn, role='review_monitoring_reader', schema=schema)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def connect(self, role: str):
        if self.system_id is None or probe_system_identifier(self.port, self.credentials) != self.system_id:
            raise ValueError('Refusing connection: monitoring test cluster identity mismatch')
        conn = psycopg.connect(**connection_settings(self.port, role, self.credentials[role]))
        if conn.execute('SELECT current_database(),current_user').fetchone() != (DATABASE, 'review_' + role):
            conn.close()
            raise ValueError('Refusing connection: monitoring test database identity mismatch')
        conn.rollback()
        return conn

    def command(self, action, target=None, *, reviewer=False, **body):
        from backend.registry.contracts import Actor, validate_command
        from backend.registry.service import RegistryService
        from backend.registry.transactions import RegistryTransactions
        actor = Actor('platform-local', '202' if reviewer else '101',
                      'reviewer' if reviewer else 'operator', '隔离测试', 'administrator')
        routes = {'well.create': '/wells', 'well.change': f'/wells/{target}/changes',
                  'device.create': '/devices', 'device.change': f'/devices/{target}/changes',
                  'device.state': f'/devices/{target}/state', 'device.bind': f'/devices/{target}/bindings',
                  'device.unbind': f'/devices/{target}/unbind', 'map.create': f'/devices/{target}/point-maps',
                  'map.assign': f'/devices/{target}/point-map-assignment',
                  'map.review': f'/devices/{target}/point-maps/{body.get("map_version", "")}/review'}
        body = {'reason': '独立安装软件测试', 'idempotency_key': str(uuid4())} | body
        command = validate_command(action, UUID(target) if target else None, actor, body,
                                   method='POST', route='/api/registry' + routes[action])
        return RegistryService(RegistryTransactions(lambda: self.connect('registry_writer'))).execute(command)

    def seed(self, scenario='device', *, well_id=None, points=None):
        if scenario != 'device': raise ValueError('Unknown monitoring scenario')
        suffix = uuid4().hex
        if well_id is None:
            well_id = self.command('well.create', area_code='MONITOR-TEST', well_code='W' + suffix,
                                   name='软件测试井')['entity']['id']
        device = self.command('device.create', gateway_id='GW', device_id='D' + suffix,
                              name='软件测试终端', transport='mqtt_direct', usage='commissioning')['entity']
        mapping = self.command('map.create', device['id'], expected_version=device['version'], parent_version=None,
            points=points or [{'point_code': 'load', 'unit': 'kN', 'engineering_min': 0, 'engineering_max': 80}])['entity']
        review = self.command('map.review', device['id'], reviewer=True, expected_version=mapping['device_version'],
                              map_version=mapping['map_version'], sha256=mapping['sha256'], decision='approved')['entity']
        bound = self.command('device.bind', device['id'], expected_version=review['device_version'], well_id=well_id)
        current = self.command('map.assign', device['id'], expected_version=bound['entity']['version'],
                               map_version=mapping['map_version'])['entity']
        current = self.command('device.state', device['id'], reviewer=True, expected_version=current['version'],
                               status='active', check_note='独立安装软件测试')['entity']
        return {'well_id': well_id, 'registry_device_id': device['id'], 'gateway_id': 'GW',
                'device_id': device['device_id'], 'version': current['version'],
                'map_version': mapping['map_version'], 'binding_time': bound['committed_at']}

    def ingest(self, device, samples, *, message_id=None, received_at=None):
        from backend.gateway.contracts import TelemetrySample, UplinkBatch
        from backend.ingest_reliability.contracts import Observation, CapacityLimits
        from backend.ingest_reliability.repository import ReliableRepository
        instant = received_at or datetime.now(timezone.utc)
        readings = []  # Samples are explicit, never silently generated.
        for index, item in enumerate(samples):
            fields = dict(point_code='load', measured_at=instant, sequence=index, value=41.125,
                          quality='good', unit='kN', source='modbus_tcp') | item
            readings.append(TelemetrySample(gateway_id=device['gateway_id'], device_id=device['device_id'], **fields))
        batch = UplinkBatch.create(device['gateway_id'], device['device_id'], 'BOOT-TEST', readings,
                                   message_id=message_id or 'M-' + uuid4().hex, created_at=instant)
        observation = Observation(batch.topic('oilfield'), batch.to_json().encode('utf-8'), 1, False, False, instant)
        result = ReliableRepository(lambda: self.connect('reliable_ingestor'), prefix='oilfield',
            receipt_gateways=frozenset({'GW'}), limits=CapacityLimits(1073741824, 268435456)).ingest(observation)
        if result.status != 'stored': raise AssertionError('Test readings did not reach stored facts')
        return batch.message_id

    def __exit__(self, *_):
        if self.system_id is not None and cluster_running(self.root):
            if probe_system_identifier(self.port, self.credentials) != self.system_id:
                raise ValueError('Refusing shutdown: monitoring test cluster identity mismatch')
            stop_cluster(self.root)
        # Retain owned files/logs for diagnosis. No recursive cleanup or old DB reset.
