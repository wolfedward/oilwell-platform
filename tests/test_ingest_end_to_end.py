"""Real owned processes, production DDL and exact-byte receipt closure."""
from dataclasses import replace
from hashlib import sha256
import json

from tests.ingest_support import IngestDatabaseCase
from tests.ingest_stack import Stack


class IngestEndToEndTests(IngestDatabaseCase):
    def stack(self, **kwargs):
        return Stack(self, **kwargs)

    def test_real_gateway_center_publisher_complete_one_exact_record(self):
        with self.stack() as stack:
            stack.start()
            obs = stack.enqueue_one()
            stack.await_gateway_state('stored')
            stack.await_counts(messages=1, samples=1)
            record = stack.gateway_record()
            self.assertEqual(record['payload'], obs.payload)
            self.assertEqual(record['payload_sha256'], sha256(obs.payload).hexdigest())
            self.assertEqual(record['confirmation_mode'], 'application')
            self.assertEqual(stack.fact_payload(), obs.payload)
            self.assertEqual(stack.database_counts()['receipts'], 1)
            self.assertEqual(stack.receipt()['payload_sha256'], record['payload_sha256'])

    def test_correlatable_rejection_finalizes_as_quarantine_not_as_telemetry(self):
        with self.stack() as stack:
            stack.start()
            obs = stack.observation()
            body = json.loads(obs.payload)
            body['samples'] = []
            rejected = replace(obs, payload=json.dumps(body).encode())
            stack.enqueue_one(rejected)
            stack.await_gateway_state('quarantined')
            self.assertEqual(stack.database_counts(), {'messages': 0, 'samples': 0, 'facts': 1, 'receipts': 1})
            self.assertEqual(stack.fact_payload(), rejected.payload)
            self.assertEqual(stack.receipt()['reason_code'], 'contract_invalid')
            self.assertEqual(stack.gateway_record()['last_error_code'], 'contract_invalid')

    def test_unassociable_input_keeps_raw_fact_without_inventing_a_receipt(self):
        with self.stack() as stack:
            stack.start()
            raw = b'\xff\x00not-json'
            peer = stack.broker.client('gateway-GW-001')
            self.assertEqual(peer.publish('oilfield/v1/GW-001/D/telemetry', raw), 0)
            stack.await_condition(lambda: stack.database_counts()['facts'] == 1, 'isolated fact')
            self.assertEqual(stack.database_counts(), {'messages': 0, 'samples': 0, 'facts': 1, 'receipts': 0})
            self.assertEqual(stack.fact_payload(), raw)

    def test_http_registry_keeps_unknown_bad_quality_and_unmapped_originals(self):
        with self.stack(http=True) as stack:
            stack.start()
            registered = stack.register_device()
            self.assertEqual(registered['status'], 'active')
            for identifier, device, quality, point, expected_reason in [
                ('UNKNOWN', 'UNKNOWN', 'good', 'load', 'not_registered'),
                ('BAD', 'D', 'bad', 'load', 'quality_bad'),
                ('UNMAPPED', 'D', 'good', 'unexpected', 'unknown_point'),
            ]:
                with self.subTest(identifier=identifier):
                    obs = stack.observation(identifier, device=device, quality=quality, point=point)
                    stack.enqueue_one(obs)
                    stack.await_gateway_state('stored', identifier)
                    self.assertEqual(stack.fact_payload(identifier), obs.payload)
                    resolved = [row for row in stack.resolved_samples(device) if row['message_id'] == identifier]
                    self.assertEqual(len(resolved), 1)
                    self.assertIn(expected_reason, resolved[0]['reasons'])
            stack.await_counts(messages=3, samples=3)

    def test_actual_browser_case_preview_close_audit_and_resend(self):
        with self.stack(http=True) as stack:
            stack.start(receipt_delivery=False)
            obs = stack.observation('BROWSER')
            body = json.loads(obs.payload)
            body['samples'] = []
            stack.enqueue_one(replace(obs, payload=json.dumps(body).encode()))
            stack.await_condition(lambda: stack.database_counts()['facts'] == 1, 'browser case')
            stack.stop_process('gateway')
            evidence = stack.browser_case_flow()
            self.assertTrue(evidence['ok'], evidence)
            self.assertEqual(stack.case_state(), 'closed')
            self.assertEqual(stack.audit_actions(), ['raw.preview', 'case.close', 'receipt.resend'])
            self.assertEqual(stack.database_counts()['samples'], 0)
