"""Every fault must leave evidence of refusal/preservation and actual recovery."""
from tests.ingest_support import IngestDatabaseCase
from tests.ingest_stack import Stack


class IngestFailureRecoveryTests(IngestDatabaseCase):
    def stack(self, **kwargs):
        return Stack(self, **kwargs)

    def test_commit_then_lost_receipt_recovers_once_after_gateway_restart(self):
        with self.stack() as stack:
            stack.start(receipt_delivery=False)
            obs = stack.enqueue_one()
            stack.await_counts(messages=1, samples=1)
            stack.await_gateway_state('awaiting_receipt')
            stack.restart_gateway()
            stack.enable_receipt_delivery()
            stack.await_gateway_state('stored')
            self.assertEqual(stack.database_counts()['samples'], 1)
            self.assertEqual(stack.gateway_record()['payload'], obs.payload)

    def test_database_connection_refusal_never_completes_gateway_record(self):
        with self.stack() as stack:
            stack.start(database_refused=True)
            obs = stack.enqueue_one()
            stack.await_gateway_error('mqtt.no_matching_subscribers')
            self.assertNotEqual(stack.gateway_record()['state'], 'stored')
            self.assertEqual(stack.database_counts()['facts'], 0)
            self.assertIn('ingest.connection_unavailable', stack.process_log('center'))
            stack.restart_center()
            stack.await_gateway_state('stored')
            self.assertEqual(stack.fact_payload(), obs.payload)

    def test_actual_transaction_failure_rolls_back_samples_fact_and_receipt(self):
        with self.stack() as stack:
            stack.start()
            with stack.reject_fact_transaction():
                stack.enqueue_one()
                stack.await_process_event('center', 'ingest_failed')
                self.assertEqual(stack.database_counts(), {'messages': 0, 'samples': 0, 'facts': 0, 'receipts': 0})
                self.assertNotEqual(stack.gateway_record()['state'], 'stored')
            stack.await_gateway_state('stored')
            stack.await_counts(messages=1, samples=1)

    def test_process_crash_before_inbound_ack_replays_only_one_durable_fact(self):
        with self.stack() as stack:
            stack.start(center_fault='before_ack', receipt_delivery=False)
            stack.enqueue_one()
            stack.await_process_exit('center', 71)
            stack.await_counts(messages=1, samples=1)
            stack.restart_center()
            stack.await_condition(lambda: stack.duplicate_observations() >= 1, 'broker redelivery')
            stack.enable_receipt_delivery()
            stack.await_gateway_state('stored')
            self.assertEqual(stack.database_counts(), {'messages': 1, 'samples': 1, 'facts': 1, 'receipts': 1})

    def test_publisher_crash_before_and_after_publish_recovers_same_receipt(self):
        for fault in ('before_publish', 'after_publish'):
            with self.subTest(fault=fault), self.stack() as stack:
                stack.start(publisher_fault=fault)
                stack.enqueue_one(stack.observation(fault))
                stack.await_process_exit('publisher', 72)
                original = stack.receipt(fault)
                self.assertEqual(stack.delivery_state(fault), 'inflight')
                stack.enable_receipt_delivery()
                stack.await_gateway_state('stored', fault)
                stack.await_condition(lambda: stack.delivery_state(fault) == 'broker_confirmed', 'outbox recovery')
                self.assertEqual(stack.receipt(fault), original)
                self.assertEqual(stack.message_sample_count(fault), 1)

    def test_lost_broker_sessions_recover_from_local_exact_bytes(self):
        with self.stack() as stack:
            stack.start(receipt_delivery=False)
            obs = stack.enqueue_one()
            stack.await_counts(messages=1, samples=1)
            stack.await_gateway_state('awaiting_receipt')
            stack.restart_broker()
            stack.enable_receipt_delivery()
            stack.await_gateway_state('stored')
            self.assertEqual(stack.fact_payload(), obs.payload)
            self.assertEqual(stack.database_counts()['samples'], 1)

    def test_queue_capacity_refusal_preserves_first_payload_and_recovers_it(self):
        with self.stack(max_messages=1) as stack:
            stack.start(receipt_delivery=False)
            original = stack.enqueue_one()
            stack.await_counts(messages=1, samples=1)
            rejected = stack.enqueue_one(stack.observation('SECOND'), expect_error=True)
            self.assertEqual(rejected['code'], 'ingest.spool_capacity_exhausted')
            self.assertEqual(stack.gateway_record()['payload'], original.payload)
            self.assertIsNone(stack.gateway_record('SECOND'))
            stack.enable_receipt_delivery()
            stack.await_gateway_state('stored')

    def test_permission_revocation_blocks_replay_and_raw_access(self):
        with self.stack(http=True) as stack:
            stack.start(receipt_delivery=False)
            stack.create_rejected_case()
            operation = stack.close_case_as_maintenance()
            stack.revoke_permission('ingest.manage')
            self.assertEqual(stack.repeat_operation(operation)['status'], 403)
            stack.revoke_permission('ingest.raw.read')
            self.assertEqual(stack.preview_as_maintenance()['status'], 403)
            self.assertEqual(stack.audit_actions(), ['case.close'])

    def test_old_queue_runtime_cannot_open_or_relabel_v2_evidence(self):
        with self.stack() as stack:
            stack.start()
            obs = stack.enqueue_one()
            stack.await_gateway_state('stored')
            stack.stop_process('gateway')
            result = stack.attempt_legacy_open()
            self.assertEqual(result['code'], 'ingest.spool_version_incompatible')
            self.assertEqual(stack.gateway_record()['state'], 'stored')
            self.assertEqual(stack.gateway_record()['payload'], obs.payload)
