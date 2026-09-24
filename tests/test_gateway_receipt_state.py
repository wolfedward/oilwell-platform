"""Durable application confirmation, stale callbacks and bounded bad receipts."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.gateway.reliable_spool import ReliableSpool, SpoolLimits, MAX_INTEGER
from backend.gateway.spool_migrate import initialize_spool
from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_vectors import observation, receipt_for


class FaultConnection(sqlite3.Connection):
    """Run real SQLite operations; fail one selected storage boundary only."""
    fault = None
    def execute(self, sql, parameters=()):
        if self.fault and self.fault not in ('commit', 'committed') and self.fault in sql:
            self.fault = None
            raise sqlite3.OperationalError('injected storage write error')
        return super().execute(sql, parameters)

    def commit(self):
        fault, self.fault = self.fault, None
        if fault == 'commit':
            raise sqlite3.OperationalError('injected commit error')
        result = super().commit()
        if fault == 'committed':
            raise sqlite3.OperationalError('injected lost commit response')
        return result


class ReceiptTransitionTests(unittest.TestCase):
    def setUp(self):
        self.transition = importlib.import_module('backend.gateway.receipt_state').transition

    def test_transport_confirmation_differs_from_application_confirmation(self):
        cases = [('pending', 'application', 'claim', 'inflight'),
                 ('inflight', 'application', 'broker_ack', 'awaiting_receipt'),
                 ('awaiting_receipt', 'application', 'claim', 'inflight'),
                 ('inflight', 'broker', 'broker_ack', 'broker_confirmed'),
                 ('inflight', 'application', 'fail', 'pending'),
                 ('inflight', 'broker', 'exhausted', 'dead'),
                 ('inflight', 'application', 'exhausted', 'pending')]
        for current, mode, event, expected in cases:
            with self.subTest(current=current, mode=mode, event=event):
                self.assertEqual(self.transition(current, mode, event), expected)

    def test_final_states_never_regress_on_late_events(self):
        for mode, states in [('application', ('stored', 'quarantined')),
                             ('broker', ('broker_confirmed', 'legacy_broker_confirmed', 'dead'))]:
            for current in states:
                for event in ('claim', 'broker_ack', 'fail', 'exhausted', 'stored', 'quarantined'):
                    with self.subTest(mode=mode, current=current, event=event):
                        self.assertEqual(self.transition(current, mode, event), current)

    def test_early_or_late_receipt_finalizes_only_application_rows(self):
        for current in ('pending', 'inflight', 'awaiting_receipt'):
            for outcome in ('stored', 'quarantined'):
                self.assertEqual(self.transition(current, 'application', outcome), outcome)
        for current in ('pending', 'inflight'):
            self.assertEqual(self.transition(current, 'broker', 'stored'), current)

    def test_unknown_or_mode_inconsistent_states_are_errors_not_success(self):
        for args in [('unknown', 'application', 'claim'), ('dead', 'application', 'claim'),
                     ('awaiting_receipt', 'broker', 'claim'), ('pending', 'auto', 'claim'),
                     ('pending', 'application', 'invent_success'), (None, 'broker', 'claim')]:
            with self.subTest(args=args), self.assertRaises(ReliabilityError):
                self.transition(*args)


class ReceiptStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='receipt-state-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'queue.db'
        self.limits = SpoolLimits(5000, 16 * 1024 * 1024, 1024 * 1024)
        initialize_spool(self.path, limits=self.limits)
        self.spool = self.open()

    def open(self, **kwargs):
        spool = ReliableSpool(self.path, limits=self.limits, clock=lambda: 100, **kwargs)
        self.addCleanup(spool.close)
        return spool

    def enqueue(self, identifier='M-1', *, mode='application', now=100):
        obs = observation(identifier)
        self.assertTrue(self.spool.enqueue(identifier, obs.topic, obs.payload, confirmation_mode=mode, now=now))
        return obs

    def receipt(self, obs, *, status='stored', reason='accepted'):
        return replace(receipt_for(obs, status=status, reason_code=reason), receipt_id=str(uuid4()))

    def scalar(self, statement, parameters=()):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(statement, parameters).fetchone()[0]

    def use_fault_connection(self):
        self.spool.close()
        connect = sqlite3.connect
        with patch('backend.gateway.reliable_spool.sqlite3.connect',
                   side_effect=lambda *args, **kwargs: connect(*args, factory=FaultConnection, **kwargs)):
            self.spool = self.open()

    def test_early_receipt_wins_over_late_transport_failure_and_puback(self):
        obs = self.enqueue()
        lease = self.spool.claim(now=100)[0]
        receipt = self.receipt(obs)
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101), 'applied')
        self.assertFalse(self.spool.fail(lease, 'transport.timeout', now=102))
        self.assertFalse(self.spool.broker_ack(lease, now=102))
        self.assertEqual(self.spool.get('M-1')['state'], 'stored')
        self.assertEqual(self.spool.counts()['stored'], 1)

    def test_broker_ack_only_waits_for_application_receipt_and_preserves_bytes(self):
        obs = self.enqueue()
        lease = self.spool.claim(now=100)[0]
        self.assertTrue(self.spool.broker_ack(lease, now=101))
        row = self.spool.get('M-1')
        self.assertEqual(row['state'], 'awaiting_receipt')
        self.assertEqual(row['payload'], obs.payload)
        self.assertIsNone(row['receipt_id'])
        self.assertEqual(self.spool.counts()['stored'], 0)
        self.assertEqual(self.spool.claim(now=102), [])

    def test_quarantined_is_a_durable_rejection_not_stored(self):
        obs = self.enqueue()
        receipt = self.receipt(obs, status='quarantined', reason='idempotency_conflict')
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101), 'applied')
        row = self.spool.get('M-1')
        self.assertEqual(row['state'], 'quarantined')
        self.assertEqual(row['last_error_code'], 'idempotency_conflict')
        self.assertEqual(self.spool.counts()['stored'], 0)
        self.assertEqual(self.spool.claim(now=10000), [])
        self.assertEqual(row['payload'], obs.payload)

    def test_valid_receipt_after_restart_and_lease_expiry_finishes_original_message(self):
        obs = self.enqueue()
        old_lease = self.spool.claim(now=100, lease_seconds=5)[0]
        self.spool.close()
        self.spool = self.open()
        receipt = self.receipt(obs)
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=1000), 'applied')
        self.assertFalse(self.spool.fail(old_lease, 'transport.timeout', now=1001))
        self.assertEqual(self.spool.get('M-1')['state'], 'stored')

    def test_lease_reclaim_fences_old_success_failure_and_forged_tokens(self):
        self.enqueue()
        old = self.spool.claim(now=100, lease_seconds=5)[0]
        current = self.spool.claim(now=106)[0]
        self.assertNotEqual(old.token, current.token)
        for stale in (old, replace(current, token=str(uuid4()))):
            self.assertFalse(self.spool.broker_ack(stale, now=107))
            self.assertFalse(self.spool.fail(stale, 'transport.timeout', now=107))
        self.assertTrue(self.spool.broker_ack(current, now=107))
        self.assertEqual(self.spool.get('M-1')['state'], 'awaiting_receipt')

    def test_expired_unreclaimed_lease_cannot_advance_state(self):
        self.enqueue()
        lease = self.spool.claim(now=100, lease_seconds=5)[0]
        self.assertFalse(self.spool.broker_ack(lease, now=105))
        self.assertFalse(self.spool.fail(lease, 'transport.timeout', now=105))
        self.assertEqual(self.spool.get('M-1')['state'], 'inflight')

    def test_duplicate_receipt_keeps_exact_first_bytes_and_one_result(self):
        obs = self.enqueue()
        receipt = self.receipt(obs)
        body = receipt.to_bytes()
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), body, now=101), 'applied')
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), body, now=102), 'duplicate')
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_receipts'), 1)
        self.assertEqual(self.scalar('SELECT payload FROM spool_receipts'), body)
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_security_events'), 0)

    def test_contradictory_receipt_is_audited_without_overwriting_final_result(self):
        obs = self.enqueue()
        receipt = self.receipt(obs)
        self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101)
        contradiction = self.receipt(obs, status='quarantined', reason='contract_invalid')
        self.assertEqual(self.spool.apply_receipt(contradiction.topic('oilfield'), contradiction.to_bytes(), now=102), 'ignored')
        self.assertEqual(self.spool.get('M-1')['state'], 'stored')
        self.assertEqual(self.scalar('SELECT payload FROM spool_receipts'), receipt.to_bytes())
        self.assertEqual(self.scalar('SELECT sum(occurrences) FROM spool_security_events'), 1)

    def test_receipt_id_reuse_cannot_complete_another_message(self):
        first, second = self.enqueue('M-1'), self.enqueue('M-2')
        receipt = self.receipt(first)
        self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101)
        forged = replace(self.receipt(second), receipt_id=receipt.receipt_id)
        self.assertEqual(self.spool.apply_receipt(forged.topic('oilfield'), forged.to_bytes(), now=102), 'ignored')
        self.assertEqual(self.spool.get('M-2')['state'], 'pending')
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_receipts'), 1)

    def test_wrong_identity_digest_topic_or_unknown_id_never_completes_queued_message(self):
        obs = self.enqueue()
        good = self.receipt(obs)
        wrong = [replace(good, gateway_id='OTHER'), replace(good, device_id='OTHER'),
                 replace(good, message_id='UNKNOWN'), replace(good, payload_sha256='0' * 64)]
        for receipt in wrong:
            self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101), 'ignored')
        self.assertEqual(self.spool.apply_receipt(good.topic('different-prefix'), good.to_bytes(), now=101), 'ignored')
        self.assertEqual(self.spool.get('M-1')['state'], 'pending')
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_receipts'), 0)
        self.assertEqual(self.scalar('SELECT sum(occurrences) FROM spool_security_events'), 5)

    def test_broker_rows_ignore_application_receipts_without_upgrading_confirmation(self):
        obs = self.enqueue(mode='broker')
        receipt = self.receipt(obs)
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101), 'ignored')
        lease = self.spool.claim(now=101)[0]
        self.assertTrue(self.spool.broker_ack(lease, now=102))
        self.assertEqual(self.spool.get('M-1')['state'], 'broker_confirmed')
        self.assertEqual(self.spool.counts()['stored'], 0)

    def test_application_failure_after_twelve_attempts_remains_retryable(self):
        self.enqueue()
        for attempt in range(20):
            now = 100 + attempt * 1000
            lease = self.spool.claim(now=now)[0]
            self.assertTrue(self.spool.fail(lease, 'transport.offline', now=now + 1))
            self.assertEqual(self.spool.get('M-1')['state'], 'pending')
        self.assertTrue(self.spool.claim(now=30000))
        self.assertEqual(self.spool.counts()['dead'], 0)

    def test_receipt_timeout_republishes_same_application_identity_and_exact_bytes(self):
        obs = self.enqueue()
        first = self.spool.claim(now=100)[0]
        self.spool.broker_ack(first, now=101)
        self.assertEqual(self.spool.claim(now=160), [])
        retry = self.spool.claim(now=161)[0]
        self.assertEqual((retry.message_id, retry.topic, retry.payload), ('M-1', obs.topic, obs.payload))
        self.assertNotEqual(retry.token, first.token)

    def test_retained_and_malformed_receipts_are_durable_events_not_results(self):
        obs = self.enqueue()
        receipt = self.receipt(obs)
        topic = receipt.topic('oilfield')
        for body, retained in [(receipt.to_bytes(), True), (b'not JSON', False), (b'[]', False),
                               (b'\xff', False), (b'{"schema":"x","schema":"y"}', False),
                               (b'x' * 4097, False)]:
            self.assertEqual(self.spool.apply_receipt(topic, body, retain=retained, now=101), 'ignored')
        self.assertEqual(self.spool.get('M-1')['state'], 'pending')
        self.assertEqual(self.scalar('SELECT sum(occurrences) FROM spool_security_events'), 6)

    def test_exact_4096_byte_receipt_is_accepted_but_not_4097(self):
        obs = self.enqueue()
        receipt = self.receipt(obs)
        body = receipt.to_bytes()
        padded = body + b' ' * (4096 - len(body))
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), padded + b' ', now=101), 'ignored')
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), padded, now=102), 'applied')
        self.assertEqual(self.scalar('SELECT payload FROM spool_receipts'), padded)

    def test_repeated_invalid_event_is_merged_and_counter_saturates(self):
        self.assertEqual(self.spool.apply_receipt('bad/topic', b'bad', now=100), 'ignored')
        self.assertEqual(self.spool.apply_receipt('bad/topic', b'bad', now=101), 'ignored')
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_security_events'), 1)
        self.assertEqual(self.scalar('SELECT occurrences FROM spool_security_events'), 2)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE spool_security_events SET occurrences=?', (MAX_INTEGER,))
        self.assertEqual(self.spool.apply_receipt('bad/topic', b'bad', now=102), 'ignored')
        self.assertEqual(self.scalar('SELECT occurrences FROM spool_security_events'), MAX_INTEGER)

    def test_clock_rollback_reanchors_deadlines_once_instead_of_sliding_forever(self):
        self.enqueue(now=1000)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE uplink_queue_v2 SET next_attempt_at=5000')
            connection.execute('UPDATE spool_capacity SET last_clock_seen=1000')
        self.assertEqual(self.spool.claim(now=100), [])
        self.assertEqual(self.spool.get('M-1')['next_attempt_at'], 400)
        self.assertEqual(self.spool.claim(now=399), [])
        self.assertTrue(self.spool.claim(now=400))
        self.assertEqual(self.scalar('SELECT last_clock_warning FROM spool_capacity'), 100)

    def test_attempts_saturate_and_failure_uses_stored_attempt_count_not_callback_copy(self):
        self.enqueue()
        first = self.spool.claim(now=100)[0]
        self.assertTrue(self.spool.fail(replace(first, attempts=MAX_INTEGER), 'transport.offline', now=101))
        self.assertLessEqual(self.spool.get('M-1')['next_attempt_at'], 103)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE uplink_queue_v2 SET attempts=?', (MAX_INTEGER,))
        last = self.spool.claim(now=1000)[0]
        self.assertEqual(last.attempts, MAX_INTEGER)
        self.assertTrue(self.spool.fail(last, 'transport.offline', now=1001))
        self.assertEqual(self.spool.get('M-1')['state'], 'pending')

    def test_receipt_and_final_state_roll_back_together_at_every_write_boundary(self):
        self.use_fault_connection()
        obs = self.enqueue()
        receipt = self.receipt(obs)
        for fault in ('INSERT INTO spool_receipts', 'UPDATE uplink_queue_v2 SET state', 'commit'):
            self.spool._connection.fault = fault
            with self.subTest(fault=fault), self.assertRaises(ReliabilityError):
                self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101)
            self.assertEqual(self.spool.get('M-1')['state'], 'pending')
            self.assertEqual(self.scalar('SELECT count(*) FROM spool_receipts'), 0)
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=102), 'applied')

    def test_lost_receipt_commit_response_is_not_reported_as_success_and_retry_is_idempotent(self):
        self.use_fault_connection()
        obs = self.enqueue()
        receipt = self.receipt(obs)
        self.spool._connection.fault = 'committed'
        with self.assertRaises(ReliabilityError):
            self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101)
        self.spool.close()
        self.spool = self.open()
        self.assertEqual(self.spool.get('M-1')['state'], 'stored')
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=102), 'duplicate')
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_receipts'), 1)

    def test_bad_receipt_cannot_return_ignored_before_event_commit(self):
        self.use_fault_connection()
        for fault in ('INSERT INTO spool_security_events', 'commit'):
            self.spool._connection.fault = fault
            with self.subTest(fault=fault), self.assertRaises(ReliabilityError):
                self.spool.apply_receipt('bad/topic', b'bad', now=101)
            self.assertEqual(self.scalar('SELECT count(*) FROM spool_security_events'), 0)
        self.assertEqual(self.spool.apply_receipt('bad/topic', b'bad', now=102), 'ignored')

    def test_security_event_cardinality_is_bounded_and_overflow_is_durable(self):
        for index in range(1025):
            self.assertEqual(self.spool.apply_receipt('bad/topic', str(index).encode(), now=101), 'ignored')
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_security_events'), 1024)
        self.assertEqual(self.scalar('SELECT security_overflow_count FROM spool_capacity'), 1)
        self.spool.close()
        self.spool = self.open()
        self.spool.apply_receipt('bad/topic', b'0', now=102)
        self.assertEqual(self.scalar('SELECT sum(occurrences) FROM spool_security_events'), 1025)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE spool_capacity SET security_overflow_count=?', (MAX_INTEGER,))
        self.spool.apply_receipt('bad/topic', b'another-key', now=103)
        self.assertEqual(self.scalar('SELECT security_overflow_count FROM spool_capacity'), MAX_INTEGER)
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_security_events'), 1024)

    def test_overflow_write_failure_never_returns_a_false_durable_ignore(self):
        self.use_fault_connection()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany('INSERT INTO spool_security_events VALUES(?,1,100,100,?,?)',
                                   [('receipt.invalid', str(index), str(index)) for index in range(1024)])
        self.spool._connection.fault = 'security_overflow_count='
        with self.assertRaises(ReliabilityError):
            self.spool.apply_receipt('bad/topic', b'overflow', now=101)
        self.assertEqual(self.scalar('SELECT security_overflow_count FROM spool_capacity'), 0)
        self.assertEqual(self.spool.apply_receipt('bad/topic', b'overflow', now=102), 'ignored')
        self.assertEqual(self.scalar('SELECT security_overflow_count FROM spool_capacity'), 1)

    def test_claim_batch_is_atomic_if_commit_fails(self):
        self.use_fault_connection()
        self.enqueue('M-1')
        self.enqueue('M-2')
        self.spool._connection.fault = 'commit'
        with self.assertRaises(ReliabilityError):
            self.spool.claim(now=100, limit=2)
        self.assertEqual(self.spool.counts()['pending'], 2)
        self.assertEqual(self.scalar('SELECT sum(attempts) FROM uplink_queue_v2'), 0)
        self.assertEqual(len(self.spool.claim(now=101, limit=2)), 2)

    def test_transport_status_is_not_advanced_on_commit_failure(self):
        self.use_fault_connection()
        self.enqueue()
        lease = self.spool.claim(now=100)[0]
        for operation in (lambda: self.spool.broker_ack(lease, now=101),
                          lambda: self.spool.fail(lease, 'transport.offline', now=101)):
            self.spool._connection.fault = 'commit'
            with self.assertRaises(ReliabilityError):
                operation()
            self.assertEqual(self.spool.get('M-1')['state'], 'inflight')
        self.assertTrue(self.spool.broker_ack(lease, now=102))

    def test_concurrent_claimers_receive_disjoint_leases_and_receipts_finalize_once(self):
        for index in range(12):
            self.enqueue(f'M-{index}')
        with ThreadPoolExecutor(max_workers=4) as pool:
            batches = list(pool.map(lambda _: self.spool.claim(now=100, limit=3), range(4)))
        self.assertEqual(len({lease.message_id for batch in batches for lease in batch}), 12)
        receipt = self.receipt(observation('M-0'))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101), range(4)))
        self.assertEqual(sorted(results), ['applied', 'duplicate', 'duplicate', 'duplicate'])
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_receipts'), 1)

    def test_broker_only_failures_keep_legacy_twelve_attempt_limit(self):
        self.enqueue(mode='broker')
        for attempt in range(12):
            now = 100 + attempt * 1000
            lease = self.spool.claim(now=now)[0]
            self.assertTrue(self.spool.fail(lease, 'transport.offline', now=now + 1))
        self.assertEqual(self.spool.get('M-1')['state'], 'dead')
        self.assertEqual(self.spool.claim(now=20000), [])

    def test_clock_reanchor_survives_reopen_and_expires_old_lease_at_bounded_deadline(self):
        self.enqueue(now=1000)
        old = self.spool.claim(now=1000, lease_seconds=300)[0]
        self.assertEqual(self.spool.claim(now=100), [])
        self.assertEqual(self.spool.get('M-1')['lease_until'], 400)
        self.spool.close()
        self.spool = self.open()
        self.assertEqual(self.spool.claim(now=399), [])
        new = self.spool.claim(now=400)[0]
        self.assertNotEqual(new.token, old.token)
        self.assertFalse(self.spool.broker_ack(old, now=401))

    def test_clock_warning_failure_rolls_back_reanchor_instead_of_silencing_it(self):
        self.use_fault_connection()
        self.enqueue(now=1000)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE spool_capacity SET last_clock_seen=1000')
            connection.execute('UPDATE uplink_queue_v2 SET next_attempt_at=5000')
        self.spool._connection.fault = 'last_clock_warning='
        with self.assertRaises(ReliabilityError):
            self.spool.claim(now=100)
        self.assertEqual(self.spool.get('M-1')['next_attempt_at'], 5000)
        self.assertEqual(self.scalar('SELECT last_clock_seen FROM spool_capacity'), 1000)
        self.assertEqual(self.spool.claim(now=100), [])
        self.assertEqual(self.spool.get('M-1')['next_attempt_at'], 400)

    def test_final_receipt_does_not_delete_original_or_release_its_retained_budget(self):
        obs = self.enqueue()
        receipt = self.receipt(obs)
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101), 'applied')
        self.assertEqual(self.spool.get('M-1')['payload'], obs.payload)
        self.assertEqual(self.scalar('SELECT used_messages FROM spool_capacity'), 1)
        self.assertEqual(self.scalar('SELECT used_payload_bytes FROM spool_capacity'), len(obs.payload))

    def test_semantically_equal_receipt_keeps_first_exact_bytes(self):
        obs = self.enqueue()
        receipt = self.receipt(obs)
        body = receipt.to_bytes()
        self.spool.apply_receipt(receipt.topic('oilfield'), body, now=101)
        alternative = json.dumps(json.loads(body), indent=2).encode()
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), alternative, now=102), 'duplicate')
        self.assertEqual(self.scalar('SELECT payload FROM spool_receipts'), body)
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_security_events'), 0)

    def test_changed_receipt_id_or_time_is_conflict_even_when_status_is_same(self):
        obs = self.enqueue()
        original = self.receipt(obs)
        self.assertEqual(self.spool.apply_receipt(original.topic('oilfield'), original.to_bytes(), now=101), 'applied')
        for modified in (replace(original, receipt_id=str(uuid4())),
                         replace(original, recorded_at='2026-09-24T01:00:00.000000Z')):
            self.assertEqual(self.spool.apply_receipt(modified.topic('oilfield'), modified.to_bytes(), now=102), 'ignored')
        self.assertEqual(self.scalar('SELECT payload FROM spool_receipts'), original.to_bytes())
        self.assertEqual(self.scalar('SELECT sum(occurrences) FROM spool_security_events'), 2)

    def test_configurable_receipt_wait_and_failure_backoff_are_applied(self):
        self.spool.close()
        self.spool = self.open(receipt_timeout_seconds=20, base_backoff_seconds=4, max_backoff_seconds=10)
        self.enqueue()
        lease = self.spool.claim(now=100)[0]
        self.assertTrue(self.spool.broker_ack(lease, now=101))
        self.assertEqual(self.spool.claim(now=120), [])
        lease = self.spool.claim(now=121)[0]
        self.spool.fail(lease, 'transport.offline', now=122)
        self.assertLessEqual(self.spool.get('M-1')['next_attempt_at'], 130)
        self.assertGreater(self.spool.get('M-1')['next_attempt_at'], 122)

    def test_lease_api_rejects_invalid_bounds_without_mutating_queue(self):
        self.enqueue()
        for kwargs in ({'limit': True}, {'limit': 0}, {'limit': 201}, {'lease_seconds': 0},
                       {'lease_seconds': 301}, {'lease_seconds': float('nan')}, {'now': float('inf')},
                       {'now': 10**10000}, {'now': 253402300799}):
            with self.subTest(keys=list(kwargs)), self.assertRaises(ReliabilityError):
                self.spool.claim(**kwargs)
        self.assertEqual(self.spool.get('M-1')['state'], 'pending')
        lease = self.spool.claim(now=100)[0]
        for value in (None, {}, replace(lease, token='not-a-uuid')):
            with self.subTest(value=type(value)), self.assertRaises(ReliabilityError):
                self.spool.broker_ack(value, now=101)
        for code in ('', 'unsafe\nlog', 'x' * 97, None):
            with self.subTest(code=code), self.assertRaises(ReliabilityError):
                self.spool.fail(lease, code, now=101)
        for args in [(None, b'bad', False), ('topic', 'not-bytes', False), ('topic', b'bad', 1)]:
            with self.subTest(args=args), self.assertRaises(ReliabilityError):
                self.spool.apply_receipt(args[0], args[1], retain=args[2], now=101)
        self.assertEqual(self.spool.get('M-1')['state'], 'inflight')

    def test_configuration_limits_fail_before_opening_a_queue(self):
        missing = self.path.parent / 'must-not-be-created.db'
        for options in ({'receipt_timeout_seconds': True}, {'receipt_timeout_seconds': 0},
                        {'receipt_timeout_seconds': 301}, {'base_backoff_seconds': 0},
                        {'max_backoff_seconds': 301}, {'base_backoff_seconds': 20, 'max_backoff_seconds': 10},
                        {'max_backoff_seconds': float('inf')}, {'base_backoff_seconds': 10**10000}):
            with self.subTest(keys=list(options)):
                with self.assertRaises(ReliabilityError) as caught:
                    ReliableSpool(missing, limits=self.limits, **options)
                self.assertEqual(caught.exception.code, 'ingest.spool_config_invalid')
        self.assertFalse(missing.exists())
        self.assertFalse(Path(str(missing) + '.owner').exists())

    def test_enqueue_advances_clock_anchor_so_reboot_can_bound_offline_wait(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE spool_capacity SET last_clock_seen=50')
        self.enqueue(now=1000)
        self.assertEqual(self.scalar('SELECT last_clock_seen FROM spool_capacity'), 1000)
        self.spool.close()
        self.spool = self.open()
        # This row was already due on the previous timeline: preserve that
        # fact instead of imposing another artificial wait after restart.
        self.assertEqual([item.message_id for item in self.spool.claim(now=100)], ['M-1'])
        self.assertEqual(self.scalar('SELECT last_clock_warning FROM spool_capacity'), 100)

    def test_callback_uses_persisted_mode_not_forged_lease_copy(self):
        self.enqueue()
        lease = self.spool.claim(now=100)[0]
        self.assertTrue(self.spool.broker_ack(replace(lease, confirmation_mode='broker'), now=101))
        self.assertEqual(self.spool.get('M-1')['state'], 'awaiting_receipt')

    def test_claim_selects_oldest_due_rows_and_respects_limit(self):
        self.enqueue('M-3', now=102)
        self.enqueue('M-1', now=100)
        self.enqueue('M-2', now=101)
        leases = self.spool.claim(now=101, limit=2)
        self.assertEqual([item.message_id for item in leases], ['M-1', 'M-2'])
        self.assertEqual(self.spool.get('M-3')['state'], 'pending')
        self.assertEqual([item.message_id for item in self.spool.claim(now=102, limit=2)], ['M-3'])

    def test_invalid_system_clock_does_not_turn_valid_receipt_into_security_event(self):
        obs = self.enqueue()
        receipt = self.receipt(obs)
        for now in (float('nan'), True, 10**10000):
            with self.subTest(type=type(now)), self.assertRaises(ReliabilityError):
                self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=now)
        self.assertEqual(self.spool.get('M-1')['state'], 'pending')
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_security_events'), 0)

    def test_receipt_uuid_case_cannot_bypass_cross_message_reuse_guard(self):
        first, second = self.enqueue('M-1'), self.enqueue('M-2')
        identifier = 'abcdefab-abcd-4abc-8abc-abcdefabcdef'
        receipt = replace(self.receipt(first), receipt_id=identifier)
        self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101)
        reused = replace(self.receipt(second), receipt_id=identifier.upper())
        self.assertEqual(self.spool.apply_receipt(reused.topic('oilfield'), reused.to_bytes(), now=102), 'ignored')
        self.assertEqual(self.spool.get('M-2')['state'], 'pending')
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_receipts'), 1)

    def test_correlated_contract_rejection_can_finalize_invalid_telemetry(self):
        obs = observation('M-1')
        invalid = replace(obs, payload=b'{"schema":"oilwell.telemetry.v1","gateway_id":"GW","device_id":"D","message_id":"M-1"}')
        self.spool.enqueue('M-1', invalid.topic, invalid.payload, confirmation_mode='application', now=100)
        receipt = self.receipt(invalid, status='quarantined', reason='contract_invalid')
        self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101), 'applied')
        self.assertEqual(self.spool.get('M-1')['state'], 'quarantined')
        self.assertEqual(self.spool.get('M-1')['payload'], invalid.payload)

    def test_old_final_broker_rows_are_not_upgraded_or_reclaimed(self):
        for index, state in enumerate(('broker_confirmed', 'legacy_broker_confirmed', 'dead')):
            obs = self.enqueue(f'M-{index}', mode='broker')
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute('UPDATE uplink_queue_v2 SET state=? WHERE message_id=?', (state, f'M-{index}'))
            receipt = self.receipt(obs)
            self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101), 'ignored')
            self.assertEqual(self.spool.get(f'M-{index}')['state'], state)
        self.assertEqual(self.spool.claim(now=1000), [])
        self.assertEqual(self.scalar('SELECT count(*) FROM spool_receipts'), 0)

    def test_clock_rollback_cannot_resurrect_an_already_expired_lease(self):
        self.enqueue(now=1000)
        expired = self.spool.claim(now=1000, lease_seconds=5)[0]
        self.assertFalse(self.spool.broker_ack(expired, now=1010))
        self.assertFalse(self.spool.broker_ack(expired, now=100))
        self.assertFalse(self.spool.fail(expired, 'transport.timeout', now=100))
        replacement = self.spool.claim(now=100)[0]
        self.assertNotEqual(replacement.token, expired.token)

    def test_unrepresentable_lease_duration_does_not_issue_an_already_expired_token(self):
        self.enqueue()
        with self.assertRaises(ReliabilityError):
            self.spool.claim(now=100, lease_seconds=5e-324)
        self.assertEqual(self.spool.get('M-1')['state'], 'pending')

    def test_unrepresentable_backoff_does_not_create_an_immediate_retry_loop(self):
        self.spool.close()
        self.spool = self.open(receipt_timeout_seconds=5e-324, base_backoff_seconds=5e-324, max_backoff_seconds=5e-324)
        self.enqueue()
        lease = self.spool.claim(now=100)[0]
        for operation in (lambda: self.spool.broker_ack(lease, now=101),
                          lambda: self.spool.fail(lease, 'transport.offline', now=101)):
            with self.assertRaises(ReliabilityError):
                operation()
            self.assertEqual(self.spool.get('M-1')['state'], 'inflight')
