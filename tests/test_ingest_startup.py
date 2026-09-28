"""Optional management startup, explicit grants, and independently held roles."""
from contextlib import contextmanager
from http.client import HTTPConnection
import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_support import IngestDatabaseCase


ROLE_MATRIX = {
    'administrator': {'ingest.read', 'ingest.raw.read', 'ingest.manage'},
    'production_engineer': {'ingest.read'},
    'algorithm_engineer': {'ingest.read', 'ingest.raw.read'},
    'maintenance_engineer': {'ingest.read', 'ingest.raw.read', 'ingest.manage'},
    'reviewer': {'ingest.read'},
}


def module(test):
    try:
        return importlib.import_module('backend.ingest_reliability.runtime')
    except ModuleNotFoundError:
        test.fail('Optional incident management runtime is missing')


def management_toml(sections, *, extra=''):
    text = ('[reliability]\nenabled=true\ntotal_bytes=16777216\nquarantine_bytes=8388608\n'
            'allowed_origins=["http://localhost:80"]\nhealth_stale_seconds=91\n') + extra
    for name, values in sections.items():
        text += '\n[' + name + ']\n'
        for key, value in values.items():
            text += key + '=' + json.dumps(value, ensure_ascii=False) + '\n'
    return text


class RuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.module = module(self)
        self.temp = tempfile.TemporaryDirectory(prefix='ingest-runtime-unit-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'center.toml'

    def config(self, text):
        self.path.write_text(text, encoding='utf-8')
        return self.path

    def valid_text(self):
        common = {'host': '127.0.0.1', 'port': 3822, 'database': 'oilwell_ingest_reliability_test',
                  'sslmode': 'disable', 'password_env': 'INGEST_UNIT_ONLY'}
        return management_toml({'postgres_ingest_reader': common | {'user': 'reader_unit'},
                                'postgres_ingest_operator': common | {'user': 'operator_unit'}})

    def test_disabled_path_or_missing_section_never_opens_a_database(self):
        with patch('psycopg.connect', side_effect=AssertionError('Disabled runtime opened database')):
            for text in (None, '[center]\ntopic_prefix="oilfield"\n', '[reliability]\nenabled=false\n'):
                path = None if text is None else self.config(text)
                result = self.module.build_reliability_runtime(path)
                self.assertEqual(result.status, 'disabled')
                self.assertIsNone(result.queries)
                self.assertIsNone(result.operations)

    def test_disabled_runtime_does_not_require_optional_postgres_driver(self):
        with patch.dict('sys.modules', {'psycopg': None}):
            self.assertEqual(self.module.build_reliability_runtime(None).status, 'disabled')

    def test_bad_configuration_is_unavailable_not_disabled_and_hides_details(self):
        for text in ('[broken', '[reliability]\nenabled=true\n', self.valid_text().replace('3822', 'true')):
            with self.assertLogs('backend.ingest_reliability.runtime', level='WARNING'):
                result = self.module.build_reliability_runtime(self.config(text))
            self.assertEqual(result.status, 'unavailable')
            self.assertEqual(result.code, 'ingest.config_invalid')
            self.assertIsNone(result.queries)
            self.assertNotIn('INGEST_UNIT_ONLY', repr(result))

    def test_management_loads_only_reader_operator_and_preserves_health_configuration(self):
        path = self.config(self.valid_text())
        with patch.dict(os.environ, {'INGEST_UNIT_ONLY': 'private-unit-value'}), \
             patch('backend.ingest_reliability.runtime.probe_privileges') as probe:
            result = self.module.build_reliability_runtime(path)
        self.assertEqual(result.status, 'ready')
        self.assertEqual(result.queries.health_stale_seconds, 91)
        self.assertEqual(result.allowed_origins, ('http://localhost',))
        self.assertIsNotNone(result.operations)
        self.assertNotIn('private-unit-value', repr(result))
        self.assertEqual(probe.call_count, 1)

    def test_failed_readonly_preflight_keeps_services_unreachable_and_logs_only_safe_code(self):
        with patch.dict(os.environ, {'INGEST_UNIT_ONLY': 'private-unit-value'}), \
             patch('backend.ingest_reliability.runtime.probe_privileges', side_effect=ReliabilityError('ingest.privilege_invalid', 'secret diagnostic')):
            with self.assertLogs('backend.ingest_reliability.runtime', level='WARNING') as captured:
                result = self.module.build_reliability_runtime(self.config(self.valid_text()))
        self.assertEqual((result.status, result.code), ('unavailable', 'ingest.privilege_invalid'))
        self.assertIsNone(result.queries)
        self.assertIsNone(result.operations)
        self.assertNotIn('secret diagnostic', ''.join(captured.output))
        self.assertNotIn('private-unit-value', repr(result))

    def test_unexpected_connection_failure_is_sanitized(self):
        with patch.dict(os.environ, {'INGEST_UNIT_ONLY': 'private-unit-value'}), \
             patch('backend.ingest_reliability.runtime.probe_privileges', side_effect=RuntimeError('password=private-unit-value')), \
             self.assertLogs('backend.ingest_reliability.runtime', level='WARNING'):
            result = self.module.build_reliability_runtime(self.config(self.valid_text()))
        self.assertEqual((result.status, result.code), ('unavailable', 'ingest.connection_unavailable'))
        self.assertNotIn('private-unit-value', repr(result))

    def test_enabled_module_without_driver_reports_dependency_unavailable(self):
        with patch.dict(os.environ, {'INGEST_UNIT_ONLY': 'private-unit-value'}), \
             patch.dict('sys.modules', {'psycopg': None}), \
             self.assertLogs('backend.ingest_reliability.runtime', level='WARNING'):
            result = self.module.build_reliability_runtime(self.config(self.valid_text()))
        self.assertEqual((result.status, result.code), ('unavailable', 'ingest.dependency_unavailable'))
        self.assertIsNone(result.queries)


class PermissionContractTests(unittest.TestCase):
    def setUp(self):
        try:
            self.permissions = importlib.import_module('backend.ingest_reliability.permissions')
        except ModuleNotFoundError:
            self.fail('Incident permission matrix is missing')

    def test_default_roles_have_only_the_specified_incident_permissions(self):
        self.assertEqual({key: set(value) for key, value in self.permissions.INGEST_ROLE_PERMISSIONS.items()}, ROLE_MATRIX)

    def test_every_operation_requires_base_read_and_its_exact_additional_permission(self):
        for action in ('cases', 'case', 'receipts', 'receipt', 'health'):
            self.assertEqual(set(self.permissions.required_permissions(action)), {'ingest.read'})
        for action in ('raw.preview', 'raw.download'):
            self.assertEqual(set(self.permissions.required_permissions(action)), {'ingest.read', 'ingest.raw.read'})
        for action in ('case.ack', 'case.close', 'case.reopen', 'case.note', 'receipt.resend'):
            self.assertEqual(set(self.permissions.required_permissions(action)), {'ingest.read', 'ingest.manage'})
        self.assertEqual(set(self.permissions.required_permissions('audit')), {'ingest.read', 'audit.read'})
        with self.assertRaises(ReliabilityError):
            self.permissions.required_permissions('execute')


class LiveManagementStartupTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        self.module = module(self)
        self.temp = tempfile.TemporaryDirectory(prefix='ingest-management-config-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'center.toml'
        self.path.write_text(management_toml({
            'postgres_ingest_reader': self.raw_config['postgres_reader'],
            'postgres_ingest_operator': self.raw_config['postgres_operator']}), encoding='utf-8')

    def test_readonly_startup_keeps_migration_history_and_existing_facts_unchanged(self):
        from tests.ingest_vectors import observation
        fact = self.repository().ingest(observation('STARTUP'))
        history = self.guard.execute('SELECT * FROM ingest_reliability.schema_migrations ORDER BY version').fetchall()
        before = self.guard.execute('SELECT * FROM ingest_reliability.facts WHERE fact_id=%s', (fact.fact_id,)).fetchone()
        result = self.module.build_reliability_runtime(self.path)
        self.assertEqual(result.status, 'ready')
        self.assertEqual(result.queries.read('receipts', {})['total'], '1')
        self.assertEqual(self.guard.execute('SELECT * FROM ingest_reliability.schema_migrations ORDER BY version').fetchall(), history)
        self.assertEqual(self.guard.execute('SELECT * FROM ingest_reliability.facts WHERE fact_id=%s', (fact.fact_id,)).fetchone(), before)

    def test_missing_operator_privilege_never_exposes_a_partial_ready_runtime(self):
        from psycopg import sql
        with self.admin() as conn:
            conn.execute(sql.SQL('REVOKE INSERT ON ingest_reliability.audit_events FROM {}').format(sql.Identifier(self.settings['operator']['user'])))
        try:
            result = self.module.build_reliability_runtime(self.path)
            self.assertEqual(result.status, 'unavailable')
            self.assertIsNone(result.operations)
            self.assertIsNone(result.queries)
        finally:
            with self.connect('migrator') as conn:
                self.migrations.grant_roles(conn, roles={role: self.settings[role]['user'] for role in ('ingestor', 'publisher', 'reader', 'operator')})

    def test_missing_metadata_structure_is_reported_not_implicitly_recreated(self):
        with self.connect('migrator') as conn:
            conn.execute('ALTER VIEW ingest_reliability.worker_health_metadata RENAME TO held_health_metadata')
        try:
            result = self.module.build_reliability_runtime(self.path)
            self.assertEqual(result.status, 'unavailable')
            self.assertIsNone(self.scalar("SELECT to_regclass('ingest_reliability.worker_health_metadata')", role='migrator'))
        finally:
            with self.connect('migrator') as conn:
                conn.execute('ALTER VIEW ingest_reliability.held_health_metadata RENAME TO worker_health_metadata')


class PlatformCapabilityTests(unittest.TestCase):
    @contextmanager
    def platform(self, **kwargs):
        from backend.http_server import create_server
        with tempfile.TemporaryDirectory(prefix='ingest-platform-startup-') as directory:
            server = create_server('127.0.0.1', 0, Path(directory) / 'platform.db', **kwargs)
            token = server.auth.login('administrator', 'Admin@2026').token
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def http(path, authorized=True):
                conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                try:
                    conn.request('GET', path, headers={'Authorization': 'Bearer ' + token} if authorized else {})
                    response = conn.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    conn.close()
            try:
                yield server, http
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_disabled_module_keeps_auth_and_old_health_without_connecting_optional_database(self):
        with self.platform() as (server, http):
            self.assertEqual(server.ingest_runtime.status, 'disabled')
            self.assertEqual(http('/api/health')[0], 200)
            caps = http('/api/auth/me')[1]['data']['capabilities']
            self.assertEqual(caps['ingest_reliability'], {'enabled': False, 'status': 'disabled'})
            self.assertIn('registry', caps)
            self.assertEqual(http('/api/ingest-reliability/cases')[0], 503)
            self.assertEqual(http('/api/auth/me', False)[0], 401)

    def test_unavailable_module_keeps_registry_capability_independent(self):
        from backend.registry.runtime import RegistryRuntime
        runtime = module(self).ReliabilityRuntime('unavailable', code='ingest.schema_incomplete')
        with self.platform(ingest_runtime=runtime, registry_runtime=RegistryRuntime('disabled')) as (_, http):
            caps = http('/api/auth/me')[1]['data']['capabilities']
            self.assertEqual(caps, {'ingest_reliability': {'enabled': True, 'status': 'unavailable'},
                                    'registry': {'enabled': False, 'status': 'disabled'}})
            status, result = http('/api/ingest-reliability/cases')
            self.assertEqual((status, result['error']['code']), (503, 'ingest.schema_incomplete'))
            self.assertNotIn('data', result)
            self.assertEqual(http('/api/health')[0], 200)

    def test_broken_management_config_does_not_break_existing_center_query_setup(self):
        with tempfile.TemporaryDirectory(prefix='ingest-old-query-') as directory:
            path = Path(directory) / 'center.toml'
            path.write_text('[reliability]\nenabled=true\n'
                '[postgres]\nhost="127.0.0.1"\nport=3822\ndatabase="oilwell_registry_test"\nuser="old_ingest"\npassword_env="INGEST_NOT_READ"\nsslmode="disable"\n'
                '[postgres_query]\nhost="127.0.0.1"\nport=3822\ndatabase="oilwell_registry_test"\nuser="old_reader"\npassword_env="OLD_QUERY_UNIT_ONLY"\nsslmode="disable"\n', encoding='utf-8')
            with patch.dict(os.environ, {'OLD_QUERY_UNIT_ONLY': 'private-query-test-only'}):
                with self.platform(center_config_path=path) as (server, http):
                    self.assertIsNotNone(server.central_telemetry)
                    self.assertEqual(server.ingest_runtime.status, 'unavailable')
                    self.assertEqual(http('/api/health')[0], 200)
                    status, result = http('/api/ingest-reliability/cases')
                    self.assertEqual((status, result['error']['code']), (503, 'ingest.config_invalid'))
                    self.assertNotIn('private-query-test-only', json.dumps(result))


class PermissionBootstrapTests(unittest.TestCase):
    def test_upgrade_grants_new_permissions_once_and_preserves_later_revocation(self):
        from backend.database import Database
        from backend.seed import seed_database, bootstrap_local_auth
        with tempfile.TemporaryDirectory(prefix='ingest-permissions-') as directory:
            db = Database(Path(directory) / 'auth.db')
            db.migrate()
            seed_database(db)
            def permissions(role):
                return set(db.column('SELECT p.code FROM role_permissions rp JOIN roles r ON r.id=rp.role_id '
                                     'JOIN permissions p ON p.id=rp.permission_id WHERE r.code=?', (role,)))
            for role, expected in ROLE_MATRIX.items():
                self.assertEqual({code for code in permissions(role) if code.startswith('ingest.')}, expected)
            with db.connection() as conn:
                conn.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code LIKE 'ingest.%')")
                conn.execute('UPDATE auth_bootstrap_state SET bootstrap_version=8 WHERE id=1')
            bootstrap_local_auth(db)
            self.assertIn('ingest.manage', permissions('maintenance_engineer'))
            with db.connection() as conn:
                conn.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='maintenance_engineer') "
                             "AND permission_id=(SELECT id FROM permissions WHERE code='ingest.manage')")
                conn.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='administrator') "
                             "AND permission_id=(SELECT id FROM permissions WHERE code='ingest.manage')")
            bootstrap_local_auth(db)
            self.assertNotIn('ingest.manage', permissions('maintenance_engineer'))
            self.assertIn('ingest.manage', permissions('administrator'))
