"""Real receipt leases: fencing, crash recovery, clock changes and retry bounds."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import importlib
from threading import Barrier
import unittest
from uuid import uuid4

import psycopg

from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_support import IngestDatabaseCase
from tests.ingest_vectors import observation
from tests.test_ingest_repository import FaultConnection


MAX_COUNTER = 9_223_372_036_854_775_807


class OutboxInputTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('backend.ingest_reliability.outbox')

    def forbidden(self):
        self.fail('Invalid input must fail before connecting')

    def test_retry_configuration_is_bounded_and_constructor_has_no_io(self):
        self.module.OutboxRepository(self.forbidden)
        for value in (True, 0, -1, 3601, '300', 3.5):
            with self.subTest(value=value), self.assertRaises((ValueError, ReliabilityError)):
                self.module.OutboxRepository(self.forbidden, retry_max_seconds=value)
        with self.assertRaises((ValueError, ReliabilityError)):
            self.module.OutboxRepository(None)

    def test_claim_rejects_invalid_batch_lease_and_clocks_before_io(self):
        outbox = self.module.OutboxRepository(self.forbidden)
        for kwargs in ({'limit': True}, {'limit': 0}, {'limit': 201}, {'limit': '20'},
                       {'lease_seconds': True}, {'lease_seconds': 0}, {'lease_seconds': 301},
                       {'now': 'today'}, {'now': datetime(2026, 9, 24)},
                       {'now': datetime.max.replace(tzinfo=timezone.utc)}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, ReliabilityError)):
                outbox.claim(**kwargs)

    def test_finish_rejects_invalid_outcome_and_unbounded_error_text_before_io(self):
        outbox = self.module.OutboxRepository(self.forbidden)
        lease = self.module.DeliveryLease(uuid4(), uuid4(), 1, 'oilfield/v2/GW/D/ingest-ack', b'{}', 1)
        for kwargs in ({'success': 1}, {'success': None}, {'success': False, 'error_code': 'password=private'},
                       {'success': False, 'error_code': 'e' * 97}, {'success': False, 'error_code': '网络错误'},
                       {'success': True, 'error_code': 'mqtt.denied'},
                       {'success': True, 'now': datetime(2026, 9, 24)}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, ReliabilityError)):
                outbox.finish(lease, **kwargs)
        with self.assertRaises((ValueError, ReliabilityError)):
            outbox.finish(None, success=True)
        with self.assertRaises(FrozenInstanceError):
            lease.generation = 2


class OutboxTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        self.module = importlib.import_module('backend.ingest_reliability.outbox')
        self.result = self.repository().ingest(observation())
        self.now = self.scalar('SELECT clock_timestamp()', role='migrator') + timedelta(milliseconds=1)

    def row(self):
        with self.connect('migrator') as connection:
            cursor = connection.execute('SELECT * FROM ingest_reliability.deliveries WHERE receipt_id=%s',
                                        (self.result.receipt_id,))
            return dict(zip([column.name for column in cursor.description], cursor.fetchone()))

    def claim(self, **kwargs):
        return self.outbox().claim(now=self.now, **kwargs)

    def test_claim_returns_exact_persisted_receipt_after_releasing_transaction(self):
        leases = self.claim()
        self.assertEqual(len(leases), 1, 'A persisted pending receipt must be claimed, not hidden as an empty queue')
        lease = leases[0]
        self.assertEqual((lease.receipt_id, lease.generation, lease.attempts), (self.result.receipt_id, 1, 1))
        with self.connect('publisher') as connection:
            row = connection.execute('SELECT topic,payload FROM ingest_reliability.receipts WHERE receipt_id=%s',
                                     (lease.receipt_id,)).fetchone()
            self.assertEqual((lease.topic, lease.payload), row)
            # NOWAIT proves the claim returned after committing its row lock.
            connection.execute('SELECT receipt_id FROM ingest_reliability.deliveries WHERE receipt_id=%s FOR UPDATE NOWAIT',
                               (lease.receipt_id,))
        self.assertEqual(self.claim(), [])

    def test_success_means_broker_only_and_duplicate_finish_is_ignored(self):
        lease = self.claim()[0]
        self.assertTrue(self.outbox().finish(lease, success=True, now=self.now + timedelta(seconds=1)))
        state = self.row()
        self.assertEqual((state['sent_generation'], state['requested_generation']), (1, 1))
        self.assertIsNone(state['lease_token'])
        self.assertIsNotNone(state['broker_acked_at'])
        self.assertFalse(self.outbox().finish(lease, success=True, now=self.now + timedelta(seconds=2)))
        self.assertEqual(self.row(), state)
        self.assertEqual(self.outbox().claim(now=self.now + timedelta(days=1)), [])
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.receipts', role='publisher'), 1)

    def test_old_lease_cannot_swallow_new_request(self):
        outbox = self.outbox()
        lease = self.claim()[0]
        self.repository().ingest(observation())
        self.assertTrue(outbox.finish(lease, success=True, now=self.now + timedelta(seconds=1)))
        remaining = outbox.claim(limit=1, now=self.now + timedelta(seconds=1))
        self.assertEqual(remaining[0].receipt_id, self.result.receipt_id)
        self.assertEqual(remaining[0].generation, 2)
        self.assertNotEqual(remaining[0].token, lease.token)
        self.assertEqual(remaining[0].payload, lease.payload)

    def test_duplicate_unleased_requests_preserve_failure_backoff(self):
        lease = self.claim()[0]
        self.assertTrue(self.outbox().finish(lease, success=False, error_code='mqtt.not_authorized', now=self.now))
        before = self.row()
        self.repository().ingest(observation())
        after = self.row()
        self.assertEqual((after['next_attempt_at'], after['requested_generation']),
                         (before['next_attempt_at'], before['requested_generation']))
        self.assertEqual(self.claim(), [])

    def test_expired_unreclaimed_lease_cannot_report_success(self):
        lease = self.claim(lease_seconds=5)[0]
        self.assertFalse(self.outbox().finish(lease, success=True, now=self.now + timedelta(seconds=5)))
        self.assertEqual(self.row()['sent_generation'], 0)

    def test_clock_rollback_cannot_resurrect_an_already_expired_lease(self):
        outbox = self.outbox()
        expired = outbox.claim(now=self.now, lease_seconds=5)[0]
        self.assertFalse(outbox.finish(expired, success=True, now=self.now + timedelta(seconds=10)))
        rollback = self.now - timedelta(seconds=900)
        self.assertFalse(outbox.finish(expired, success=True, now=rollback))
        self.assertFalse(outbox.finish(expired, success=False, now=rollback))
        current = outbox.claim(now=rollback)[0]
        self.assertNotEqual(current.token, expired.token)
        self.assertEqual(self.row()['sent_generation'], 0)

    def test_reclaimed_lease_fences_late_success_and_failure(self):
        old = self.claim(lease_seconds=5)[0]
        current = self.outbox().claim(now=self.now + timedelta(seconds=6))[0]
        self.assertNotEqual(old.token, current.token)
        before = self.row()
        for success in (True, False):
            self.assertFalse(self.outbox().finish(old, success=success, now=self.now + timedelta(seconds=7)))
            self.assertEqual(self.row(), before)
        self.assertTrue(self.outbox().finish(current, success=True, now=self.now + timedelta(seconds=8)))

    def test_forged_token_or_generation_cannot_complete_another_claim(self):
        lease = self.claim()[0]
        for forged in (replace(lease, token=uuid4()), replace(lease, generation=2), replace(lease, receipt_id=uuid4())):
            self.assertFalse(self.outbox().finish(forged, success=True, now=self.now))
        self.assertEqual(self.row()['sent_generation'], 0)

    def test_retry_is_jittered_bounded_and_never_stops_after_twelve_failures(self):
        outbox = self.module.OutboxRepository(lambda: self.connect('publisher'), retry_max_seconds=7)
        now = self.now
        waits = []
        for index in range(20):
            lease = outbox.claim(now=now)[0]
            self.assertEqual(lease.attempts, index + 1)
            self.assertTrue(outbox.finish(lease, success=False, error_code='mqtt.no_matching_subscribers', now=now))
            state = self.row()
            delay = (state['next_attempt_at'] - now).total_seconds()
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 7)
            waits.append(delay)
            self.assertIsNone(state['broker_acked_at'])
            self.assertEqual((state['sent_generation'], state['requested_generation']), (0, 1))
            now = state['next_attempt_at']
        self.assertGreater(len(set(waits[10:])), 1, 'Capped retries still need jitter')
        self.assertEqual(len(outbox.claim(now=now)), 1)

    def test_retry_uses_persisted_attempts_not_caller_supplied_attempt_count(self):
        lease = self.claim()[0]
        forged = replace(lease, attempts=MAX_COUNTER)
        self.assertTrue(self.outbox().finish(forged, success=False, now=self.now))
        delay = (self.row()['next_attempt_at'] - self.now).total_seconds()
        self.assertGreater(delay, 0)
        self.assertLessEqual(delay, 2)

    def test_attempt_counter_saturates_without_permanent_abandonment(self):
        with self.connect('migrator') as connection:
            connection.execute('UPDATE ingest_reliability.deliveries SET attempts=%s', (MAX_COUNTER,))
        lease = self.claim()[0]
        self.assertEqual(lease.attempts, MAX_COUNTER)
        self.assertTrue(self.outbox().finish(lease, success=False, now=self.now))
        after = self.row()
        self.assertEqual(after['attempts'], MAX_COUNTER)
        self.assertEqual(len(self.outbox().claim(now=after['next_attempt_at'])), 1)

    def test_version_exhaustion_rolls_back_claim_instead_of_wrapping(self):
        with self.connect('migrator') as connection:
            connection.execute('UPDATE ingest_reliability.deliveries SET version=%s', (MAX_COUNTER,))
        with self.assertRaises(ReliabilityError):
            self.claim()
        state = self.row()
        self.assertEqual(state['version'], MAX_COUNTER)
        self.assertEqual(state['attempts'], 0)
        self.assertIsNone(state['lease_token'])

    def test_version_exhaustion_does_not_ack_a_failed_finish(self):
        lease = self.claim()[0]
        with self.connect('migrator') as connection:
            connection.execute('UPDATE ingest_reliability.deliveries SET version=%s', (MAX_COUNTER,))
        with self.assertRaises(ReliabilityError):
            self.outbox().finish(lease, success=True, now=self.now)
        self.assertEqual(self.row()['sent_generation'], 0)
        self.assertEqual(self.row()['lease_token'], lease.token)

    def test_clock_rollback_reanchors_once_and_preserves_pending_result(self):
        lease = self.claim()[0]
        self.outbox().finish(lease, success=False, now=self.now)
        backward = self.now - timedelta(days=2)
        outbox = self.module.OutboxRepository(lambda: self.connect('publisher'), retry_max_seconds=7)
        self.assertEqual(outbox.claim(now=backward), [])
        state = self.row()
        self.assertLessEqual(state['next_attempt_at'], backward + timedelta(seconds=7))
        anchored = state['next_attempt_at']
        self.assertEqual(outbox.claim(now=backward + timedelta(seconds=1)), [])
        self.assertEqual(self.row()['next_attempt_at'], anchored, 'Repeated polling must not move recovery farther away')
        self.assertEqual(len(outbox.claim(now=anchored)), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.worker_health WHERE error_code='ingest.clock_rollback'",
                                     role='publisher'), 1)
        self.assertEqual(self.row()['sent_generation'], 0)

    def test_clock_rollback_bounds_old_active_lease_without_discarding_its_token(self):
        lease = self.claim(lease_seconds=300)[0]
        backward = self.now - timedelta(hours=3)
        self.assertEqual(self.outbox().claim(now=backward, lease_seconds=30), [])
        state = self.row()
        self.assertLessEqual(state['lease_until'], backward + timedelta(seconds=300))
        self.assertEqual(state['lease_token'], lease.token)
        replacement = self.outbox().claim(now=backward + timedelta(seconds=301))[0]
        self.assertNotEqual(replacement.token, lease.token)

    def test_two_publishers_claim_disjoint_receipts(self):
        for index in range(3):
            self.repository().ingest(observation(f'M-{index + 2}'))
        barrier = Barrier(2)
        def claim():
            outbox = self.outbox()
            barrier.wait(timeout=5)
            return outbox.claim(limit=2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            batches = [future.result(timeout=10) for future in futures]
        self.assertEqual([len(batch) for batch in batches], [2, 2])
        self.assertEqual(len({lease.receipt_id for batch in batches for lease in batch}), 4)

    def test_locked_delivery_is_skipped_without_blocking_other_receipts(self):
        second = self.repository().ingest(observation('M-2'))
        with self.connect('migrator') as blocker:
            blocker.execute('SELECT receipt_id FROM ingest_reliability.deliveries WHERE receipt_id=%s FOR UPDATE',
                            (self.result.receipt_id,))
            leases = self.outbox().claim(limit=20)
            self.assertEqual([lease.receipt_id for lease in leases], [second.receipt_id])
        self.assertEqual(len(self.outbox().claim()), 1)

    def test_claim_commit_uncertainty_never_returns_a_lease_but_later_expires(self):
        outbox = self.module.OutboxRepository(lambda: FaultConnection(self.connect('publisher'), lost_commit_response=True))
        with self.assertRaises(ReliabilityError):
            outbox.claim(now=self.now, lease_seconds=5)
        self.assertIsNotNone(self.row()['lease_token'])
        self.assertEqual(len(self.outbox().claim(now=self.now + timedelta(seconds=6))), 1)

    def test_finish_commit_uncertainty_never_claims_success_and_retry_is_idempotent(self):
        lease = self.claim()[0]
        outbox = self.module.OutboxRepository(lambda: FaultConnection(self.connect('publisher'), lost_commit_response=True))
        with self.assertRaises(ReliabilityError):
            outbox.finish(lease, success=True, now=self.now)
        self.assertEqual(self.row()['sent_generation'], 1)
        self.assertFalse(self.outbox().finish(lease, success=True, now=self.now))

    def test_db_failures_do_not_become_empty_queue_or_completed_delivery(self):
        def failed_connect():
            raise psycopg.OperationalError('private DSN must not appear')
        outbox = self.module.OutboxRepository(failed_connect)
        lease = self.claim()[0]
        for operation in (lambda: outbox.claim(), lambda: outbox.finish(lease, success=True)):
            with self.assertRaises(ReliabilityError) as caught:
                operation()
            self.assertNotIn('private DSN', str(caught.exception))
        self.assertEqual(self.row()['sent_generation'], 0)

    def test_bad_row_in_batch_rolls_back_all_claims(self):
        second = self.repository().ingest(observation('M-2'))
        with self.connect('migrator') as connection:
            connection.execute('UPDATE ingest_reliability.deliveries SET version=%s WHERE receipt_id=%s',
                               (MAX_COUNTER, second.receipt_id))
        with self.assertRaises(ReliabilityError):
            self.outbox().claim(limit=2)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.deliveries WHERE lease_token IS NOT NULL',
                                     role='publisher'), 0)

    def test_clock_warning_failure_rolls_back_rebase_and_does_not_erase_deadline(self):
        lease = self.claim()[0]
        self.outbox().finish(lease, success=False, now=self.now)
        before = self.row()
        from tests.test_ingest_repository import InjectedWriteFailure
        outbox = self.module.OutboxRepository(lambda: FaultConnection(self.connect('publisher'),
                                              fragment='INSERT INTO ingest_reliability.worker_health'))
        with self.assertRaises(InjectedWriteFailure):
            outbox.claim(now=self.now - timedelta(days=2))
        self.assertEqual(self.row(), before)

    def test_request_after_broker_success_reuses_body_and_starts_new_generation(self):
        first = self.claim()[0]
        self.assertTrue(self.outbox().finish(first, success=True, now=self.now))
        self.repository().ingest(observation())
        current = self.outbox().claim()[0]
        self.assertEqual((current.receipt_id, current.payload, current.generation),
                         (first.receipt_id, first.payload, 2))

    def test_failure_of_old_generation_leaves_new_generation_pending(self):
        first = self.claim()[0]
        self.repository().ingest(observation())
        self.assertTrue(self.outbox().finish(first, success=False, now=self.now))
        state = self.row()
        self.assertEqual((state['requested_generation'], state['sent_generation']), (2, 0))
        current = self.outbox().claim(now=state['next_attempt_at'])[0]
        self.assertEqual(current.generation, 2)
        self.assertEqual(current.payload, first.payload)

    def test_last_generation_can_finish_but_a_new_request_cannot_wrap(self):
        with self.connect('migrator') as connection:
            connection.execute('UPDATE ingest_reliability.deliveries SET requested_generation=%s,sent_generation=%s',
                               (MAX_COUNTER, MAX_COUNTER - 1))
        lease = self.claim()[0]
        self.assertEqual(lease.generation, MAX_COUNTER)
        self.assertTrue(self.outbox().finish(lease, success=True, now=self.now))
        with self.assertRaises(ReliabilityError):
            self.repository().ingest(observation())
        self.assertEqual((self.row()['requested_generation'], self.row()['sent_generation']),
                         (MAX_COUNTER, MAX_COUNTER))

    def test_timezone_offset_does_not_change_the_lease_duration(self):
        east = self.now.astimezone(timezone(timedelta(hours=8)))
        leases = self.outbox().claim(now=east, lease_seconds=30)
        self.assertEqual(len(leases), 1)
        self.assertEqual(self.row()['lease_until'], self.now + timedelta(seconds=30))
