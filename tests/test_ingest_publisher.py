"""Publisher boundary races; real Broker proof is a separate integration gate."""
from dataclasses import replace
import importlib
import threading
import time
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4

from backend.gateway.mqtt_uplink import MqttConfig
from backend.ingest_reliability.errors import ReliabilityError
from backend.ingest_reliability.outbox import DeliveryLease
from tests.ingest_vectors import observation, receipt_for
from tests.ingest_support import IngestDatabaseCase


def lease_fixture():
    receipt = replace(receipt_for(observation('M-' + uuid4().hex)), receipt_id=str(uuid4()))
    return DeliveryLease(UUID(receipt.receipt_id), uuid4(), 1, receipt.topic('oilfield'), receipt.to_bytes(), 1)


class MemoryOutboxBoundary:
    """Only replaces the already independently verified PostgreSQL boundary."""
    def __init__(self, count=1):
        self.pending = [lease_fixture() for _ in range(count)]
        self.active, self.delivered, self.failures, self.events = {}, [], [], []
        self.finish_error = None
        self.heartbeat_error = None
        self.finish_stale = False

    def claim(self, *, limit=20, lease_seconds=30):
        self.events.append(('claim', limit, lease_seconds))
        rows, self.pending = self.pending[:limit], self.pending[limit:]
        self.active.update({row.token: row for row in rows})
        return rows

    def finish(self, lease, *, success, error_code=None):
        self.events.append(('finish', success, error_code))
        if self.finish_error:
            raise self.finish_error
        if self.finish_stale or lease.token not in self.active:
            return False
        self.active.pop(lease.token)
        if success:
            self.delivered.append(lease)
        else:
            self.failures.append((lease, error_code))
        return True

    def heartbeat(self, *, success=None, error_code=None):
        self.events.append(('heartbeat', success, error_code))
        if self.heartbeat_error:
            raise self.heartbeat_error


class PahoBoundary:
    """No sockets: delivers precise callback timings/codes to the real worker."""
    def __init__(self, mode='early', *, connect_error=None, connack=0, hook=None):
        self.mode, self.connect_error, self.connack, self.hook = mode, connect_error, connack, hook
        self.publications, self.threads = [], []
        self.auth, self.tls = None, None
        self.connected = self.looping = False
        self.stopped = False
        self.mid = 7

    def username_pw_set(self, username, password):
        self.auth = username, password

    def tls_set(self, **kwargs):
        self.tls = kwargs

    def connect(self, *_args, **_kwargs):
        if self.connect_error:
            raise self.connect_error
        self.connected = True
        return 0

    def loop_start(self):
        self.looping = True
        if self.connack is not None:
            self.on_connect(self, None, None, SimpleNamespace(value=self.connack, is_failure=self.connack >= 128), None)

    def subscribe(self, *_args, **_kwargs):
        raise AssertionError('Receipt publisher must not subscribe with its write-only identity')

    def publish(self, topic, payload, *, qos, retain):
        self.publications.append((topic, payload, qos, retain))
        if self.hook:
            self.hook(self)
        if self.mode == 'raise':
            raise OSError('private-password private-payload')
        if self.mode == 'disconnect':
            self.on_disconnect(self, None, None, SimpleNamespace(value=128, is_failure=True), None)
        if self.mode in ('early', 'no_subscribers', 'denied'):
            code = {'early': 0, 'no_subscribers': 16, 'denied': 135}[self.mode]
            self.on_publish(self, None, self.mid, SimpleNamespace(value=code, is_failure=code >= 128), None)
        if self.mode == 'delayed':
            callback = self.on_publish
            timer = threading.Timer(.005, lambda: callback(self, None, self.mid, SimpleNamespace(value=0, is_failure=False), None))
            timer.daemon = True
            self.threads.append(timer)
            timer.start()
        if self.mode == 'storm':
            for mid in range(1000, 2000):
                self.on_publish(self, None, mid, SimpleNamespace(value=0, is_failure=False), None)
        return SimpleNamespace(mid=self.mid, rc=4 if self.mode == 'bad_rc' else 0)

    def disconnect(self):
        self.connected = False

    def loop_stop(self):
        self.looping = False
        self.stopped = True
        for timer in self.threads:
            timer.join(.5)


class PublisherUnitTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('backend.ingest_reliability.publisher')
        self.config = MqttConfig('127.0.0.1', 1883, 'receipt-test', username='receipts', password='test-secret',
                                 connect_timeout_seconds=.08, publish_timeout_seconds=.08)

    def worker(self, repository=None, client=None, **kwargs):
        repository = repository if repository is not None else MemoryOutboxBoundary()
        client = client if client is not None else PahoBoundary()
        worker = self.module.ReceiptPublisher(repository, self.config, client_factory=lambda _config: client, **kwargs)
        self.addCleanup(worker.close)
        return worker, repository, client

    def test_synchronous_puback_before_publish_returns_is_not_lost(self):
        worker, repository, client = self.worker()
        expected = repository.pending[0]
        self.assertEqual(worker.run_once(), {'claimed': 1, 'published': 1, 'failed': 0})
        self.assertEqual(repository.delivered, [expected])
        self.assertEqual(client.publications, [(expected.topic, expected.payload, 1, False)])
        self.assertEqual(client.auth, ('receipts', 'test-secret'))

    def test_delayed_successful_puback_is_required_before_finish(self):
        worker, repository, client = self.worker(client=PahoBoundary('delayed'))
        self.assertEqual(worker.run_once()['published'], 1)
        self.assertEqual(len(repository.delivered), 1)
        self.assertEqual(repository.failures, [])

    def test_no_subscribers_and_broker_rejection_remain_retryable(self):
        for mode in ('no_subscribers', 'denied', 'bad_rc', 'missing', 'disconnect', 'raise', 'storm'):
            with self.subTest(mode=mode):
                worker, repository, _ = self.worker(client=PahoBoundary(mode))
                result = worker.run_once()
                self.assertEqual((result['published'], result['failed']), (0, 1))
                self.assertEqual(repository.delivered, [])
                self.assertEqual(len(repository.failures), 1)
                code = repository.failures[0][1]
                self.assertRegex(code, r'^[a-z][a-z0-9_.]{0,95}$')
                self.assertNotIn('private', code)

    def test_initial_connect_failure_and_denied_connack_never_claim_rows(self):
        clients = [PahoBoundary(connect_error=OSError('private-password')), PahoBoundary(connack=135), PahoBoundary(connack=None)]
        for client in clients:
            with self.subTest(connack=client.connack, connect_error=bool(client.connect_error)):
                worker, repository, _ = self.worker(client=client)
                result = worker.run_once()
                self.assertEqual((result['claimed'], result['published']), (0, 0))
                self.assertEqual(len(repository.pending), 1)
                self.assertFalse(any(event[0] == 'claim' for event in repository.events))
                self.assertTrue(any(event[0] == 'heartbeat' and event[2] for event in repository.events))
                self.assertNotIn('private-password', str(repository.events))

    def test_finish_failure_after_puback_is_not_reported_as_durable_success(self):
        repository = MemoryOutboxBoundary()
        repository.finish_error = ReliabilityError('ingest.storage_unavailable', 'storage')
        worker, repository, _ = self.worker(repository=repository)
        with self.assertRaises(ReliabilityError):
            worker.run_once()
        self.assertEqual(len(repository.active), 1)
        self.assertEqual(repository.delivered, [])
        self.assertEqual(len([event for event in repository.events if event[0] == 'finish']), 1,
                         'Do not replace an uncertain successful finish with a failure write')

    def test_lost_lease_is_not_counted_as_completed_delivery(self):
        repository = MemoryOutboxBoundary()
        repository.finish_stale = True
        worker, repository, _ = self.worker(repository=repository)
        self.assertEqual(worker.run_once()['published'], 0)
        self.assertEqual(repository.delivered, [])

    def test_claims_one_receipt_at_a_time_with_configured_run_limit(self):
        worker, repository, _ = self.worker(repository=MemoryOutboxBoundary(5), batch_size=2, lease_seconds=5)
        result = worker.run_once()
        self.assertEqual(result, {'claimed': 2, 'published': 2, 'failed': 0})
        self.assertEqual(len(repository.pending), 3)
        self.assertEqual([event[1:] for event in repository.events if event[0] == 'claim'], [(1, 5), (1, 5)])

    def test_failed_first_publish_does_not_claim_the_remaining_batch(self):
        worker, repository, _ = self.worker(repository=MemoryOutboxBoundary(3), client=PahoBoundary('disconnect'))
        result = worker.run_once()
        self.assertEqual(result['claimed'], 1)
        self.assertEqual(len(repository.pending), 2)

    def test_cross_connection_late_callback_with_reused_mid_cannot_confirm_new_receipt(self):
        repository = MemoryOutboxBoundary()
        first, second = PahoBoundary(), PahoBoundary('missing')
        clients = iter((first, second))
        worker = self.module.ReceiptPublisher(repository, self.config, client_factory=lambda _config: next(clients))
        self.addCleanup(worker.close)
        self.assertEqual(worker.run_once()['published'], 1)
        stale_callback = first.on_publish
        repository.pending.append(lease_fixture())
        second.hook = lambda _client: stale_callback(first, None, 7, SimpleNamespace(value=0, is_failure=False), None)
        self.assertEqual(worker.run_once()['published'], 0)
        self.assertEqual(len(repository.delivered), 1)
        self.assertEqual(len(repository.failures), 1)

    def test_timeout_does_not_reuse_its_late_callback_on_a_new_attempt(self):
        repository = MemoryOutboxBoundary()
        first, second = PahoBoundary('missing'), PahoBoundary('missing')
        clients = iter((first, second))
        worker = self.module.ReceiptPublisher(repository, self.config, client_factory=lambda _config: next(clients))
        self.addCleanup(worker.close)
        self.assertEqual(worker.run_once()['published'], 0)
        stale_callback = first.on_publish
        repository.pending.append(repository.failures[0][0])
        second.hook = lambda _client: stale_callback(first, None, 7, SimpleNamespace(value=0, is_failure=False), None)
        self.assertEqual(worker.run_once()['published'], 0)
        self.assertEqual(repository.delivered, [])

    def test_close_during_wait_preserves_durable_lease_and_stops_network_loop(self):
        worker, repository, client = self.worker(client=PahoBoundary('missing'))
        client.hook = lambda _client: worker.close()
        started = time.monotonic()
        self.assertEqual(worker.run_once()['published'], 0)
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(len(repository.active), 1)
        self.assertFalse(any(event[0] == 'finish' for event in repository.events))
        self.assertFalse(client.looping)
        self.assertFalse(client.connected)

    def test_database_health_failure_stops_before_network_or_claim(self):
        repository = MemoryOutboxBoundary()
        repository.heartbeat_error = ReliabilityError('ingest.storage_unavailable', 'storage')
        worker, repository, client = self.worker(repository=repository)
        with self.assertRaises(ReliabilityError):
            worker.run_once()
        self.assertFalse(client.connected)
        self.assertEqual(len(repository.pending), 1)


    def test_remote_tls_preserves_certificate_and_hostname_verification(self):
        import ssl
        self.config = replace(self.config, host='mqtt.example.test', ca_cert='trusted-ca.pem',
                              client_cert='client.pem', client_key='client.key')
        worker, _, client = self.worker()
        worker.run_once()
        self.assertEqual(client.tls['cert_reqs'], ssl.CERT_REQUIRED)
        self.assertEqual(client.tls['ca_certs'], 'trusted-ca.pem')
        self.assertEqual(client.tls['tls_version'], ssl.PROTOCOL_TLS_CLIENT)

    def test_constructor_rejects_invalid_bounds_without_creating_a_network_client(self):
        def forbidden(_config):
            self.fail('Constructor must not create/connect a network client')
        self.module.ReceiptPublisher(MemoryOutboxBoundary(), self.config, client_factory=forbidden)
        for kwargs in ({'batch_size': True}, {'batch_size': 0}, {'batch_size': 201}, {'lease_seconds': 0}, {'lease_seconds': 301}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, ReliabilityError)):
                self.module.ReceiptPublisher(MemoryOutboxBoundary(), self.config, client_factory=forbidden, **kwargs)
        with self.assertRaises((ValueError, ReliabilityError)):
            self.module.ReceiptPublisher(MemoryOutboxBoundary(), replace(self.config, publish_timeout_seconds=30),
                                         client_factory=forbidden, lease_seconds=30)

    def test_closed_worker_cannot_claim_new_receipts(self):
        worker, repository, client = self.worker()
        worker.close()
        with self.assertRaises(ReliabilityError):
            worker.run_once()
        self.assertEqual(len(repository.pending), 1)
        self.assertFalse(client.connected)

    def test_close_during_connect_cannot_start_an_orphan_network_loop(self):
        entered, release = threading.Event(), threading.Event()
        class DelayedConnect(PahoBoundary):
            def connect(self, *_args, **_kwargs):
                entered.set()
                release.wait(1)
                self.connected = True
                return 0
        client = DelayedConnect()
        worker, repository, _ = self.worker(client=client)
        errors = []
        def run():
            try:
                worker.run_once()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            worker.close()
        finally:
            release.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(client.looping, 'A completed late connect must not start an orphan MQTT loop')
        self.assertFalse(client.connected)
        self.assertEqual(len(repository.pending), 1)


class PublisherDatabaseTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        self.module = importlib.import_module('backend.ingest_reliability.publisher')
        self.config = MqttConfig('127.0.0.1', 1883, 'receipt-db-test', username='receipts', password='test-secret',
                                 connect_timeout_seconds=.08, publish_timeout_seconds=.08)
        self.result = self.repository().ingest(observation())

    def worker(self, mode='early', *, outbox=None, hook=None):
        client = PahoBoundary(mode, hook=hook)
        repository = outbox if outbox is not None else self.outbox()
        worker = self.module.ReceiptPublisher(repository, self.config, client_factory=lambda _config: client)
        self.addCleanup(worker.close)
        return worker, repository, client

    def state(self):
        with self.connect('publisher') as connection:
            cursor = connection.execute('SELECT * FROM ingest_reliability.deliveries WHERE receipt_id=%s', (self.result.receipt_id,))
            return dict(zip([field.name for field in cursor.description], cursor.fetchone()))

    def test_broker_ack_is_persisted_without_inventing_gateway_confirmation(self):
        worker, repository, client = self.worker()
        body = self.scalar('SELECT payload FROM ingest_reliability.receipts', role='publisher')
        result = worker.run_once()
        self.assertEqual(result, {'claimed': 1, 'published': 1, 'failed': 0})
        state = self.state()
        self.assertEqual((state['sent_generation'], state['requested_generation']), (1, 1))
        self.assertIsNotNone(state['broker_acked_at'])
        self.assertIsNone(state['lease_token'])
        self.assertEqual(client.publications[0][1], body)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.worker_health WHERE last_success IS NOT NULL',
                                     role='publisher'), 1)

    def test_no_subscriber_has_failure_code_and_retry_deadline_not_success(self):
        worker, _, _ = self.worker('no_subscribers')
        self.assertEqual(worker.run_once(), {'claimed': 1, 'published': 0, 'failed': 1})
        state = self.state()
        self.assertEqual(state['sent_generation'], 0)
        self.assertEqual(state['last_error_code'], 'mqtt.no_matching_subscribers')
        self.assertIsNone(state['broker_acked_at'])
        self.assertIsNone(state['lease_token'])
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.worker_health', role='publisher'), 1)
        self.assertEqual(self.scalar('SELECT error_code FROM ingest_reliability.worker_health', role='publisher'),
                         'mqtt.no_matching_subscribers')

    def test_network_publish_does_not_hold_the_delivery_database_lock(self):
        # This hook opens a real PostgreSQL connection before simulating PUBACK.
        # NOWAIT is the lock assertion; an 80 ms network-unit-test deadline must
        # not also constrain unrelated database connection setup/scheduling.
        self.config = replace(self.config, publish_timeout_seconds=2)
        observed = []
        def inspect(_client):
            with self.connect('publisher') as connection:
                row = connection.execute('SELECT sent_generation FROM ingest_reliability.deliveries '
                                         'WHERE receipt_id=%s FOR UPDATE NOWAIT', (self.result.receipt_id,)).fetchone()
                observed.append(row[0])
        worker, _, _ = self.worker(hook=inspect)
        self.assertEqual(worker.run_once()['published'], 1)
        self.assertEqual(observed, [0])

    def test_commit_uncertain_finish_keeps_one_persisted_result_without_fallback_failure_write(self):
        from backend.ingest_reliability.outbox import OutboxRepository
        from tests.test_ingest_repository import FaultConnection
        lose_reply = [False]
        def connect():
            return FaultConnection(self.connect('publisher'), lost_commit_response=lose_reply[0])
        outbox = OutboxRepository(connect)
        worker, _, _ = self.worker(outbox=outbox, hook=lambda _client: lose_reply.__setitem__(0, True))
        with self.assertRaises(ReliabilityError):
            worker.run_once()
        self.assertEqual(self.state()['sent_generation'], 1)
        self.assertIsNone(self.state()['last_error_code'])
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.receipts', role='publisher'), 1)

    def test_close_preserves_lease_until_another_process_recovers_it(self):
        from datetime import timedelta
        worker, _, client = self.worker('missing')
        client.hook = lambda _client: worker.close()
        self.assertEqual(worker.run_once()['published'], 0)
        state = self.state()
        self.assertIsNotNone(state['lease_token'])
        self.assertEqual(state['sent_generation'], 0)
        replacement = self.outbox().claim(now=state['lease_until'] + timedelta(seconds=1))[0]
        self.assertNotEqual(replacement.token, state['lease_token'])

    def test_heartbeat_updates_one_worker_record_and_does_not_claim_delivery(self):
        outbox = self.outbox()
        outbox.heartbeat()
        outbox.heartbeat(success=False, error_code='mqtt.connect_failed')
        outbox.heartbeat(success=True)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.worker_health', role='publisher'), 1)
        with self.connect('publisher') as connection:
            row = connection.execute('SELECT last_seen,last_success,last_failure,error_code FROM ingest_reliability.worker_health').fetchone()
        self.assertIsNotNone(row[1])
        self.assertIsNotNone(row[2])
        self.assertIsNone(row[3])
        self.assertIsNone(self.state()['lease_token'])

    def test_heartbeat_database_failure_is_not_reported_as_healthy(self):
        from backend.ingest_reliability.outbox import OutboxRepository
        import psycopg
        def broken():
            raise psycopg.OperationalError('private DSN text')
        with self.assertRaises(ReliabilityError) as caught:
            OutboxRepository(broken).heartbeat()
        self.assertNotIn('private DSN', str(caught.exception))

    def test_successful_heartbeat_does_not_erase_a_detected_clock_rollback(self):
        from datetime import timedelta
        outbox = self.outbox()
        with self.connect('publisher') as connection:
            now = connection.execute('SELECT clock_timestamp()').fetchone()[0]
            connection.execute('UPDATE ingest_reliability.capacity SET last_clock_seen=%s', (now + timedelta(hours=1),))
        outbox.claim(now=now)
        outbox.heartbeat(success=True, now=now)
        self.assertEqual(self.scalar('SELECT error_code FROM ingest_reliability.worker_health WHERE worker_id=%s',
                                     (outbox.worker_id,), role='publisher'), 'ingest.clock_rollback')

    def test_alive_heartbeat_does_not_claim_broker_health_or_clear_existing_failure(self):
        outbox = self.outbox()
        outbox.heartbeat()
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.worker_health', role='publisher'), 1)
        self.assertIsNone(self.scalar('SELECT last_success FROM ingest_reliability.worker_health', role='publisher'))
        outbox.heartbeat(success=False, error_code='mqtt.connect_failed')
        outbox.heartbeat()
        self.assertEqual(self.scalar('SELECT error_code FROM ingest_reliability.worker_health', role='publisher'), 'mqtt.connect_failed')


