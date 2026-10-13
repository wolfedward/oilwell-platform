from datetime import datetime, timedelta, timezone
import json
import unittest
from uuid import uuid4
from tests.monitoring_support import MonitoringDatabase
from tests.test_monitoring_wells_pg import PERMISSIONS

ALL = PERMISSIONS | {'data_quality.read'}


class MonitoringQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = MonitoringDatabase().__enter__()
        cls.addClassCleanup(cls.db.__exit__, None, None, None)

    def query(self):
        from backend.monitoring.queries import MonitoringQueries
        return MonitoringQueries(lambda: self.db.connect('monitoring_reader'), environment='test',
            source_ref=self.db.source_ref, online_seconds=120, delayed_seconds=900, clock_tolerance_seconds=300)

    def test_three_views_preserve_raw_quality_and_never_authorize_automation(self):
        device = self.db.seed()
        self.db.ingest(device, [{}, {'sequence': 1, 'quality': 'bad'}, {'sequence': 2, 'unit': 'N'},
            {'sequence': 3, 'point_code': 'unknown'}, {'sequence': 4, 'value': 1000},
            {'sequence': 5, 'measured_at': datetime.now(timezone.utc)+timedelta(hours=1)}])
        self.db.quarantine(device)
        filters = {'device_id': device['device_id']}
        query = self.query()
        raw = query.read('quality', filters | {'view':'observations', 'quality': 'bad'}, ALL)
        self.assertEqual(raw['total'], '1')
        self.assertEqual(raw['scope'], 'reported_observation_quality')
        resolved = query.read('quality', filters | {'view':'resolution'}, ALL)
        self.assertEqual(resolved['total'], '6')
        reasons = {reason for item in resolved['items'] for reason in item['reasons']}
        self.assertTrue({'unit_mismatch','unknown_point','out_of_range','clock_anomaly','quality_bad'} <= reasons)
        self.assertTrue(all(not item['eligible_for_automation'] and item['application_status']=='unverified' for item in resolved['items']))
        specific = query.read('quality', filters | {'view':'resolution','reason_code':'unit_mismatch','page_size':'1'}, ALL)
        self.assertEqual(specific['total'], '1')
        self.assertEqual(specific['items'][0]['unit'], 'N')
        cases = query.read('quality', filters | {'view':'cases'}, ALL)
        self.assertEqual(cases['total'], '1')
        self.assertEqual(cases['scope'], 'current_case_state')
        self.assertIn('status_queried_at', cases)
        self.assertNotIn('raw_payload', json.dumps(cases))
        self.assertNotIn('fault_total', cases)

    def test_unregistered_unbound_and_well_filter_do_not_erase_unresolved(self):
        query = self.query()
        device = {'gateway_id': 'GW', 'device_id': 'U-'+uuid4().hex}
        self.db.ingest(device, [{}])
        unresolved = query.read('quality', {'view':'resolution','device_id':device['device_id'],'unresolved':'true'}, ALL)
        self.assertEqual(unresolved['total'], '1')
        self.assertIn('not_registered', unresolved['items'][0]['reasons'])
        known = self.db.seed()
        self.db.command('device.unbind', known['registry_device_id'], expected_version=known['version'])
        self.db.ingest(known, [{}])
        result = query.read('quality', {'view':'resolution','device_id':known['device_id'],'reason_code':'unbound'}, ALL)
        self.assertEqual(result['total'], '1')
        selected = query.read('quality', {'view':'resolution','well_id':known['well_id']}, ALL)
        self.assertEqual(selected['total'], '0')
        self.assertEqual(selected['coverage'], 'selected_well_only')
        self.assertTrue(selected['unresolved_available_separately'])

    def test_case_state_change_between_pages_conflicts(self):
        from backend.monitoring.errors import MonitoringError
        device = self.db.seed()
        one = self.db.quarantine(device)
        self.db.quarantine(device)
        filters = {'view':'cases','device_id':device['device_id'],'state':'open','page_size':'1'}
        first = self.query().read('quality', filters, ALL)
        self.assertEqual(first['total'], '2')
        self.db.case_action('close', one['case_id'])
        with self.assertRaises(MonitoringError) as caught:
            self.query().read('quality', filters | {'page':'2','revision':first['context']['registry_revision'],
                'received_before':first['context']['received_before'],'query_total':'2'}, ALL)
        self.assertEqual(caught.exception.status, 409)

    def test_permissions_before_connection_and_sanitized_unavailable(self):
        import psycopg
        from backend.monitoring.errors import MonitoringError
        query = self.query()
        calls = []
        def unavailable():
            calls.append(True)
            raise psycopg.OperationalError('password=secret-test-value')
        query.connect = unavailable
        for filters, permissions in [({'view':'observations'}, {'data_quality.read'}),
            ({'view':'resolution'}, {'data_quality.read','telemetry.read'}),
            ({'view':'cases'}, {'data_quality.read'}),
            ({'view':'cases','well_id':str(uuid4())}, {'ingest.read'})]:
            with self.assertRaises(MonitoringError) as caught:
                query.read('quality', filters, frozenset(permissions))
            self.assertEqual(caught.exception.status, 403)
        self.assertEqual(calls, [])
        with self.assertRaises(MonitoringError) as caught: query.read('quality', {'view':'cases'}, ALL)
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn('secret-test-value', str(caught.exception))
