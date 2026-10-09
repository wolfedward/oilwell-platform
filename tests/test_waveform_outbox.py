"""Durable publisher role tests on the verified owned database."""
from datetime import timedelta
from dataclasses import replace
import importlib
from uuid import uuid4
from tests.waveform_support import WaveformDatabaseCase, LIMITS, FIXED_TIME


class OutboxFixture(WaveformDatabaseCase):
    def outbox(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.waveforms.outbox'), 'durable outbox is required')
        from backend.waveforms.outbox import WaveOutbox
        self.now = FIXED_TIME
        return WaveOutbox(lambda: self.connect('receipt_publisher'), gateway_id='GW', limits=LIMITS, clock=lambda: self.now)

    def receipt(self,topic='oil/v2/GW/D/waveform-ack'):
        fact = self.insert_fact(payload=b'synthetic-'+uuid4().bytes)
        outcome = self.insert_outcome(fact)
        receipt = uuid4()
        with self.connect() as conn:
            conn.execute("INSERT INTO waveforms.receipts(receipt_id,intake_gateway_id,fact_id,outcome_id,scope,topic,payload,recorded_at) VALUES(%s,'GW',%s,%s,'message',%s,%s,%s)", (receipt, fact, outcome, topic, b'{ "persisted":true }', FIXED_TIME))
            conn.execute("INSERT INTO waveforms.deliveries(receipt_id,intake_gateway_id,next_attempt_at) VALUES(%s,'GW',%s)", (receipt, FIXED_TIME))
        return receipt

    def state(self, receipt):
        with self.connect('receipt_publisher') as conn:
            return conn.execute('SELECT requested_generation,sent_generation,lease_token,attempts,last_error_code FROM waveforms.deliveries WHERE receipt_id=%s', (receipt,)).fetchone()


class WaveformOutboxTests(OutboxFixture):
    def test_nullable_failure_records_generic_error_and_bounded_retry(self):
        outbox=self.outbox(); receipt=self.receipt()
        lease,=outbox.claim(uuid4(),limit=1)
        try:
            finished=outbox.finish(lease,delivered=False,error_code=None)
        except ValueError:
            self.fail('nullable failure must become a durable generic failure')
        self.assertTrue(finished)
        self.assertEqual(self.state(receipt),(1,0,None,1,'delivery.failed'))
        self.assertEqual(self.scalar('SELECT next_attempt_at FROM waveforms.deliveries',role='receipt_publisher'),FIXED_TIME+timedelta(seconds=2))
        with self.connect('receipt_publisher') as conn:
            self.assertEqual(conn.execute("SELECT error_code,last_failure FROM waveforms.worker_health WHERE component='receipt_publisher'").fetchone(),('delivery.failed',FIXED_TIME))
        self.assertEqual(outbox.claim(uuid4(),limit=1),())

    def test_exhausted_revision_is_nonretryable_and_claim_rolls_back(self):
        from backend.waveforms.errors import WaveformError
        outbox=self.outbox(); receipt=self.receipt()
        with self.connect('migration_owner') as conn:
            conn.execute('UPDATE waveforms.state_head SET version=9223372036854775807')
        with self.assertRaises(WaveformError) as caught: outbox.claim(uuid4(),limit=1)
        self.assertEqual(caught.exception.code,'waveform.invariant')
        self.assertIsNone(self.state(receipt)[2])

    def test_clock_is_rechecked_after_delivery_lock_wait(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        outbox=self.outbox(); receipt=self.receipt()
        lease,=outbox.claim(uuid4(),limit=1)
        with self.connect('migration_owner') as blocker, ThreadPoolExecutor(1) as pool:
            blocker.execute('SELECT receipt_id FROM waveforms.deliveries WHERE receipt_id=%s FOR UPDATE',(receipt,))
            future=pool.submit(outbox.finish,lease,delivered=True,error_code=None)
            try:
                with self.admin() as observer:
                    deadline=time.monotonic()+2
                    while time.monotonic()<deadline:
                        waiting=observer.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename=%s AND wait_event_type='Lock'",(self.settings['receipt_publisher']['user'],)).fetchone()[0]
                        observer.commit()
                        if waiting: break
                        time.sleep(.02)
                    self.assertTrue(waiting,'finish must reach the actual delivery row lock')
                self.now+=timedelta(seconds=31)
            finally: blocker.commit()
            self.assertFalse(future.result(timeout=5))

    def test_repeated_frozen_clock_does_not_rewrite_failure_time(self):
        outbox=self.outbox(); self.receipt()
        outbox.claim(uuid4(),limit=1)
        self.now -= timedelta(seconds=1)
        outbox.claim(uuid4(),limit=1)
        previous=self.scalar("SELECT last_failure FROM waveforms.worker_health WHERE component='receipt_publisher'",role='receipt_publisher')
        self.now -= timedelta(seconds=1)
        outbox.claim(uuid4(),limit=1)
        self.assertEqual(self.scalar("SELECT last_failure FROM waveforms.worker_health WHERE component='receipt_publisher'",role='receipt_publisher'),previous)

    def test_connection_failure_health_preserves_rollback(self):
        outbox=self.outbox(); self.receipt()
        self.assertTrue(hasattr(outbox,'record_error'),'publisher connection failures need bounded durable health')
        outbox.claim(uuid4(),limit=1)
        self.now -= timedelta(seconds=1)
        outbox.record_error('mqtt.connect_failed')
        self.assertEqual(self.scalar("SELECT error_code FROM waveforms.worker_health WHERE component='receipt_publisher'",role='receipt_publisher'),'clock_rollback')
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.deliveries WHERE lease_token IS NOT NULL',role='receipt_publisher'),0)

    def test_cap_includes_expired_and_reclaim_works_at_cap(self):
        outbox=self.outbox()
        for _ in range(65): self.receipt()
        leases=outbox.claim(uuid4(),limit=64)
        self.assertEqual(len(leases),64)
        self.assertEqual(outbox.claim(uuid4(),limit=1),())
        self.now += timedelta(seconds=31)
        self.assertEqual(len(outbox.claim(uuid4(),limit=64)),64)
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.deliveries WHERE lease_token IS NOT NULL',role='receipt_publisher'),64)
        self.now -= timedelta(seconds=1)
        self.assertEqual(outbox.claim(uuid4(),limit=1),())
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.deliveries WHERE lease_token IS NOT NULL',role='receipt_publisher'),0)

    def test_overflow_fails_closed_and_does_not_mutate(self):
        from backend.waveforms.errors import WaveformError
        outbox=self.outbox()
        for _ in range(65): self.receipt()
        with self.connect('migration_owner') as conn:
            conn.execute('UPDATE waveforms.deliveries SET lease_token=gen_random_uuid(),lease_generation=1,lease_until=%s',(FIXED_TIME+timedelta(seconds=30),))
        with self.assertRaises(WaveformError) as caught: outbox.claim(uuid4(),limit=1)
        self.assertEqual(caught.exception.code,'waveform.invariant')
        self.assertIsNone(self.scalar("SELECT last_clock_seen FROM waveforms.capacity WHERE bucket='GW'",role='receipt_publisher'))

    def test_backoff_and_attempts_saturate_without_overflow(self):
        outbox=self.outbox(); receipt=self.receipt()
        with self.connect('migration_owner') as conn:
            conn.execute('UPDATE waveforms.deliveries SET attempts=2147483647 WHERE receipt_id=%s',(receipt,))
        lease,=outbox.claim(uuid4(),limit=1)
        self.assertTrue(outbox.finish(lease,delivered=False,error_code='mqtt.timeout'))
        self.assertEqual(self.state(receipt)[3],2147483647)
        self.assertEqual(self.scalar('SELECT next_attempt_at FROM waveforms.deliveries',role='receipt_publisher'),FIXED_TIME+timedelta(seconds=300))

    def test_publisher_privileges_are_narrow(self):
        import psycopg
        from backend.waveforms.privileges import probe_role
        probe_role(lambda:self.connect('receipt_publisher'),'receipt_publisher')
        for query in ["INSERT INTO waveforms.facts DEFAULT VALUES", "UPDATE waveforms.capacity SET used_facts=0", "UPDATE waveforms.deliveries SET requested_generation=2"]:
            with self.subTest(query=query), self.connect('receipt_publisher') as conn:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege): conn.execute(query)
                conn.rollback()

    def test_old_generation_cannot_finish_new_delivery(self):
        outbox = self.outbox(); receipt = self.receipt()
        old, = outbox.claim(uuid4(), limit=1)
        self.now += timedelta(seconds=31)
        current, = outbox.claim(uuid4(), limit=1)
        before = self.state(receipt)
        version=self.scalar('SELECT version FROM waveforms.state_head',role='receipt_publisher')
        self.assertFalse(outbox.finish(old, delivered=True, error_code=None))
        self.assertEqual(self.state(receipt), before)
        self.assertEqual(self.scalar('SELECT version FROM waveforms.state_head',role='receipt_publisher'),version)
        self.assertTrue(outbox.finish(current, delivered=True, error_code=None))

    def test_new_request_survives_old_success(self):
        outbox = self.outbox(); receipt = self.receipt()
        lease, = outbox.claim(uuid4(), limit=1)
        with self.connect() as conn:
            conn.execute('UPDATE waveforms.deliveries SET requested_generation=2,version=version+1 WHERE receipt_id=%s', (receipt,))
        self.assertTrue(outbox.finish(lease, delivered=True, error_code=None))
        self.assertEqual(self.state(receipt)[:2], (2, 1))
        new, = outbox.claim(uuid4(), limit=1)
        self.assertEqual((new.receipt_id,new.payload,new.generation), (receipt,b'{ "persisted":true }',2))

    def test_rollback_invalidates_all_tokens_and_freezes_until_catchup(self):
        outbox = self.outbox(); receipt = self.receipt()
        old, = outbox.claim(uuid4(), limit=1)
        self.now -= timedelta(seconds=1)
        self.assertEqual(outbox.claim(uuid4(), limit=1), ())
        self.assertIsNone(self.state(receipt)[2])
        version = self.scalar('SELECT version FROM waveforms.state_head',role='receipt_publisher')
        self.assertFalse(outbox.finish(old,delivered=True,error_code=None))
        self.assertEqual(self.scalar('SELECT version FROM waveforms.state_head',role='receipt_publisher'),version)
        self.now += timedelta(seconds=1)
        new, = outbox.claim(uuid4(),limit=1)
        self.assertNotEqual(new.lease_token,old.lease_token)
        self.assertFalse(outbox.finish(old,delivered=True,error_code=None))

    def test_monotonic_deadline_fences_finish(self):
        outbox=self.outbox(); receipt=self.receipt()
        lease,=outbox.claim(uuid4(),limit=1)
        self.assertFalse(outbox.finish(replace(lease,_deadline=0),delivered=True,error_code=None))
        self.assertEqual(self.state(receipt)[1],0)

    def test_failure_backoff_replays_identical_bytes(self):
        outbox=self.outbox(); self.receipt()
        lease,=outbox.claim(uuid4(),limit=1)
        self.assertTrue(outbox.finish(lease,delivered=False,error_code='mqtt.puback_timeout'))
        self.assertEqual(outbox.claim(uuid4(),limit=1),())
        self.now += timedelta(seconds=2)
        retry,=outbox.claim(uuid4(),limit=1)
        self.assertEqual((retry.receipt_id,retry.payload,retry.topic),(lease.receipt_id,lease.payload,lease.topic))