class PublisherHeartbeatInputTests(unittest.TestCase):
    def test_heartbeat_rejects_unsafe_values_before_database_access(self):
        from datetime import datetime
        from backend.ingest_reliability.outbox import OutboxRepository
        def forbidden():
            self.fail('Invalid heartbeat must not reach the database')
        repository = OutboxRepository(forbidden)
        for kwargs in ({'success': 1}, {'success': 'yes'}, {'error_code': 'secret: password'},
                       {'success': True, 'error_code': 'mqtt.error'}, {'error_code': 'mqtt.error'},
                       {'success': False, 'error_code': 'x' * 97}, {'now': datetime(2026, 1, 1)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ReliabilityError):
                repository.heartbeat(**kwargs)


class PublisherLoopTests(unittest.TestCase):
    def test_once_propagates_failed_delivery_status_and_closes_worker(self):
        from contextlib import redirect_stdout
        from io import StringIO
        module = importlib.import_module('backend.ingest_reliability.__main__')
        class Worker:
            closed = False
            def run_once(self):
                return {'claimed': 1, 'published': 0, 'failed': 1}
            def close(self):
                self.closed = True
        worker, output = Worker(), StringIO()
        with redirect_stdout(output):
            result = module.run_loop(worker, once=True, retry_max_seconds=300, health_stale_seconds=120)
        self.assertEqual(result, 1)
        self.assertTrue(worker.closed)
        self.assertIn('"published": 0', output.getvalue())

    def test_daemon_retries_storage_failure_with_bounded_wait_and_sanitized_output(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        module = importlib.import_module('backend.ingest_reliability.__main__')
        class Worker:
            attempts, closed = 0, False
            def run_once(self):
                self.attempts += 1
                if self.attempts < 10:
                    raise ReliabilityError('ingest.storage_unavailable', 'private-password private-payload')
                raise KeyboardInterrupt()
            def close(self):
                self.closed = True
        worker, output, waits = Worker(), StringIO(), []
        with redirect_stdout(output), patch.object(module.time, 'sleep', side_effect=waits.append):
            result = module.run_loop(worker, once=False, retry_max_seconds=3, health_stale_seconds=5)
        self.assertEqual(result, 0)
        self.assertTrue(worker.closed)
        self.assertEqual(len(waits), 9)
        self.assertTrue(all(0 < delay <= 2.5 for delay in waits))
        self.assertNotIn('private', output.getvalue())
        self.assertIn('ingest.storage_unavailable', output.getvalue())


class PublisherStartupTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        from pathlib import Path
        import socket
        import tempfile
        from tests.test_ingest_config import PG_SECTIONS, configuration
        temporary = tempfile.TemporaryDirectory(prefix='receipt-startup-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'center.toml'
        self.raw = configuration()
        reserved = socket.socket()
        reserved.bind(('127.0.0.1', 0))
        self.addCleanup(reserved.close)
        for section in ('mqtt', 'mqtt_receipt_publisher'):
            self.raw[section]['port'] = reserved.getsockname()[1]
            self.raw[section]['connect_timeout_seconds'] = .2
        self.raw['postgres']['database'] = self.settings['publisher']['dbname']
        for role, section in PG_SECTIONS.items():
            self.raw[section] = dict(self.raw_config['postgres_' + role])

    def run_cli(self, *flags):
        import os
        import subprocess
        import sys
        from tests.test_ingest_config import write_configuration
        write_configuration(self.path, self.raw)
        result = subprocess.run([sys.executable, '-m', 'backend.ingest_reliability', '--config', str(self.path), *flags],
                                env=os.environ | {'RECEIPT_MQTT': 'unused-test-receipt-secret'},
                                text=True, capture_output=True, timeout=20)
        self.assertTrue(all(settings['password'] not in result.stdout + result.stderr for settings in self.settings.values()))
        self.assertNotIn('unused-test-receipt-secret', result.stdout + result.stderr)
        return result

    def test_check_only_probes_publisher_role_without_network_or_health_writes(self):
        # Unused component secrets deliberately cannot be resolved.
        for section in ('postgres_reliable_ingest', 'postgres_ingest_reader', 'postgres_ingest_operator'):
            self.raw[section]['password_credential'] = 'deliberately-absent-other-component'
        result = self.run_cli('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"configuration_valid"', result.stdout)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.worker_health', role='publisher'), 0)

    def test_once_broker_failure_is_nonzero_without_claiming_or_losing_pending_receipt(self):
        self.repository().ingest(observation())
        result = self.run_cli('--once')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('"failed": 1', result.stdout)
        self.assertIn('"published": 0', result.stdout)
        self.assertEqual(self.scalar('SELECT sent_generation FROM ingest_reliability.deliveries', role='publisher'), 0)
        self.assertIsNone(self.scalar('SELECT lease_token FROM ingest_reliability.deliveries', role='publisher'))
        self.assertIsNotNone(self.scalar('SELECT last_failure FROM ingest_reliability.worker_health', role='publisher'))

    def test_privilege_drift_is_refused_without_runtime_repair(self):
        from psycopg import sql
        name = sql.Identifier(self.settings['publisher']['user'])
        with self.connect('migrator') as connection:
            connection.execute(sql.SQL('GRANT SELECT ON ingest_reliability.facts TO {}').format(name))
            connection.commit()
            try:
                result = self.run_cli('--check')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('ingest.privilege_unsafe', result.stdout)
                self.assertTrue(self.scalar("SELECT has_table_privilege(current_user,'ingest_reliability.facts','SELECT')", role='publisher'))
            finally:
                connection.execute(sql.SQL('REVOKE SELECT ON ingest_reliability.facts FROM {}').format(name))
                connection.commit()

    def test_capacity_mismatch_does_not_rewrite_database_limits(self):
        from tests.ingest_support import LIMITS
        self.raw['reliability']['total_bytes'] += 1
        result = self.run_cli('--check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ingest.capacity_mismatch', result.stdout)
        self.assertEqual(self.scalar('SELECT total_bytes FROM ingest_reliability.capacity', role='publisher'), LIMITS.total_bytes)

    def test_disabled_is_explicit_and_does_not_resolve_missing_credentials(self):
        self.raw = {'reliability': {'enabled': False}}
        result = self.run_cli('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"publisher_disabled"', result.stdout)
        self.assertNotIn('"configuration_valid"', result.stdout)

    def test_runtime_migration_is_not_supported_and_does_not_change_schema_history(self):
        before = self.scalar('SELECT count(*) FROM ingest_reliability.schema_migrations', role='publisher')
        result = self.run_cli('--migrate')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.schema_migrations', role='publisher'), before)

    def test_check_rejects_a_publish_timeout_that_can_outlive_its_lease(self):
        self.raw['mqtt_receipt_publisher']['publish_timeout_seconds'] = 30
        self.raw['reliability']['lease_seconds'] = 30
        result = self.run_cli('--check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ingest.publisher_config_invalid', result.stdout)
