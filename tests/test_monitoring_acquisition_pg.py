from datetime import datetime, timedelta, timezone
import json
import unittest
from tests.monitoring_support import MonitoringDatabase
from tests.test_monitoring_wells_pg import PERMISSIONS


class MonitoringAcquisitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = MonitoringDatabase().__enter__()
        cls.addClassCleanup(cls.db.__exit__, None, None, None)

    def query(self):
        from backend.monitoring.queries import MonitoringQueries
        return MonitoringQueries(lambda: self.db.connect('monitoring_reader'), environment='test',
            source_ref=self.db.source_ref, online_seconds=120, delayed_seconds=900, clock_tolerance_seconds=300)

    def test_stored_duplicate_is_one_fact_no_raw_and_separate_receipt_state(self):
        device = self.db.seed()
        now = datetime.now(timezone.utc)
        message = self.db.ingest(device, [{}, {'sequence': 1}], received_at=now)
        self.db.ingest(device, [{}, {'sequence': 1}], message_id=message, received_at=now)
        result = self.query().read('messages', {'device_id': device['device_id']}, PERMISSIONS)
        self.assertEqual(result['total'], '1')
        row = result['items'][0]
        self.assertEqual(row['sample_count'], '2')
        self.assertEqual(row['ingest_status'], 'stored')
        self.assertEqual(row['processing']['duplicate_count'], '1')
        self.assertEqual(row['processing']['receipt_status'], 'stored')
        self.assertEqual(row['processing']['delivery_state'], 'pending')
        self.assertNotIn('raw_payload', json.dumps(result))
        self.assertNotIn('payload"', json.dumps(result))
        self.assertEqual(row['attribution']['well_count'], '1')
        from backend.ingest_reliability.outbox import OutboxRepository
        outbox = OutboxRepository(lambda: self.db.connect('reliable_publisher'))
        lease = next(lease for lease in outbox.claim(limit=100) if str(lease.receipt_id)==row['processing']['receipt_id'])
        outbox.finish(lease, success=True)  # Repository state test; not a network-delivery claim.
        after = self.query().read('message', {'id':message}, PERMISSIONS)
        self.assertEqual(after['ingest_status'], 'stored')
        self.assertEqual(after['processing']['delivery_state'], 'broker_confirmed')

    def test_message_mixed_historical_wells_with_independent_well_paging(self):
        device = self.db.seed()
        before = datetime.now(timezone.utc)
        new_well = self.db.command('well.create', area_code='MONITOR-TEST', well_code='MOVE-'+device['device_id'], name='迁入井')['entity']
        move = self.db.command('device.bind', device['registry_device_id'], expected_version=device['version'], well_id=new_well['id'])
        at = datetime.fromisoformat(move['committed_at'])
        message = self.db.ingest(device, [{'measured_at': before}, {'measured_at': at, 'sequence': 1}])
        first = self.query().read('message', {'id': message, 'page_size': '1'}, PERMISSIONS)
        self.assertEqual(first['sample_count'], '2')
        self.assertEqual(first['well_count'], '2')
        self.assertEqual(first['unassigned_count'], '0')
        second = self.query().read('message', {'id': message, 'page_size': '1', 'page': '2',
            'revision': first['context']['registry_revision'], 'received_before': first['context']['received_before'],
            'query_total': first['context']['query_total']}, PERMISSIONS)
        self.assertEqual(set(first['well_ids']+second['well_ids']), {device['well_id'],new_well['id']})
        self.assertEqual(second['sample_count'], '2')

    def test_missing_optional_permissions_never_query_or_leak_related_sections(self):
        import psycopg
        device = self.db.seed()
        self.db.ingest(device, [{}])
        touched = []
        class TrackingCursor(psycopg.Cursor):
            def execute(self, statement, params=None, **kwargs):
                touched.append(str(statement))
                return super().execute(statement, params, **kwargs)
        query = self.query()
        original = query.connect
        def tracked():
            conn = original(); conn.cursor_factory = TrackingCursor; return conn
        query.connect = tracked
        result = query.read('messages', {'device_id': device['device_id']}, frozenset({'acquisition.read'}))
        row = result['items'][0]
        self.assertEqual(row['processing'], {'status': 'forbidden'})
        self.assertEqual(row['attribution'], {'status': 'forbidden'})
        self.assertFalse(any('ingest_reliability.' in sql or 'well_bindings' in sql for sql in touched))

    def test_quarantine_is_not_a_stored_message_and_unassigned_is_visible(self):
        from backend.monitoring.errors import MonitoringError
        device = {'gateway_id': 'GW', 'device_id': 'UNKNOWN-'+str(datetime.now().timestamp()).replace('.','')}
        message = self.db.ingest(device, [{}])
        self.db.quarantine(device)
        result = self.query().read('messages', {'device_id': device['device_id']}, PERMISSIONS)
        self.assertEqual(result['total'], '1')
        detail = self.query().read('message', {'id': message}, PERMISSIONS)
        self.assertEqual(detail['well_ids'], [])
        self.assertEqual(detail['unassigned_count'], '1')
        with self.assertRaises(MonitoringError) as caught:
            self.query().read('message', {'id': 'x'*129}, PERMISSIONS)
        self.assertEqual(caught.exception.status, 422)
