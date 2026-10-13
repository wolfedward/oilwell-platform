import csv
from datetime import datetime, timedelta, timezone
import io
import unittest
from tests.test_monitoring_wells_pg import PERMISSIONS
from tests.monitoring_support import MonitoringDatabase


class MonitoringHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = MonitoringDatabase().__enter__()
        cls.addClassCleanup(cls.db.__exit__, None, None, None)

    def query(self):
        try: from backend.monitoring.queries import MonitoringQueries
        except ImportError: self.fail('Monitoring queries are not implemented')
        return MonitoringQueries(lambda: self.db.connect('monitoring_reader'), environment='test',
            source_ref=self.db.source_ref, online_seconds=120, delayed_seconds=900, clock_tolerance_seconds=300)

    def filters(self, device):
        instant = datetime.now(timezone.utc)
        return {'well_id': device['well_id'], 'registry_device_id': device['registry_device_id'], 'point_code': 'load',
                'from_time': (instant - timedelta(days=1)).isoformat(), 'to_time': (instant + timedelta(hours=1)).isoformat()}

    def test_history_filter_trend_export_cutoff_and_large_sequence(self):
        query = self.query()
        device = self.db.seed()
        instant = datetime.now(timezone.utc)
        self.db.ingest(device, [{'measured_at': instant + timedelta(microseconds=i), 'sequence': 9007199254740993+i,
            'value': 41.123456789+i/1000, 'quality': 'bad' if i == 601 else 'good'} for i in range(602)])
        filters = self.filters(device) | {'page_size': '200', 'quality': 'good'}
        result = query.read('history', filters, PERMISSIONS)
        self.assertEqual(result['total'], '601')
        self.assertEqual(result['trend']['point_count'], 600)
        self.assertTrue(result['trend']['truncated'])
        self.assertEqual(result['items'][0]['sequence'], str(9007199254740993+600))
        self.assertEqual(result['items'][0]['quality'], 'good')
        self.assertEqual(result['trend']['items'][-1]['sample_index'], 600)
        context = {'revision': result['context']['registry_revision'], 'received_before': result['context']['received_before'], 'query_total': '601'}
        self.db.ingest(device, [{'measured_at': instant, 'sequence': 5}])
        identifiers = [(x['message_id'], x['sample_index']) for x in result['items']]
        for page in ('2', '3', '4'):
            data = query.read('history', filters | context | {'page': page}, PERMISSIONS)
            identifiers += [(x['message_id'], x['sample_index']) for x in data['items']]
        export_filters = {k:v for k,v in filters.items() if k != 'page_size'} | context
        exported = query.export('history_csv', export_filters, PERMISSIONS)
        rows = list(csv.DictReader(io.StringIO(exported.body.decode('utf-8-sig'))))
        self.assertEqual([(x['message_id'], int(x['sample_index'])) for x in rows], identifiers)
        self.assertEqual(rows[0]['sequence'], str(9007199254740993+600))
        self.assertEqual(float(rows[0]['value']), result['items'][0]['value'])

    def test_historical_binding_half_open_and_stable_tie_order(self):
        query = self.query()
        device = self.db.seed()
        before = datetime.now(timezone.utc)
        self.db.ingest(device, [{'measured_at': before, 'sequence': 1000}])
        next_well = self.db.command('well.create', area_code='MONITOR-TEST', well_code='NEW-'+device['device_id'], name='迁入井')['entity']
        moved = self.db.command('device.bind', device['registry_device_id'], expected_version=device['version'], well_id=next_well['id'])
        boundary = datetime.fromisoformat(moved['committed_at'])
        ids = ['M-Z-'+device['device_id'], 'M-A-'+device['device_id']]
        for identifier in ids:
            self.db.ingest(device, [{'measured_at': boundary, 'sequence': 1}, {'measured_at': boundary, 'sequence': 2}], message_id=identifier)
        self.db.ingest(device, [{'measured_at': boundary-timedelta(microseconds=1), 'sequence': 0}])
        old = query.read('history', self.filters(device), PERMISSIONS)
        new = query.read('history', self.filters(device) | {'well_id': next_well['id']}, PERMISSIONS)
        self.assertEqual(old['total'], '2')
        self.assertEqual(new['total'], '4')
        self.assertEqual([(x['message_id'], x['sample_index']) for x in new['items']], [(ids[0],1),(ids[0],0),(ids[1],1),(ids[1],0)])

    def test_changed_total_conflicts_even_with_older_received_time(self):
        from backend.monitoring.errors import MonitoringError
        query = self.query()
        device = self.db.seed()
        received = datetime.now(timezone.utc)
        self.db.ingest(device, [{}], received_at=received)
        filters = self.filters(device)
        first = query.read('history', filters, PERMISSIONS)
        self.db.ingest(device, [{}], received_at=received)
        with self.assertRaises(MonitoringError) as caught:
            query.read('history', filters | {'revision': first['context']['registry_revision'],
                'received_before': first['context']['received_before'], 'query_total': '1'}, PERMISSIONS)
        self.assertEqual(caught.exception.status, 409)
