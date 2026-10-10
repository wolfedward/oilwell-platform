import importlib.util
import unittest
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from tests.inversion_worker import RunCase,REF,command

class RunFoundationTests(unittest.TestCase):
    def test_durable_service_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.inversion.runs'),'durable request service is required')

class RunTests(RunCase):
    def test_same_request_key_replays_after_session_refresh(self):
        from dataclasses import replace
        cmd=command(); first=self.service.create(self.request,cmd,REF)
        replay=self.service.create(self.request,cmd,replace(REF,session_id=2))
        self.assertEqual(first.id,replay.id)

    def test_snapshot_actor_cannot_be_substituted(self):
        import json
        from hashlib import sha256
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.input_contracts import InputSnapshot
        from tests.inversion_worker import authority
        run=self.create(); attempt=self.service.claim(uuid4()); result=self.result()
        body=json.loads(result.snapshot.canonical); body['actor']['user_id']='2'
        raw=canonical_bytes(body); snapshot=InputSnapshot(body,raw,sha256(raw).hexdigest())
        metadata=result.body; metadata['input_sha256']=snapshot.content_sha256
        altered=replace(result,snapshot=snapshot,metadata=canonical_bytes(metadata))
        with self.assertRaises(RuntimeError): self.service.publish(attempt,altered,authority(REF))
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),0)
    def test_running_cancel_retains_slot_until_supervisor_reaps(self):
        run=self.create(); attempt=self.service.claim(uuid4()); active=self.service.get(run.id)
        cancelled=self.service.cancel(run.id,command(active.version))
        self.assertEqual((cancelled.state,cancelled.reason_code),('running','cancel_requested'))
        self.assertEqual(self.scalar('SELECT running FROM inversion.capacity'),1)
        self.service.finish(attempt,'cancelled','user_cancelled')
        self.assertEqual(self.service.get(run.id).state,'cancelled')
        self.assertEqual(self.scalar('SELECT running FROM inversion.capacity'),0)

    def test_heartbeat_keeps_immutable_deadline(self):
        run=self.create(); attempt=self.service.claim(uuid4()); before=self.service.get(run.id)
        self.service.heartbeat(attempt)
        after=self.service.get(run.id)
        self.assertEqual(after.deadline,before.deadline)
        self.assertEqual(after.version,before.version+1)
    def test_request_plus_snapshot_share_one_input_ceiling(self):
        from backend.inversion.input_contracts import InputSnapshot
        from backend.inversion.contracts import canonical_bytes
        from hashlib import sha256
        import json
        from tests.inversion_worker import authority
        run=self.create(); attempt=self.service.claim(uuid4()); result=self.result()
        body=json.loads(result.snapshot.canonical); body['padding']=''
        raw=canonical_bytes(body); body['padding']='x'*(64*1024**2-len(raw)); raw=canonical_bytes(body)
        snapshot=InputSnapshot(body,raw,sha256(raw).hexdigest())
        metadata=result.body; metadata['input_sha256']=snapshot.content_sha256
        enlarged=replace(result,snapshot=snapshot,metadata=canonical_bytes(metadata))
        with self.assertRaises(RuntimeError): self.service.publish(attempt,enlarged,authority(REF))
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),0)
        self.service.finish(attempt,'failed','input_size_limit')
        self.assertEqual(self.scalar('SELECT used_bytes FROM inversion.capacity'),run.request_bytes)

    def test_not_applicable_becomes_blocked_without_result(self):
        from backend.inversion.contracts import canonical_bytes
        from tests.inversion_worker import authority
        run=self.create(); attempt=self.service.claim(uuid4()); result=self.result()
        body=result.body; body['prediction_status']='not_applicable'; body['prediction_reason_codes']=['insufficient_coverage']
        result=replace(result,metadata=canonical_bytes(body))
        self.assertIsNone(self.service.publish(attempt,result,authority(REF)))
        terminal=self.service.get(run.id)
        self.assertEqual((terminal.state,terminal.reason_code),('blocked','not_applicable'))
        from backend.inversion.queries import InversionQueries
        self.assertEqual(InversionQueries(self.service.read_connect).run_diagnostics(terminal)['reasons'],['insufficient_coverage'])
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),0)
        self.assertEqual(self.scalar('SELECT used_bytes FROM inversion.capacity'),run.request_bytes)
    def test_missing_frozen_member_refuses_before_reservation(self):
        with self.assertRaises(RuntimeError): self.service.create(replace(self.request,member_ordinal=1),command(),REF)
        self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),0)

    def test_publish_rechecks_revoked_input(self):
        from dataclasses import asdict
        from psycopg.types.json import Jsonb
        from tests.inversion_worker import ACTOR,authority
        run=self.create(); attempt=self.service.claim(uuid4()); prepared=self.result()
        with self.inversion_fixture.connect() as conn:
            conn.execute("INSERT INTO inversion.reviews(id,subject_id,version,action,actor,reason,body) VALUES(%s,%s,4,'revoke',%s,'Synthetic revocation',%s)",
                (uuid4(),self.request.parameter_id,Jsonb(asdict(ACTOR)),Jsonb({})))
        self.assertIsNone(self.service.publish(attempt,prepared,authority(REF)))
        self.assertEqual(self.service.get(run.id).state,'blocked')
        from backend.inversion.queries import InversionQueries
        self.assertEqual(InversionQueries(self.service.read_connect).run_diagnostics(self.service.get(run.id))['reasons'],['parameter_pin_changed'])
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),0)
        self.assertEqual(self.scalar('SELECT used_bytes FROM inversion.capacity'),run.request_bytes)

    def test_cancel_and_commit_have_one_terminal(self):
        from tests.inversion_worker import authority
        run=self.create(); attempt=self.service.claim(uuid4()); prepared=self.result()
        active=self.service.get(run.id)
        def publish():
            try: return self.service.publish(attempt,prepared,authority(REF))
            except RuntimeError: return None
        def cancel():
            try: return self.service.cancel(run.id,command(active.version))
            except RuntimeError: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(publish),pool.submit(cancel)]
            for future in futures: future.result()
        if self.service.get(run.id).reason_code=='cancel_requested':
            self.service.finish(attempt,'cancelled','user_cancelled')
        terminal=self.service.get(run.id)
        self.assertIn(terminal.state,('completed','cancelled'))
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),int(terminal.state=='completed'))
        self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),0)

    def test_success_settles_request_snapshot_metadata_and_chunks_atomically(self):
        from tests.inversion_worker import authority
        run=self.create(); attempt=self.service.claim(uuid4()); prepared=self.result()
        first=self.service.publish(attempt,prepared,authority(REF))
        replay=self.service.publish(attempt,prepared,authority(REF))
        self.assertEqual(first,replay)
        self.assertEqual(self.scalar('SELECT used_bytes FROM inversion.capacity'),run.request_bytes+len(prepared.snapshot.canonical)+prepared.result_bytes)
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),1)

    def test_publish_refuses_revoked_authority_even_with_old_snapshot(self):
        from tests.inversion_worker import authority
        run=self.create(); attempt=self.service.claim(uuid4()); prepared=self.result(); old=authority(REF)
        self.allowed=False
        with self.assertRaises(RuntimeError): self.service.publish(attempt,prepared,old)
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),0)
    def test_committed_request_survives_http_disconnect(self):
        first=self.create()
        self.assertEqual(self.service.get(first.id).state,'queued')
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.run_requests'),1)

    def test_same_key_returns_same_run(self):
        cmd=command(); first=self.service.create(self.request,cmd,REF)
        replay=self.service.create(self.request,cmd,REF)
        self.assertEqual(first.id,replay.id)
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.run_requests'),1)
        self.assertLessEqual(self.scalar('SELECT count(*) FROM inversion.results'),1)
        self.allowed=False
        with self.assertRaises(RuntimeError): self.service.create(self.request,cmd,REF)

    def test_different_body_same_key_conflicts(self):
        cmd=command(); self.service.create(self.request,cmd,REF)
        with self.assertRaises(RuntimeError): self.service.create(replace(self.request,member_ordinal=1),cmd,REF)

    def test_claim_one_slot_and_fenced_publication(self):
        first=self.create(); self.create()
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts=list(pool.map(self.service.claim,[uuid4(),uuid4()]))
        self.assertEqual(sum(a is not None for a in attempts),1)
        attempt=next(a for a in attempts if a is not None)
        self.assertEqual(attempt.generation,1)
        self.assertEqual(self.scalar('SELECT running FROM inversion.capacity'),1)
        with self.assertRaises(RuntimeError): self.service.finish(replace(attempt,generation=2),'failed','stale')
        self.service.finish(attempt,'failed','test_failure')
        self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),80*1024**2)

    def test_queued_cancel_releases_capacity_and_counts_request(self):
        run=self.create(); self.service.cancel(run.id,command(run.version))
        self.assertEqual(self.service.get(run.id).state,'cancelled')
        self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),0)
        self.assertGreater(self.scalar('SELECT used_bytes FROM inversion.capacity'),0)

    def test_retry_creates_successor_with_fresh_authority(self):
        run=self.create(); cancelled=self.service.cancel(run.id,command(run.version))
        successor=self.service.retry(run.id,command(cancelled.version),REF)
        self.assertNotEqual(run.id,successor.id)
        self.assertEqual(successor.predecessor_id,run.id)
        self.assertEqual(self.service.get(run.id).state,'cancelled')
