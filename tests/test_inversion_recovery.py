from uuid import uuid4
from tests.inversion_worker import RunCase

class RecoveryTests(RunCase):
    def test_timeout_during_commit_reconciles_durable_terminal(self):
        from contextlib import contextmanager
        from tests.inversion_worker import authority,REF
        run=self.create(); attempt=self.service.claim(uuid4()); result=self.result()
        original=self.service.connect
        @contextmanager
        def lost_response():
            with original() as conn: yield conn
            raise OSError('Injected lost commit acknowledgement')
        self.service.connect=lost_response
        with self.assertRaises(OSError): self.service.publish(attempt,result,authority(REF))
        self.service.connect=original
        terminal=self.service.finish(attempt,'failed','deadline_expired')
        self.assertEqual(terminal.state,'completed')
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),1)
        self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),0)

    def test_database_commit_failure_rolls_back_results_audit_and_settlement(self):
        from tests.inversion_worker import authority,REF
        run=self.create(); attempt=self.service.claim(uuid4()); result=self.result()
        before=self.scalar('SELECT count(*) FROM inversion.audit')
        with self.inversion_fixture.connect('owner') as conn:
            conn.execute("ALTER TABLE inversion.audit ADD CONSTRAINT task9_commit_failure CHECK(action<>'runs.completed')")
        try:
            with self.assertRaises(Exception): self.service.publish(attempt,result,authority(REF))
            self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),0)
            self.assertEqual(self.scalar('SELECT count(*) FROM inversion.result_chunks'),0)
            self.assertEqual(self.scalar('SELECT count(*) FROM inversion.audit'),before)
            self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),80*1024**2)
        finally:
            with self.inversion_fixture.connect('owner') as conn:
                conn.execute('ALTER TABLE inversion.audit DROP CONSTRAINT task9_commit_failure')
        self.service.finish(attempt,'failed','database_commit_failure')
        self.assertEqual(self.scalar('SELECT used_bytes FROM inversion.capacity'),run.request_bytes)

    def test_late_generation_is_rejected_by_database_fence(self):
        from dataclasses import replace
        from psycopg.types.json import Jsonb
        from tests.inversion_worker import authority,REF
        run=self.create(); attempt=self.service.claim(uuid4()); result=self.result()
        with self.assertRaises(RuntimeError): self.service.publish(replace(attempt,generation=2),result,authority(REF))
        self.service.finish(attempt,'failed','worker_interrupted')
        with self.assertRaises(Exception):
            with self.inversion_fixture.connect() as conn:
                conn.execute('INSERT INTO inversion.results(id,run_id,generation,input_snapshot,body,content_sha256) VALUES(%s,%s,%s,%s,%s,%s)',
                    (uuid4(),run.id,attempt.generation,Jsonb({}),Jsonb({}),'a'*64))
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),0)
    def test_restarted_owner_fails_without_autoretry(self):
        run=self.create(); owner=uuid4(); attempt=self.service.claim(owner)
        # Internal supervisor has observed/reaped its owned process before this
        # method; merely naming an owner cannot recover an unexpired attempt.
        self.service.finish(attempt,'failed','worker_interrupted')
        current=self.service.get(run.id)
        self.assertEqual((current.state,current.reason_code),('failed','worker_interrupted'))
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.run_requests'),1)
        self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),0)
        self.assertGreater(self.scalar('SELECT used_bytes FROM inversion.capacity'),0)

    def test_expired_deadline_proves_lost_publication_authority(self):
        from unittest.mock import patch
        from datetime import datetime,timedelta,timezone
        run=self.create(); owner=uuid4()
        with patch('backend.inversion.runs.datetime') as clock:
            clock.now.return_value=datetime.now(timezone.utc)-timedelta(seconds=181)
            self.service.claim(owner)
        discovered=self.service.expired_owner()
        self.assertEqual(discovered,owner)
        self.service.recover(discovered)
        self.assertEqual(self.service.get(run.id).reason_code,'deadline_expired')
        self.assertIsNone(self.service.expired_owner())

    def test_recovery_does_not_touch_another_owner(self):
        run=self.create(); self.service.claim(uuid4()); self.service.recover(uuid4())
        self.assertEqual(self.service.get(run.id).state,'running')
