"""Strict management requests and real audited incident transactions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import timedelta
from hashlib import sha256
import importlib
import json
import threading
import unittest
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_support import IngestDatabaseCase
from tests.ingest_vectors import observation
from tests.test_ingest_repository import FaultConnection


class ManagementContractTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('backend.ingest_reliability.management_contracts')
        self.actor = self.module.Actor('platform', '17', 'operator', '现场运维', 'maintenance')
        self.target = UUID('93f5462c-790a-4478-a672-d0e76f103bed')
        self.body = {'reason': '核查格式', 'idempotency_key': 'bd00de4c-a4c4-4b36-9629-333f98d74e71', 'expected_version': '1'}

    def validate(self, *, action='case.close', target=None, actor=None, body=None, route=None):
        target = self.target if target is None else target
        route = route or f'/api/ingest-reliability/cases/{target}/{action.split(".")[-1]}'
        return self.module.validate_management(action, target, actor or self.actor,
                                               self.body if body is None else body, route=route)

    def test_valid_command_captures_exact_reason_identity_and_bounded_version(self):
        value = self.validate(body=self.body | {'reason': '  已核查设备格式  ', 'expected_version': '9223372036854775807'})
        self.assertEqual(value.reason, '  已核查设备格式  ')
        self.assertEqual(value.expected_version, 9223372036854775807)
        self.assertEqual(value.key, UUID(self.body['idempotency_key']))
        self.assertEqual(value.target, self.target)
        self.assertEqual(asdict(value.actor), {'domain': 'platform', 'user_id': '17', 'username': 'operator',
                                              'display_name': '现场运维', 'role_code': 'maintenance'})

    def test_unknown_fields_and_actor_injection_are_rejected(self):
        for body in [[], None, {}, self.body | {'actor': {'user_id': 'admin'}}, self.body | {'unknown': 'x'}]:
            with self.subTest(body=body), self.assertRaises(ReliabilityError) as caught:
                self.module.validate_management('case.close', self.target, self.actor, body,
                                                 route=f'/api/ingest-reliability/cases/{self.target}/close')
            self.assertEqual(caught.exception.status, 422)

    def test_version_rejects_numbers_booleans_leading_zero_and_unbounded_input(self):
        for version in (True, 1, 1.0, '0', '-1', '01', '1.0', '1e2', ' 1', '9' * 5000, '9223372036854775808'):
            with self.subTest(version=str(version)[:25]), self.assertRaises(ReliabilityError):
                self.validate(body=self.body | {'expected_version': version})

    def test_uuid_and_target_cannot_be_coerced_or_arbitrary_text(self):
        for key in (None, True, '1', str(uuid4()).replace('-', ''), ' ' + str(uuid4()), uuid4()):
            with self.subTest(key=key), self.assertRaises(ReliabilityError):
                self.validate(body=self.body | {'idempotency_key': key})
        with self.assertRaises(ReliabilityError):
            self.validate(target=str(self.target))

    def test_reason_is_bounded_text_not_blank_control_or_nested_data(self):
        for reason in ('x', '   ', '\x00bad', 'bad\r\nheader', 'bad\x80', '\ud800x', 'x' * 501, 12, ['note']):
            with self.subTest(reason=repr(reason)[:35]), self.assertRaises(ReliabilityError):
                self.validate(body=self.body | {'reason': reason})

    def test_actor_requires_all_bounded_real_strings(self):
        for field in ('domain', 'user_id', 'username', 'display_name', 'role_code'):
            for value in ('', ' ', True, 'x' * 129, 'x\x00', 'x\x80', '\ud800'):
                with self.subTest(field=field, value=repr(value)[:25]), self.assertRaises(ReliabilityError):
                    actor = self.module.Actor(**(asdict(self.actor) | {field: value}))
                    self.validate(actor=actor)

    def test_action_and_route_bind_target_without_alias_or_query(self):
        for action in ('case.delete', 'case.repeat_reopen', 'raw.preview', '', True):
            with self.subTest(action=action), self.assertRaises(ReliabilityError):
                self.validate(action=action, route=f'/api/ingest-reliability/cases/{self.target}/close')
        for route in (f'/api/ingest-reliability/cases/{uuid4()}/close',
                      f'/api/ingest-reliability/cases/{self.target}/close?ignored=1',
                      f'/api/ingest-reliability/cases/{self.target}/note', '/different/close'):
            with self.subTest(route=route), self.assertRaises(ReliabilityError):
                self.validate(route=route)

    def test_request_hash_stable_for_key_order_but_changes_for_reason_action_and_target(self):
        first = self.validate()
        reordered = self.validate(body=dict(reversed(list(self.body.items()))))
        self.assertEqual(first.request_hash, reordered.request_hash)
        self.assertRegex(first.request_hash, r'^[0-9a-f]{64}$')
        changed = [self.validate(body=self.body | {'reason': '另一核查意见'}), self.validate(action='case.note'),
                   self.validate(target=uuid4()), self.validate(body=self.body | {'expected_version': '2'})]
        for value in changed:
            self.assertNotEqual(value.request_hash, first.request_hash)
        self.body['reason'] = '修改原始对象'
        self.assertEqual(first.reason, '核查格式')

    def test_receipt_resend_has_its_own_route_and_rejects_case_alias(self):
        value = self.validate(action='receipt.resend', route=f'/api/ingest-reliability/receipts/{self.target}/resend')
        self.assertEqual(value.action, 'receipt.resend')
        with self.assertRaises(ReliabilityError):
            self.validate(action='receipt.resend')


class OperationInputTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('backend.ingest_reliability.operations')
        self.contracts = importlib.import_module('backend.ingest_reliability.management_contracts')
        self.actor = self.contracts.Actor('platform', '17', 'operator', '现场运维', 'maintenance')
        def forbidden():
            self.fail('Invalid operation must fail before opening a database connection')
        self.service = self.module.ReliabilityOperations(forbidden)

    def test_constructor_requires_factory_without_connecting(self):
        with self.assertRaises((ValueError, ReliabilityError)):
            self.module.ReliabilityOperations(None)

    def test_execute_requires_a_valid_server_command(self):
        for command in (None, {}, 'case.close', True):
            with self.subTest(command=command), self.assertRaises(ReliabilityError):
                self.service.execute(command)

    def test_raw_access_rejects_coerced_ids_actors_and_preview_modes(self):
        for identifier, actor, preview in [(str(uuid4()), self.actor, True), (uuid4(), None, True),
                                           (uuid4(), self.actor, 1), (uuid4(), self.actor, 'false')]:
            with self.subTest(preview=preview), self.assertRaises(ReliabilityError):
                self.service.read_raw(identifier, actor, preview=preview)


class IncidentOperationsTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        self.contracts = importlib.import_module('backend.ingest_reliability.management_contracts')
        self.actor = self.contracts.Actor('platform', '17', 'operator', '现场运维', 'maintenance')
        self.obs = replace(observation('INCIDENT'), retain=True)
        self.fact = self.repository().ingest(self.obs)
        self.case_id = self.scalar('SELECT case_id FROM ingest_reliability.cases WHERE fact_id=%s', (self.fact.fact_id,), role='operator')

    def command(self, action='case.close', *, reason='已核查设备数据格式', key=None, version=None, actor=None, target=None):
        target = target or (self.fact.receipt_id if action == 'receipt.resend' else self.case_id)
        if version is None:
            table, column = ('deliveries', 'receipt_id') if action == 'receipt.resend' else ('cases', 'case_id')
            version = self.scalar(f'SELECT version FROM ingest_reliability.{table} WHERE {column}=%s', (target,), role='operator')
        resource = 'receipts' if action == 'receipt.resend' else 'cases'
        return self.contracts.validate_management(action, target, actor or self.actor,
                    {'reason': reason, 'idempotency_key': str(key or uuid4()), 'expected_version': str(version)},
                    route=f'/api/ingest-reliability/{resource}/{target}/{action.split(".")[-1]}')

    @contextmanager
    def reject_audit(self):
        with self.connect('migrator') as conn:
            conn.execute("CREATE FUNCTION ingest_reliability.test_reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                         "BEGIN RAISE EXCEPTION 'injected audit storage failure'; END $$")
            conn.execute('CREATE TRIGGER test_reject_audit BEFORE INSERT ON ingest_reliability.audit_events '
                         'FOR EACH ROW EXECUTE FUNCTION ingest_reliability.test_reject_audit()')
        try:
            yield
        finally:
            with self.connect('migrator') as conn:
                conn.execute('DROP TRIGGER test_reject_audit ON ingest_reliability.audit_events')
                conn.execute('DROP FUNCTION ingest_reliability.test_reject_audit()')

    def test_same_operation_key_does_not_double_audit(self):
        command = self.command()
        first = self.operations().execute(command)
        second = self.operations().execute(command)
        self.assertEqual(first, {'entity': {'id': str(self.case_id), 'version': '2', 'state': 'closed'}, 'replayed': False})
        self.assertEqual(first['entity'], second['entity'])
        self.assertTrue(second['replayed'])
        for table in ('audit_events', 'operation_receipts'):
            self.assertEqual(self.scalar(f'SELECT count(*) FROM ingest_reliability.{table}', role='migrator'), 1)
        self.assertEqual(self.scalar('SELECT status FROM ingest_reliability.facts WHERE fact_id=%s', (self.fact.fact_id,), role='migrator'), 'quarantined')
        self.assertEqual(self.scalar('SELECT count(*) FROM public.telemetry_samples', role='migrator'), 0)

    def test_changed_body_under_same_key_conflicts_without_second_mutation(self):
        command = self.command()
        self.operations().execute(command)
        changed = self.command(reason='改变处理意见', key=command.key, version=1)
        with self.assertRaises(ReliabilityError) as caught:
            self.operations().execute(changed)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_events', role='migrator'), 1)

    def test_replay_is_scoped_by_actor_domain_and_id(self):
        key = uuid4()
        first = self.operations().execute(self.command('case.note', key=key))
        second = self.operations().execute(self.command('case.note', key=key, actor=replace(self.actor, domain='another-platform')))
        self.assertFalse(first['replayed'])
        self.assertFalse(second['replayed'])
        self.assertEqual(second['entity']['version'], '3')
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.operation_receipts', role='migrator'), 2)

    def test_audit_rejection_rolls_back_case_and_idempotency_result(self):
        with self.reject_audit(), self.assertRaises(ReliabilityError):
            self.operations().execute(self.command())
        self.assertEqual(self.scalar('SELECT state FROM ingest_reliability.cases WHERE case_id=%s', (self.case_id,), role='operator'), 'open')
        self.assertEqual(self.scalar('SELECT version FROM ingest_reliability.cases WHERE case_id=%s', (self.case_id,), role='operator'), 1)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.operation_receipts', role='operator'), 0)

    def test_each_legal_transition_changes_version_and_invalid_edges_leave_no_audit(self):
        for action, state, version in [('case.ack', 'acknowledged', '2'), ('case.note', 'acknowledged', '3'),
                                       ('case.close', 'closed', '4'), ('case.note', 'closed', '5'), ('case.reopen', 'open', '6')]:
            result = self.operations().execute(self.command(action))
            self.assertEqual((result['entity']['state'], result['entity']['version']), (state, version))
        with self.assertRaises(ReliabilityError) as caught:
            self.operations().execute(self.command('case.reopen'))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_events', role='migrator'), 5)

    def test_recurrent_closed_input_keeps_immutable_rejection_and_system_actor(self):
        result = self.operations().execute(self.command())
        self.assertIsNotNone(result, 'The close transaction must return its committed result')
        self.repository().ingest(replace(self.obs, received_at=self.obs.received_at + timedelta(seconds=1), dup=True))
        self.assertEqual(self.scalar('SELECT state FROM ingest_reliability.cases WHERE case_id=%s', (self.case_id,), role='operator'), 'open')
        self.assertEqual(self.scalar("SELECT actor->>'domain' FROM ingest_reliability.audit_events WHERE action='case.repeat_reopen'", role='migrator'), 'system')
        self.assertEqual(self.scalar('SELECT status FROM ingest_reliability.receipts WHERE receipt_id=%s', (self.fact.receipt_id,), role='publisher'), 'quarantined')

    def test_concurrent_different_keys_with_same_version_have_one_winner(self):
        commands = [self.command(), self.command()]
        barrier = threading.Barrier(2)
        def run(command):
            barrier.wait(2)
            try:
                return self.operations().execute(command)
            except ReliabilityError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, commands))
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertIn(409, results)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_events', role='migrator'), 1)

    def test_concurrent_same_key_replays_one_committed_result(self):
        command = self.command()
        barrier = threading.Barrier(2)
        def run(_index):
            barrier.wait(2)
            return self.operations().execute(command)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(sorted(item['replayed'] for item in results), [False, True])
        self.assertEqual(results[0]['entity'], results[1]['entity'])
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_events', role='migrator'), 1)

    def test_lost_commit_response_retries_original_key_without_duplicate_change(self):
        module = importlib.import_module('backend.ingest_reliability.operations')
        service = module.ReliabilityOperations(lambda: FaultConnection(self.connect('operator'), lost_commit_response=True))
        command = self.command()
        with self.assertRaises(ReliabilityError):
            service.execute(command)
        replayed = self.operations().execute(command)
        self.assertTrue(replayed['replayed'])
        self.assertEqual(replayed['entity']['version'], '2')
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_events', role='migrator'), 1)

    def test_resend_preserves_receipt_bytes_and_active_lease_generation(self):
        before = bytes(self.scalar('SELECT payload FROM ingest_reliability.receipts WHERE receipt_id=%s', (self.fact.receipt_id,), role='publisher'))
        leases = self.outbox().claim()
        self.assertEqual(len(leases), 1)
        lease = leases[0]
        result = self.operations().execute(self.command('receipt.resend'))
        self.assertIsNotNone(result, 'A resend must return its audited result')
        self.assertEqual(result['entity']['state'], 'pending')
        self.assertEqual(self.scalar('SELECT requested_generation FROM ingest_reliability.deliveries WHERE receipt_id=%s', (self.fact.receipt_id,), role='operator'), 2)
        self.assertEqual(bytes(self.scalar('SELECT payload FROM ingest_reliability.receipts WHERE receipt_id=%s', (self.fact.receipt_id,), role='publisher')), before)
        self.assertTrue(self.outbox().finish(lease, success=True))
        self.assertEqual(self.scalar('SELECT requested_generation-sent_generation FROM ingest_reliability.deliveries WHERE receipt_id=%s', (self.fact.receipt_id,), role='operator'), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.audit_events WHERE action='receipt.resend'", role='migrator'), 1)

    def test_resend_audit_failure_keeps_previous_delivery_request(self):
        leases = self.outbox().claim()
        self.assertEqual(len(leases), 1)
        lease = leases[0]
        with self.reject_audit(), self.assertRaises(ReliabilityError):
            self.operations().execute(self.command('receipt.resend'))
        self.assertEqual(self.scalar('SELECT requested_generation FROM ingest_reliability.deliveries WHERE receipt_id=%s', (self.fact.receipt_id,), role='operator'), 1)
        self.assertEqual(self.scalar('SELECT lease_token FROM ingest_reliability.deliveries WHERE receipt_id=%s', (self.fact.receipt_id,), role='operator'), lease.token)

    def test_raw_preview_and_download_return_only_after_access_audit(self):
        payload = b'<script>not executed</script>' + '原文'.encode() * 1000 + b'\xff'
        fact = self.repository().ingest(replace(observation('BINARY'), payload=payload))
        preview = self.operations().read_raw(fact.fact_id, self.actor, preview=True)
        self.assertEqual(preview['text'], payload[:4096].decode('utf-8', errors='replace'))
        self.assertIsNone(preview['raw_bytes'])
        self.assertTrue(preview['truncated'])
        self.assertEqual(preview['payload_bytes'], str(len(payload)))
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.audit_events WHERE action='raw.preview'", role='migrator'), 1)
        download = self.operations().read_raw(fact.fact_id, self.actor, preview=False)
        self.assertEqual(download['raw_bytes'], payload)
        self.assertIsNone(download['text'])
        self.assertFalse(download['truncated'])
        self.assertEqual(download['payload_sha256'], sha256(payload).hexdigest())
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.audit_events WHERE action='raw.download'", role='migrator'), 1)

    def test_raw_audit_failure_or_lost_commit_response_returns_no_raw_data(self):
        with self.reject_audit(), self.assertRaises(ReliabilityError):
            self.operations().read_raw(self.fact.fact_id, self.actor, preview=False)
        module = importlib.import_module('backend.ingest_reliability.operations')
        service = module.ReliabilityOperations(lambda: FaultConnection(self.connect('operator'), lost_commit_response=True))
        with self.assertRaises(ReliabilityError):
            service.read_raw(self.fact.fact_id, self.actor, preview=False)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.audit_events WHERE action='raw.download'", role='migrator'), 1)

    def test_reader_and_operator_have_no_direct_raw_select(self):
        for role in ('reader', 'operator'):
            with self.subTest(role=role), self.connect(role) as conn, self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute('SELECT raw_payload FROM ingest_reliability.facts')

    def test_missing_target_and_version_exhaustion_leave_no_operation_result(self):
        with self.assertRaises(ReliabilityError) as caught:
            self.operations().execute(self.command(target=uuid4(), version=1))
        self.assertEqual(caught.exception.status, 404)
        with self.connect('migrator') as conn:
            conn.execute('UPDATE ingest_reliability.cases SET version=9223372036854775807 WHERE case_id=%s', (self.case_id,))
        with self.assertRaises(ReliabilityError) as caught:
            self.operations().execute(self.command())
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.operation_receipts', role='operator'), 0)

    def test_raw_function_is_operator_only_and_unknown_fact_returns_no_content(self):
        for role in ('reader', 'publisher', 'ingestor'):
            with self.subTest(role=role), self.connect(role) as conn, self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute('SELECT * FROM ingest_reliability.audited_raw(%s,%s,%s)',
                             (self.fact.fact_id, Jsonb(asdict(self.actor)), True))
        with self.assertRaises(ReliabilityError) as caught:
            self.operations().read_raw(uuid4(), self.actor, preview=False)
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_events', role='migrator'), 0)

    def test_raw_access_function_is_explicitly_available_without_broad_raw_select(self):
        function = self.scalar("SELECT to_regprocedure('ingest_reliability.audited_raw(uuid,jsonb,boolean)')", role='operator')
        self.assertIsNotNone(function, 'The narrow audited access path must be explicitly migrated')
        self.assertFalse(self.scalar("SELECT has_column_privilege(current_user,'ingest_reliability.facts','raw_payload','SELECT')", role='operator'))

    def test_raw_function_validates_actor_and_commits_audit_with_exact_preview_bytes(self):
        for actor in (None, {}, asdict(self.actor) | {'unknown': 'x'}, asdict(self.actor) | {'user_id': True}):
            with self.subTest(actor=actor), self.assertRaises(psycopg.Error), self.connect('operator') as conn:
                conn.execute('SELECT * FROM ingest_reliability.audited_raw(%s,%s,%s)',
                             (self.fact.fact_id, Jsonb(actor), True))
        with self.connect('operator') as conn:
            row = conn.execute('SELECT fact_id,payload_sha256,payload_bytes,raw_bytes '
                               'FROM ingest_reliability.audited_raw(%s,%s,%s)',
                               (self.fact.fact_id, Jsonb(asdict(self.actor)), True)).fetchone()
        self.assertEqual(row, (self.fact.fact_id, sha256(self.obs.payload).hexdigest(), len(self.obs.payload), self.obs.payload))
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.audit_events WHERE action='raw.preview'", role='migrator'), 1)

    def test_raw_function_public_execute_is_rejected_by_readonly_preflight(self):
        from backend.ingest_reliability.privileges import probe_role
        signature = 'ingest_reliability.audited_raw(uuid,jsonb,boolean)'
        with self.connect('migrator') as conn:
            conn.execute(f'GRANT EXECUTE ON FUNCTION {signature} TO PUBLIC')
            conn.commit()
            try:
                with self.assertRaises(ReliabilityError):
                    probe_role(lambda: self.connect('reader'), 'reader')
            finally:
                conn.execute(f'REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC')
                conn.commit()

    def test_raw_function_uses_owner_fixed_search_path_not_session_objects(self):
        with self.connect('operator') as conn:
            conn.execute('SET LOCAL search_path=public,pg_catalog')
            row = conn.execute('SELECT raw_bytes FROM ingest_reliability.audited_raw(%s,%s,%s)',
                               (self.fact.fact_id, Jsonb(asdict(self.actor)), False)).fetchone()
        self.assertEqual(bytes(row[0]), self.obs.payload)
        config = self.scalar("SELECT proconfig FROM pg_proc WHERE oid='ingest_reliability.audited_raw(uuid,jsonb,boolean)'::regprocedure", role='operator')
        self.assertIn('search_path=pg_catalog, pg_temp', config)

    def test_empty_raw_payload_has_exact_hash_size_and_audited_access(self):
        fact = self.repository().ingest(replace(observation('EMPTY'), payload=b''))
        result = self.operations().read_raw(fact.fact_id, self.actor, preview=False)
        self.assertEqual((result['payload_bytes'], result['raw_bytes'], result['payload_sha256']),
                         ('0', b'', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'))
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_events WHERE fact_id=%s', (fact.fact_id,), role='migrator'), 1)

    def test_resend_of_waiting_unleased_receipt_coalesces_without_bypassing_backoff(self):
        lease = self.outbox().claim()[0]
        self.outbox().finish(lease, success=False, error_code='mqtt.publish_failed')
        before = self.scalar('SELECT next_attempt_at FROM ingest_reliability.deliveries WHERE receipt_id=%s', (self.fact.receipt_id,), role='operator')
        self.operations().execute(self.command('receipt.resend'))
        self.assertEqual(self.scalar('SELECT next_attempt_at FROM ingest_reliability.deliveries WHERE receipt_id=%s', (self.fact.receipt_id,), role='operator'), before)
        self.assertEqual(self.scalar('SELECT requested_generation FROM ingest_reliability.deliveries WHERE receipt_id=%s', (self.fact.receipt_id,), role='operator'), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.audit_events WHERE action='receipt.resend'", role='migrator'), 1)

    def test_stale_resend_command_does_not_overwrite_publisher_progress(self):
        command = self.command('receipt.resend')
        lease = self.outbox().claim()[0]
        self.outbox().finish(lease, success=True)
        with self.assertRaises(ReliabilityError) as caught:
            self.operations().execute(command)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.scalar('SELECT requested_generation-sent_generation FROM ingest_reliability.deliveries WHERE receipt_id=%s', (self.fact.receipt_id,), role='operator'), 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_events', role='migrator'), 0)
