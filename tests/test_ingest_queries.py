"""Metadata-only reader queries, fail-closed filters and honest health."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import json
import unittest
from uuid import uuid4

import psycopg

from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_support import IngestDatabaseCase
from tests.ingest_vectors import observation


class QueryInputTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('backend.ingest_reliability.queries')
        def forbidden():
            self.fail('Invalid queries must fail before touching database credentials or connections')
        self.queries = self.module.ReliabilityQueries(forbidden)

    def test_unknown_resource_or_field_cannot_become_sql(self):
        for resource, filters in [('facts', {}), ('cases;DROP TABLE cases', {}), ('cases', {'sort': 'received_at'}),
                                  ('health', {'extra': 'x'}), ('case', {}), ('case', {'id': 'not-a-uuid'}),
                                  ('cases', None), ('receipts', {'raw_payload': True})]:
            with self.subTest(resource=resource, filters=filters):
                with self.assertRaises(ReliabilityError) as caught:
                    self.queries.read(resource, filters)
                self.assertEqual(caught.exception.status, 422)

    def test_constructor_rejects_unbounded_health_timeout_without_connecting(self):
        for value in (True, 0, 86401, float('inf'), '120'):
            with self.subTest(value=value), self.assertRaises((ReliabilityError, ValueError)):
                self.module.ReliabilityQueries(lambda: None, health_stale_seconds=value)
        with self.assertRaises((ReliabilityError, ValueError)):
            self.module.ReliabilityQueries(None)

    def test_paging_rejects_boolean_infinite_or_overflowing_offsets(self):
        for filters in ({'page': True}, {'page': '9' * 5000}, {'page': 0}, {'page': float('inf')},
                        {'page_size': 101}, {'page_size': 0}, {'page_size': '01'}, {'page': 100001, 'page_size': 2}):
            with self.subTest(filters=str(filters)[:80]), self.assertRaises(ReliabilityError):
                self.queries.read('cases', filters)

    def test_time_window_needs_timezone_order_and_at_most_31_days(self):
        for filters in ({'from_time': '2026-09-01'}, {'to_time': 'not a time'},
                        {'from_time': '2026-09-25T00:00:00Z', 'to_time': '2026-09-24T00:00:00Z'},
                        {'from_time': '2026-01-01T00:00:00Z', 'to_time': '2026-09-24T00:00:00Z'},
                        {'from_time': 10}, {'to_time': '0001-01-01T00:00:00Z'}):
            with self.subTest(filters=filters), self.assertRaises(ReliabilityError):
                self.queries.read('receipts', filters)

    def test_identity_reason_and_state_are_explicit_values_not_patterns(self):
        for filters in ({'gateway_id': 'GW%'}, {'device_id': ['D']}, {'message_id': 'x\x00'},
                        {'reason_code': 'invented'}, {'state': 'deleted'}, {'status': 'stored'}):
            with self.subTest(filters=filters), self.assertRaises(ReliabilityError):
                self.queries.read('cases', filters)

    def test_unavailable_database_is_not_a_successful_empty_list(self):
        def unavailable():
            raise psycopg.OperationalError('password=private-test-diagnostic')
        with self.assertRaises(ReliabilityError) as caught:
            self.module.ReliabilityQueries(unavailable).read('cases', {})
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn('private-test-diagnostic', str(caught.exception))


class IncidentQueryTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        module = importlib.import_module('backend.ingest_reliability.queries')
        self.queries = module.ReliabilityQueries(lambda: self.connect('reader'))
        self.now = self.scalar('SELECT clock_timestamp()', role='reader')
        self.obs = replace(observation('INCIDENT'), received_at=self.now, retain=True)
        self.fact = self.repository().ingest(self.obs)
        self.case_id = self.scalar('SELECT case_id FROM ingest_reliability.cases WHERE fact_id=%s', (self.fact.fact_id,), role='operator')

    def read(self, resource='cases', **filters):
        return self.queries.read(resource, filters)

    def test_cases_are_metadata_only_with_string_counters_and_literal_filters(self):
        self.repository().ingest(replace(self.obs, dup=True))
        data = self.read(gateway_id='GW', device_id='D', message_id='INCIDENT', reason_code='retained_publish', state='open')
        self.assertEqual((data['total'], data['page'], data['page_size']), ('1', 1, 20))
        row = data['items'][0]
        self.assertEqual((row['id'], row['fact_id'], row['duplicate_count'], row['version']),
                         (str(self.case_id), str(self.fact.fact_id), '1', '1'))
        self.assertEqual(row['receipt_id'], str(self.fact.receipt_id))
        self.assertNotIn('raw_payload', row)
        self.assertNotIn('payload', row)
        self.assertEqual(self.read(gateway_id='OTHER')['total'], '0')
        json.dumps(data)

    def test_single_case_outside_default_window_remains_retrievable(self):
        old = self.repository().ingest(replace(observation('OLD'), retain=True, received_at=self.now - timedelta(days=70)))
        old_case = self.scalar('SELECT case_id FROM ingest_reliability.cases WHERE fact_id=%s', (old.fact_id,), role='operator')
        self.assertEqual(self.read()['total'], '1')
        detail = self.read('case', id=str(old_case))
        self.assertEqual(detail['entity']['message_id'], 'OLD')
        self.assertNotIn('audit', detail)
        with self.assertRaises(ReliabilityError) as caught:
            self.read('case', id=str(uuid4()))
        self.assertEqual(caught.exception.status, 404)

    def test_timezones_pagination_and_fixed_tie_order_are_consistent(self):
        for number in range(4):
            self.repository().ingest(replace(observation('P-' + str(number)), retain=True, received_at=self.now))
        start = (self.now - timedelta(hours=1)).astimezone(timezone(timedelta(hours=8))).isoformat()
        end = (self.now + timedelta(hours=1)).astimezone(timezone(timedelta(hours=8))).isoformat()
        expected = sorted([str(row[0]) for row in self.connect('reader').execute('SELECT id FROM ingest_reliability.case_metadata').fetchall()], reverse=True)
        one = self.read(from_time=start, to_time=end, page_size=2)
        two = self.read(from_time=start, to_time=end, page_size=2, page=2)
        self.assertEqual([row['id'] for row in one['items'] + two['items']], expected[:4])
        self.assertEqual(one['total'], '5')
        self.assertEqual(self.read(from_time=(self.now + timedelta(seconds=1)).isoformat(), to_time=end)['total'], '0')

    def test_receipts_show_broker_delivery_not_unproven_gateway_confirmation(self):
        pending = self.read('receipt', id=str(self.fact.receipt_id))['entity']
        self.assertEqual((pending['delivery_state'], pending['status']), ('pending', 'quarantined'))
        lease = self.outbox().claim()[0]
        inflight = self.read('receipt', id=str(self.fact.receipt_id))['entity']
        self.assertEqual(inflight['delivery_state'], 'inflight')
        self.outbox().finish(lease, success=True)
        done = self.read('receipts', status='quarantined')['items'][0]
        self.assertEqual(done['delivery_state'], 'broker_confirmed')
        self.assertEqual(done['requested_generation'], '1')
        self.assertNotIn('gateway_confirmed', done)
        self.assertNotIn('payload', done)

    def close_case(self):
        contracts = importlib.import_module('backend.ingest_reliability.management_contracts')
        actor = contracts.Actor('platform', '17', 'operator', '现场运维', 'maintenance')
        command = contracts.validate_management('case.close', self.case_id, actor,
                         {'idempotency_key': str(uuid4()), 'expected_version': '1', 'reason': '已检查报文格式'},
                         route=f'/api/ingest-reliability/cases/{self.case_id}/close')
        self.operations().execute(command)

    def test_audit_history_contains_actor_and_changes_but_never_raw_bytes(self):
        self.close_case()
        history = self.read('audit', fact_id=str(self.fact.fact_id))
        self.assertEqual(history['total'], '1')
        row = history['items'][0]
        self.assertEqual((row['action'], row['actor']['user_id'], row['after_data']['state']), ('case.close', '17', 'closed'))
        self.assertNotIn('raw_payload', json.dumps(row))

    def test_health_requires_fresh_error_free_heartbeat_and_available_capacity(self):
        missing = self.read('health')
        self.assertFalse(missing['healthy'])
        self.assertIn('publisher.missing', missing['reasons'])
        outbox = self.outbox()
        outbox.heartbeat(success=True)
        self.assertTrue(self.read('health')['healthy'])
        outbox.heartbeat(success=False, error_code='mqtt.publish_failed')
        failed = self.read('health')
        self.assertFalse(failed['healthy'])
        self.assertIn('publisher.failed', failed['reasons'])
        outbox.heartbeat(success=True)
        self.assertTrue(self.read('health')['healthy'])
        with self.connect('migrator') as conn:
            conn.execute('UPDATE ingest_reliability.capacity SET used_total_bytes=total_bytes WHERE singleton=1')
        full = self.read('health')
        self.assertFalse(full['healthy'])
        self.assertIn('capacity.exhausted', full['reasons'])

    def test_stale_future_or_clock_warning_heartbeat_is_not_green(self):
        outbox = self.outbox()
        outbox.heartbeat(success=True, now=self.now - timedelta(minutes=10))
        self.assertIn('publisher.stale', self.read('health')['reasons'])
        outbox.heartbeat(success=True, now=self.now + timedelta(hours=1))
        self.assertIn('publisher.clock_invalid', self.read('health')['reasons'])
        outbox.heartbeat(success=False, error_code='ingest.clock_rollback')
        outbox.heartbeat(success=True)
        data = self.read('health')
        self.assertFalse(data['healthy'])
        self.assertIn('publisher.failed', data['reasons'])

    def test_fresh_healthy_worker_does_not_mask_another_active_failed_worker(self):
        self.outbox().heartbeat(success=False, error_code='mqtt.publish_failed')
        self.outbox().heartbeat(success=True)
        self.assertFalse(self.read('health')['healthy'])

    def test_liveness_without_recent_success_does_not_claim_a_working_publisher(self):
        outbox = self.outbox()
        outbox.heartbeat()
        self.assertFalse(self.read('health')['healthy'])
        self.assertIn('publisher.unverified', self.read('health')['reasons'])
        outbox.heartbeat(success=True, now=self.now - timedelta(minutes=10))
        outbox.heartbeat()
        self.assertFalse(self.read('health')['healthy'])
        self.assertIn('publisher.unverified', self.read('health')['reasons'])

    def test_future_capacity_clock_is_not_a_healthy_empty_queue(self):
        self.outbox().heartbeat(success=True)
        with self.connect('migrator') as conn:
            conn.execute('UPDATE ingest_reliability.capacity SET last_clock_seen=%s',
                         (self.now + timedelta(hours=1),))
        self.assertFalse(self.read('health')['healthy'])
        self.assertIn('capacity.clock_invalid', self.read('health')['reasons'])

    def test_missing_capacity_clock_is_reported_without_a_false_healthy_state(self):
        self.outbox().heartbeat(success=True)
        with self.connect('migrator') as conn:
            conn.execute('UPDATE ingest_reliability.capacity SET last_clock_seen=NULL')
        result = self.read('health')
        self.assertFalse(result['healthy'])
        self.assertIn('capacity.clock_missing', result['reasons'])

    def test_health_counts_recent_new_isolations_not_repeated_deliveries(self):
        self.repository().ingest(replace(observation('OLD'), retain=True, received_at=self.now - timedelta(days=2)))
        self.repository().ingest(replace(self.obs, dup=True))
        self.close_case()
        result = self.read('health')
        self.assertEqual(result.get('quarantined_cases'), '2')
        self.assertEqual(result.get('new_cases_24h'), '1')
        self.assertEqual(result['open_cases'], '1')

    def test_health_exposes_controlled_latest_active_failure_reason(self):
        self.outbox().heartbeat(success=False, error_code='mqtt.publish_failed')
        result = self.read('health')
        self.assertEqual(result.get('publisher_error_code'), 'mqtt.publish_failed')

    def test_health_has_open_and_pending_counts_but_no_remote_claim_or_credentials(self):
        data = self.read('health')
        self.assertEqual(data['open_cases'], '1')
        self.assertEqual(data['pending_receipts'], '1')
        self.assertGreaterEqual(data['oldest_open_age_seconds'], 0)
        self.assertNotIn('gateway_confirmed', data)
        for forbidden in ('password', 'host', 'dsn', 'raw_payload'):
            self.assertNotIn(forbidden, data)

    def test_reader_queries_cannot_access_rows_through_ungranted_raw_path(self):
        with self.connect('reader') as conn, self.assertRaises(psycopg.errors.InsufficientPrivilege):
            conn.execute('SELECT raw_payload FROM ingest_reliability.facts')

    def test_audit_actor_filter_remains_a_literal_not_a_sql_fragment(self):
        self.close_case()
        self.assertEqual(self.read('audit', actor_id="17' OR TRUE --")['total'], '0')
        self.assertEqual(self.read('audit', actor_id='17')['total'], '1')

    def test_byte_counts_and_big_versions_survive_json_without_float_rounding(self):
        with self.connect('migrator') as conn:
            conn.execute('UPDATE ingest_reliability.cases SET version=9007199254740993 WHERE case_id=%s', (self.case_id,))
            conn.execute('UPDATE ingest_reliability.fact_observations SET duplicate_count=9007199254740993 WHERE fact_id=%s', (self.fact.fact_id,))
        row = self.read('case', id=str(self.case_id))['entity']
        decoded = json.loads(json.dumps(row))
        self.assertEqual(decoded['version'], '9007199254740993')
        self.assertEqual(decoded['duplicate_count'], '9007199254740993')
        self.assertEqual(decoded['payload_bytes'], str(len(self.obs.payload)))
