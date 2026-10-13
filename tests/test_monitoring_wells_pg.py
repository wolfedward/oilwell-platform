import unittest
from uuid import uuid4
from tests.monitoring_support import MonitoringDatabase

PERMISSIONS = frozenset({'registry.read', 'telemetry.read', 'acquisition.read', 'ingest.read'})


class MonitoringWellsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = MonitoringDatabase().__enter__()
        cls.addClassCleanup(cls.db.__exit__, None, None, None)

    def query(self):
        try:
            from backend.monitoring.queries import MonitoringQueries
        except ImportError: self.fail('Monitoring queries are not implemented')
        return MonitoringQueries(lambda: self.db.connect('monitoring_reader'), environment='test',
            source_ref=self.db.source_ref, online_seconds=120, delayed_seconds=900, clock_tolerance_seconds=300)

    def test_detail_missing_and_optional_permissions(self):
        from backend.monitoring.errors import MonitoringError
        query = self.query()
        with self.assertRaises(MonitoringError) as caught:
            query.read('well', {'well_id': str(uuid4())}, PERMISSIONS)
        self.assertEqual(caught.exception.status, 404)
        device = self.db.seed()
        result = query.read('well', {'well_id': device['well_id']}, frozenset({'registry.read'}))
        self.assertEqual(result['well']['id'], device['well_id'])
        self.assertEqual(result['bindings']['total'], '1')
        self.assertEqual(result['availability'], {name: {'status': 'forbidden'} for name in ('telemetry', 'link', 'cases')})
        with self.assertRaises(MonitoringError) as caught:
            query.read('latest', {'well_id': device['well_id']}, frozenset({'registry.read'}))
        self.assertEqual(caught.exception.status, 403)

    def test_forbidden_summary_is_not_queried_and_optional_failure_is_not_zero(self):
        import psycopg
        query = self.query()
        device = self.db.seed()
        original = query.connect
        touched = []
        class TrackingCursor(psycopg.Cursor):
            def execute(self, statement, params=None, **kwargs):
                touched.append(str(statement))
                return super().execute(statement, params, **kwargs)
        def tracked():
            conn = original()
            conn.cursor_factory = TrackingCursor
            return conn
        query.connect = tracked
        query.read('well', {'well_id': device['well_id']}, frozenset({'registry.read'}))
        self.assertFalse(any('telemetry_samples' in sql or 'ingest_messages' in sql or 'case_metadata' in sql for sql in touched))
        with self.db.connect('foundation_owner') as conn:
            conn.execute('REVOKE SELECT ON public.telemetry_samples FROM review_monitoring_reader')
        try:
            result = query.read('well', {'well_id': device['well_id']}, PERMISSIONS)
            self.assertEqual(result['availability']['telemetry'], {'status': 'unavailable'})
            self.assertEqual(result['availability']['link']['status'], 'ready')
            self.assertEqual(result['availability']['cases']['status'], 'ready')
        finally:
            with self.db.connect('foundation_owner') as conn:
                conn.execute('GRANT SELECT ON public.telemetry_samples TO review_monitoring_reader')

    def test_latest_expected_slots_same_code_and_latest_bad_not_hidden(self):
        query = self.query()
        first = self.db.seed(points=[{'point_code': 'load', 'unit': 'kN'}, {'point_code': 'pressure', 'unit': 'MPa'}])
        second = self.db.seed(well_id=first['well_id'])
        self.db.ingest(first, [{'sequence': 9007199254740993, 'quality': 'good'}])
        self.db.ingest(first, [{'sequence': 1, 'quality': 'bad', 'unit': 'N', 'value': 50.1234567}])
        result = query.read('latest', {'well_id': first['well_id']}, PERMISSIONS)
        self.assertEqual(result['total'], '3')
        loads = [x for x in result['items'] if x['point_code'] == 'load']
        self.assertEqual(len(loads), 2)
        observed = next(x for x in loads if x['registry_device_id'] == first['registry_device_id'])['observation']
        self.assertEqual(observed['quality'], 'bad')
        self.assertIn('unit_mismatch', observed['reasons'])
        self.assertFalse(observed['eligible_for_automation'])
        self.assertEqual(next(x for x in loads if x['registry_device_id'] == second['registry_device_id'])['observation'], None)
        self.assertTrue(all(x['interval_basis'] == 'unknown' and x['interval_seconds'] is None for x in result['items']))

    def test_bindings_current_history_and_revision_conflict(self):
        from backend.monitoring.errors import MonitoringError
        query = self.query()
        first = self.db.seed()
        self.db.seed(well_id=first['well_id'])
        initial = query.read('well', {'well_id': first['well_id'], 'page_size': '1'}, PERMISSIONS)
        self.assertEqual(initial['bindings']['total'], '2')
        context = initial['context']
        filters = {'well_id': first['well_id'], 'page_size': '1', 'page': '2', 'revision': context['registry_revision'],
                   'received_before': context['received_before'], 'query_total': context['query_total']}
        second = query.read('well', filters, PERMISSIONS)
        self.assertNotEqual(initial['bindings']['items'][0]['id'], second['bindings']['items'][0]['id'])
        self.db.command('device.unbind', first['registry_device_id'], expected_version=first['version'])
        with self.assertRaises(MonitoringError) as caught: query.read('well', filters, PERMISSIONS)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(query.read('well', {'well_id': first['well_id']}, PERMISSIONS)['bindings']['total'], '1')
        self.assertEqual(query.read('well', {'well_id': first['well_id'], 'binding_scope': 'history'}, PERMISSIONS)['bindings']['total'], '2')

    def test_wells_export_matches_registry_filter_and_snapshot(self):
        import csv, io
        from backend.registry.queries import RegistryQueries
        device = self.db.seed()
        detail = self.query().read('well', {'well_id': device['well_id']}, PERMISSIONS)
        registry = RegistryQueries(lambda: self.db.connect('foundation_reader'), online_seconds=120,
                                   delayed_seconds=900, clock_tolerance_seconds=300)
        listing = registry.read('wells', {'well_code': detail['well']['well_code']})
        result = self.query().export('wells_csv', {'well_code': detail['well']['well_code'],
            'revision': listing['registry_revision'], 'received_before': listing['received_before'],
            'query_total': str(listing['total'])}, PERMISSIONS)
        rows = list(csv.DictReader(io.StringIO(result.body.decode('utf-8-sig'))))
        self.assertEqual([x['id'] for x in rows], [x['id'] for x in listing['items']])
