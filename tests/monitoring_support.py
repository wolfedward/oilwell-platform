"""Own a fresh cluster per integration fixture; never adopt or reset an old DB."""
from pathlib import Path
import secrets
import socket
import tempfile
from uuid import uuid4

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

    def __exit__(self, *_):
        if self.system_id is not None and cluster_running(self.root):
            if probe_system_identifier(self.port, self.credentials) != self.system_id:
                raise ValueError('Refusing shutdown: monitoring test cluster identity mismatch')
            stop_cluster(self.root)
        # Retain owned files/logs for diagnosis. No recursive cleanup or old DB reset.
