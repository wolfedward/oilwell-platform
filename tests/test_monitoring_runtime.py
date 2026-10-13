"""Strict query/config contracts, with no credentials or live service fallback."""
from importlib import import_module
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from uuid import uuid4


class MonitoringContractTests(unittest.TestCase):
    def setUp(self):
        try:
            self.contracts = import_module('backend.monitoring.contracts')
            self.errors = import_module('backend.monitoring.errors')
        except ImportError:
            self.fail('Monitoring query contracts are not implemented')
        self.history = {'well_id': str(uuid4()), 'registry_device_id': str(uuid4()),
                        'point_code': 'load', 'from_time': '2026-10-01T00:00:00Z',
                        'to_time': '2026-11-01T00:00:00Z'}

    def test_query_limits(self):
        result = self.contracts.validate_filters('history', self.history | {'page_size': '200'})
        self.assertEqual(result['page_size'], 200)
        for change in ({'page_size': '201'}, {'page': '100002', 'page_size': '1'},
                       {'to_time': '2026-11-01T00:00:00.000001Z'},
                       {'from_time': '2026-10-01T00:00:00'}, {'quality': 'fixed'},
                       {'point_code': ''}, {'registry_device_id': 'W-001'},
                       {'unknown': 'x'}, {'page_size': True}):
            with self.subTest(change=change), self.assertRaises(self.errors.MonitoringError):
                self.contracts.validate_filters('history', self.history | change)
        with self.assertRaises(self.errors.MonitoringError):
            self.contracts.validate_filters('latest', {'well_id': str(uuid4()), 'page_size': '33'})

    def test_context_and_large_counts(self):
        context = {'revision': '9007199254740993', 'received_before': '2026-11-01T00:00:00Z',
                   'query_total': '9007199254740994'}
        result = self.contracts.validate_filters('history', self.history | context | {'page': '2'})
        self.assertEqual(result['revision'], '9007199254740993')
        self.assertEqual(result['query_total'], '9007199254740994')
        for missing in context:
            with self.subTest(missing=missing), self.assertRaises(self.errors.MonitoringError):
                self.contracts.validate_filters('history', self.history | {'page': '2'} |
                                                 {k: v for k, v in context.items() if k != missing})

    def test_duplicate_and_malformed_query_rejected(self):
        for query in ('page=1&page=2', 'unknown=x', 'page=%GG', 'page=%FF', 'page', 'page=1&&page_size=2'):
            with self.subTest(query=query), self.assertRaises(self.errors.MonitoringError):
                self.contracts.parse_filters('messages', query)
        self.assertEqual(self.contracts.parse_filters('messages', 'gateway_id=GW&device_id=D'),
                         {'gateway_id': 'GW', 'device_id': 'D'})


class MonitoringConfigTests(unittest.TestCase):
    def setUp(self):
        try:
            self.config = import_module('backend.monitoring.config')
            self.errors = import_module('backend.monitoring.errors')
        except ImportError:
            self.fail('Independent monitoring config is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'center.toml'
        self.source = str(uuid4())
        self.content = (f'[monitoring]\nenabled=true\nenvironment="test"\nsource_ref="{self.source}"\n'
            '[postgres_monitoring]\nhost="127.0.0.1"\nport=55436\ndatabase="monitoring_test"\n'
            'user="monitor_reader"\npassword_env="MONITOR_TEST_PASSWORD"\nsslmode="disable"\n'
            '[postgres]\nhost="127.0.0.1"\nport=55436\ndatabase="monitoring_test"\nuser="ingest"\n')

    def load(self, text, env=None):
        self.path.write_text(text, encoding='utf-8')
        return self.config.load_monitoring_config(self.path, env=env or {'MONITOR_TEST_PASSWORD': 'test-only-secret'})

    def test_disabled_never_falls_back(self):
        self.assertIsNone(self.config.load_monitoring_config(None))
        self.assertIsNone(self.load('[monitoring]\nenabled=false\n[postgres]\npassword="not-read"'))
        with self.assertRaises(self.errors.MonitoringError):
            self.load(self.content.replace('[postgres_monitoring]', '[postgres_query]'))

    def test_runtime_disabled_unavailable_and_thresholds(self):
        from backend.monitoring.runtime import build_monitoring_runtime
        self.assertEqual(build_monitoring_runtime(None).status, 'disabled')
        settings = SimpleNamespace(postgres={'user': 'dedicated'}, environment='test', source_ref=self.source)
        thresholds = SimpleNamespace(online_seconds=5, delayed_seconds=12, clock_tolerance_seconds=9)
        with patch('backend.monitoring.runtime.load_monitoring_config', return_value=settings), \
             patch('backend.registry.config.load_registry_config', return_value=thresholds), \
             patch('backend.monitoring.privileges.probe_reader') as probe:
            runtime = build_monitoring_runtime(self.path)
            self.assertEqual(runtime.status, 'ready')
            self.assertEqual(runtime.query.online_seconds, 5)
            self.assertEqual(runtime.query.delayed_seconds, 12)
            self.assertEqual(runtime.query.clock_tolerance_seconds, 9)
            probe.side_effect = RuntimeError('secret-connection-value')
            with self.assertLogs('backend.monitoring.runtime', level='WARNING') as captured:
                runtime = build_monitoring_runtime(self.path)
            self.assertEqual(runtime.status, 'unavailable')
            self.assertIsNone(runtime.query)
            self.assertNotIn('secret-connection-value', repr(runtime) + repr(captured.output))

    def test_separate_identity_and_no_password_repr(self):
        config = self.load(self.content)
        self.assertEqual(config.environment, 'test')
        self.assertEqual(config.source_ref, self.source)
        self.assertEqual(config.postgres['user'], 'monitor_reader')
        self.assertNotIn('test-only-secret', repr(config))
        for text in (self.content.replace('user="monitor_reader"', 'user="ingest"'),
                     self.content.replace('environment="test"', 'environment="demo"'),
                     self.content.replace('password_env="MONITOR_TEST_PASSWORD"', 'password="inline"'),
                     self.content.replace('port=55436', 'port=true', 1),
                     self.content.replace('database="monitoring_test"', 'database="other"', 1)):
            with self.subTest(text=text.splitlines()[0]), self.assertRaises(self.errors.MonitoringError):
                self.load(text)

    def test_fixture_refuses_existing_cluster(self):
        try:
            from tests.monitoring_support import MonitoringDatabase
        except ImportError:
            self.fail('Isolated monitoring database fixture is not implemented')
        sentinel = Path(self.tmp.name) / 'preserve.txt'
        sentinel.write_text('preserve', encoding='utf-8')
        with self.assertRaises(ValueError):
            with MonitoringDatabase(root=Path(self.tmp.name)):
                self.fail('Existing target adopted')
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'preserve')
