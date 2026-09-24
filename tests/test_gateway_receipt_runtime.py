"""Real durable queue with controlled MQTT callbacks and real worker threads."""
from contextlib import closing
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import io
import json
from pathlib import Path
import sqlite3
import ssl
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.gateway.mqtt_uplink import MqttConfig
from backend.gateway.config import GatewayConfig, GatewayConfigurationError, load_gateway_config
from backend.gateway.contracts import PointDefinition, TelemetrySample
from backend.gateway.poller import ModbusDevice, PollResult
from backend.gateway.reliable_spool import ReliableSpool, SpoolLimits
from backend.gateway.runtime import GatewayRuntime
from backend.gateway.spool import DurableSpool
from backend.gateway.spool_migrate import initialize_spool, read_spool_version
from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_vectors import observation, receipt_for
from tests.test_gateway_config import VALID_CONFIG
from tests.test_gateway_receipt_state import FaultConnection


def reason(value):
    return SimpleNamespace(value=value, is_failure=value >= 128)


class ReceiptClient:
    """A network boundary only; queue transactions and the worker are real."""
    def __init__(self, *, connack=0, suback=1, puback=0, hook=None, connect_gate=None, ack_hook=None):
        self.connack, self.suback, self.puback = connack, suback, puback
        self.hook, self.connect_gate, self.ack_hook = hook, connect_gate, ack_hook
        self.publications, self.subscriptions, self.acks = [], [], []
        self.connected = self.looping = self.manual_ack = False
        self.auth = self.tls = self.connect_options = None
        self.entered_connect = threading.Event()
        self.entered_subscribe = threading.Event()
        self.entered_publish = threading.Event()
        self.disconnected = threading.Event()
        self.mid = 7

    def username_pw_set(self, username, password):
        self.auth = (username, password)

    def tls_set(self, **kwargs):
        self.tls = kwargs

    def manual_ack_set(self, value):
        self.manual_ack = value

    def max_inflight_messages_set(self, _value):
        pass

    def max_queued_messages_set(self, _value):
        pass

    def connect(self, *args, **kwargs):
        self.connect_options = (args, kwargs)
        self.entered_connect.set()
        if self.connect_gate is not None:
            self.connect_gate.wait(3)
        self.connected = True
        return 0

    def loop_start(self):
        self.looping = True
        if self.connack is not None:
            self.on_connect(self, None, None, reason(self.connack), None)
        return 0

    def subscribe(self, topic, *, options):
        self.subscriptions.append((topic, options))
        self.entered_subscribe.set()
        if self.suback is not None:
            self.on_subscribe(self, None, 3, [reason(self.suback)], None)
        return (0, 3)

    def grant_subscription(self, value=1):
        self.on_subscribe(self, None, 3, [reason(value)], None)

    def publish(self, topic, payload, *, qos, retain):
        self.publications.append((topic, payload, qos, retain))
        self.entered_publish.set()
        if self.hook:
            self.hook(self, topic, payload)
        if self.puback is not None:
            self.on_publish(self, None, self.mid, reason(self.puback), None)
        return SimpleNamespace(mid=self.mid, rc=0)

    def receive(self, topic, payload, *, retain=False, qos=1, mid=91):
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload, retain=retain, qos=qos, mid=mid))

    def ack(self, mid, qos):
        if self.ack_hook:
            self.ack_hook(mid, qos)
        self.acks.append((mid, qos))
        return 0

    def disconnect(self):
        self.connected = False
        self.disconnected.set()
        return 0

    def loop_stop(self):
        self.looping = False
        return 0


class ReliableUplinkTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('backend.gateway.reliable_uplink')
        temporary = tempfile.TemporaryDirectory(prefix='gateway-receipts-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'spool.db'
        self.limits = SpoolLimits(100, 2 * 1024 * 1024, 1024 * 1024)
        initialize_spool(self.path, limits=self.limits)
        self.spool = ReliableSpool(self.path, limits=self.limits, base_backoff_seconds=.02, max_backoff_seconds=.05)
        self.addCleanup(self.spool.close)
        self.config = MqttConfig('127.0.0.1', 1883, 'GW', username='gateway', password='test-secret',
                                 connect_timeout_seconds=.15, publish_timeout_seconds=.15)

    def enqueue(self, identifier='M-1', mode='application'):
        obs = observation(identifier)
        self.spool.enqueue(identifier, obs.topic, obs.payload, confirmation_mode=mode)
        return obs

    def receipt(self, obs, **kwargs):
        return replace(receipt_for(obs, **kwargs), receipt_id=str(uuid4()))

    def worker(self, client=None, *, factory=None, **kwargs):
        if factory is None:
            client = client or ReceiptClient()
            factory = lambda _config: client
        worker = self.module.ReliableUplink(self.config, self.spool, gateway_id='GW', prefix='oilfield',
                                            client_factory=factory, **kwargs)
        self.addCleanup(worker.close)
        return worker

    def await_condition(self, condition, *, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            threading.Event().wait(.005)
        self.fail('Expected worker state was not observed before its bounded deadline')

    def test_constructor_and_wake_before_start_do_not_connect_or_consume_queue(self):
        self.enqueue()
        client = ReceiptClient()
        worker = self.worker(client)
        worker.wake()
        self.assertFalse(client.entered_connect.is_set())
        self.assertEqual(self.spool.counts()['pending'], 1)
        self.assertFalse(worker.snapshot()['running'])

    def test_subscribes_own_gateway_with_manual_ack_and_checks_early_suback(self):
        self.enqueue()
        client = ReceiptClient()
        worker = self.worker(client)
        worker.start()
        self.await_condition(lambda: self.spool.get('M-1')['state'] == 'awaiting_receipt')
        self.assertTrue(client.manual_ack)
        topic, options = client.subscriptions[0]
        self.assertEqual(topic, 'oilfield/v2/GW/+/ingest-ack')
        self.assertEqual(options.QoS, 1)
        self.assertTrue(options.retainAsPublished)
        self.assertEqual(self.spool.counts()['stored'], 0)
        self.assertEqual(client.publications[0][2:], (1, False))
        self.assertEqual(client.auth, ('gateway', 'test-secret'))

    def test_pending_subscription_does_not_publish_before_actual_grant(self):
        self.enqueue()
        client = ReceiptClient(suback=None)
        worker = self.worker(client)
        worker.start()
        self.assertTrue(client.entered_subscribe.wait(1))
        self.assertEqual(client.publications, [])
        self.assertEqual(self.spool.get('M-1')['state'], 'pending')
        client.grant_subscription()
        self.assertTrue(client.entered_publish.wait(1))

    def test_denied_or_qos_zero_subscription_never_publishes_telemetry(self):
        for grant in (0, 128):
            with self.subTest(grant=grant):
                client = ReceiptClient(suback=grant)
                worker = self.worker(client)
                worker.start()
                self.await_condition(lambda: worker.snapshot()['error_code'] == 'mqtt.subscription_rejected')
                worker.close()
                self.assertEqual(client.publications, [])

    def test_early_application_receipt_finalizes_before_puback_without_regression(self):
        obs = self.enqueue()
        receipt = self.receipt(obs)
        client = ReceiptClient(hook=lambda source, _topic, _body: source.receive(receipt.topic('oilfield'), receipt.to_bytes()))
        worker = self.worker(client)
        worker.start()
        self.await_condition(lambda: self.spool.get('M-1')['state'] == 'stored')
        self.await_condition(lambda: client.acks == [(91, 1)])
        self.assertIsNone(self.spool.get('M-1')['lease_token'])
        self.assertEqual(self.spool.get('M-1')['payload'], obs.payload)

    def test_incoming_receipt_is_acknowledged_only_after_a_separate_connection_sees_commit(self):
        obs = self.enqueue()
        receipt = self.receipt(obs)
        observed = []
        def check_commit(_mid, _qos):
            with closing(sqlite3.connect(self.path)) as connection:
                observed.append(connection.execute('SELECT state,receipt_id FROM uplink_queue_v2 WHERE message_id=?', ('M-1',)).fetchone())
        client = ReceiptClient(ack_hook=check_commit)
        worker = self.worker(client)
        worker.start()
        self.await_condition(lambda: self.spool.get('M-1')['state'] == 'awaiting_receipt')
        client.receive(receipt.topic('oilfield'), receipt.to_bytes())
        self.assertEqual(observed, [('stored', receipt.receipt_id)])
        self.assertEqual(client.acks, [(91, 1)])

    def test_retained_and_malformed_receipts_ack_only_after_durable_rejection_event(self):
        obs = self.enqueue()
        receipt = self.receipt(obs)
        counts = []
        def check_commit(_mid, _qos):
            with closing(sqlite3.connect(self.path)) as connection:
                counts.append(connection.execute('SELECT sum(occurrences) FROM spool_security_events').fetchone()[0])
        client = ReceiptClient(ack_hook=check_commit)
        worker = self.worker(client)
        worker.start()
        self.await_condition(lambda: self.spool.get('M-1')['state'] == 'awaiting_receipt')
        client.receive(receipt.topic('oilfield'), receipt.to_bytes(), retain=True)
        client.receive(receipt.topic('oilfield'), b'bad', mid=92)
        self.assertEqual(counts, [1, 2])
        self.assertEqual(client.acks, [(91, 1), (92, 1)])
        self.assertEqual(self.spool.counts()['stored'], 0)

    def test_local_storage_failure_disconnects_without_incoming_ack(self):
        obs = self.enqueue()
        receipt = self.receipt(obs)
        client = ReceiptClient()
        worker = self.worker(client)
        worker.start()
        self.await_condition(lambda: self.spool.get('M-1')['state'] == 'awaiting_receipt')
        with patch.object(self.spool, 'apply_receipt', side_effect=ReliabilityError('ingest.spool_write_unavailable', 'test failure')):
            client.receive(receipt.topic('oilfield'), receipt.to_bytes())
        self.assertEqual(client.acks, [])

        self.assertEqual(self.spool.get('M-1')['state'], 'awaiting_receipt')
        self.assertTrue(client.disconnected.is_set())
        self.assertEqual(worker.snapshot()['error_code'], 'ingest.spool_write_unavailable')

    def test_bad_puback_and_no_subscribers_do_not_complete_application_record(self):
        for identifier, code in [('M-1', 16), ('M-2', 135)]:
            with self.subTest(code=code):
                self.enqueue(identifier)
                client = ReceiptClient(puback=code)
                worker = self.worker(client)
                worker.start()
                self.assertTrue(client.entered_publish.wait(1))
                self.await_condition(lambda: bool(worker.snapshot()['error_code']))
                worker.close()
                self.assertEqual(self.spool.counts()['stored'], 0)
                self.assertEqual(self.spool.counts()['awaiting_receipt'], 0)

    def test_late_puback_and_receipt_from_old_connection_cannot_touch_new_connection(self):
        obs = self.enqueue()
        first, second = ReceiptClient(puback=None), ReceiptClient(puback=None)
        clients = iter([first, second])
        worker = self.worker(factory=lambda _config: next(clients))
        worker.start()
        self.assertTrue(first.entered_publish.wait(1))
        self.assertTrue(second.entered_subscribe.wait(2))
        # Wait for its fresh claim, which uses the same MQTT mid in this test.
        self.assertTrue(second.entered_publish.wait(2))
        current = self.spool.get('M-1')['lease_token']
        first.on_publish(first, None, 7, reason(0), None)
        receipt = self.receipt(obs)
        first.receive(receipt.topic('oilfield'), receipt.to_bytes())
        self.assertEqual(first.acks, [])
        self.assertEqual(self.spool.get('M-1')['lease_token'], current)
        self.assertEqual(self.spool.get('M-1')['state'], 'inflight')
        second.on_publish(second, None, 7, reason(0), None)
        self.await_condition(lambda: self.spool.get('M-1')['state'] == 'awaiting_receipt')
        second.receive(receipt.topic('oilfield'), receipt.to_bytes())
        self.assertEqual(self.spool.get('M-1')['state'], 'stored')

    def test_close_interrupts_puback_wait_and_preserves_unconfirmed_record(self):
        self.enqueue()
        client = ReceiptClient(puback=None)
        worker = self.worker(client)
        worker.start()
        self.assertTrue(client.entered_publish.wait(1))
        started = time.monotonic()
        worker.close()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(worker.snapshot()['running'])
        self.assertFalse(client.looping)
        self.assertNotEqual(self.spool.get('M-1')['state'], 'stored')
        self.assertEqual(sum(self.spool.counts().values()), 1)

    def test_close_timeout_does_not_close_storage_and_can_be_retried(self):
        gate = threading.Event()
        client = ReceiptClient(connect_gate=gate)
        worker = self.worker(client)
        worker.start()
        try:
            self.assertTrue(client.entered_connect.wait(1))
            started = time.monotonic()
            with self.assertRaises(ReliabilityError):
                worker.close(timeout=.03)
            self.assertLess(time.monotonic() - started, .5)
            self.enqueue()
            self.assertEqual(self.spool.counts()['pending'], 1)
        finally:
            gate.set()
            worker.close(timeout=1)
        self.assertFalse(client.connected)
        self.assertFalse(client.looping)

    def test_start_is_idempotent_and_closed_worker_cannot_restart(self):
        clients = []
        def factory(_config):
            client = ReceiptClient()
            clients.append(client)
            return client
        worker = self.worker(factory=factory)
        worker.start()
        worker.start()
        self.await_condition(lambda: worker.snapshot()['ready'])
        self.assertEqual(len(clients), 1)
        worker.close()
        with self.assertRaises(ReliabilityError):
            worker.start()

    def test_snapshot_transport_delta_is_not_reported_twice_or_called_center_success(self):
        self.enqueue()
        worker = self.worker()
        worker.start()
        self.await_condition(lambda: self.spool.get('M-1')['state'] == 'awaiting_receipt')
        # The database commit precedes the delta update; wait for the worker pass.
        self.assertTrue(worker.wait_once(timeout=1))
        first, second = worker.snapshot(), worker.snapshot()
        self.assertEqual(first['broker_published_since_last_snapshot'], 1)
        self.assertEqual(second['broker_published_since_last_snapshot'], 0)
        self.assertEqual(first['spool']['stored'], 0)
        self.assertEqual(first['spool']['awaiting_receipt'], 1)

    def test_wait_once_times_out_without_claiming_delivery_complete(self):
        client = ReceiptClient(suback=None)
        worker = self.worker(client)
        worker.start()
        self.assertTrue(client.entered_subscribe.wait(1))
        started = time.monotonic()
        self.assertFalse(worker.wait_once(timeout=.02))
        self.assertLess(time.monotonic() - started, .5)
        self.assertFalse(worker.snapshot()['ready'])
        self.assertEqual(client.publications, [])

    def test_constructor_rejects_unsafe_identity_and_timing_without_connecting(self):
        client = ReceiptClient()
        for options in ({'gateway_id': '../other'}, {'prefix': 'bad/#'}, {'gateway_id': ''}):
            with self.subTest(options=options), self.assertRaises((ReliabilityError, ValueError)):
                self.module.ReliableUplink(self.config, self.spool, client_factory=lambda _config: client,
                                           **({'gateway_id': 'GW', 'prefix': 'oilfield'} | options))
        for field in ('connect_timeout_seconds', 'publish_timeout_seconds'):
            for value in (True, float('inf'), 61):
                config = replace(self.config, **{field: value})
                with self.subTest(field=field, value=value), self.assertRaises((ReliabilityError, ValueError)):
                    self.module.ReliableUplink(config, self.spool, gateway_id='GW', prefix='oilfield')
        self.assertFalse(client.entered_connect.is_set())

    def test_reconnect_stops_previous_network_loop_before_replacing_client(self):
        first, second = ReceiptClient(), ReceiptClient()
        created, observed = [], []
        replacement = threading.Event()
        def factory(_config):
            if not created:
                created.append(first)
                return first
            observed.append(first.looping)
            replacement.set()
            return second
        worker = self.worker(factory=factory)
        worker.start()
        self.await_condition(lambda: worker.snapshot()['ready'])
        first.on_disconnect(first, None, None, reason(128), None)
        self.assertTrue(replacement.wait(1))
        self.assertEqual(observed, [False])

    def test_disconnect_socket_error_still_stops_loop_without_escaping_callback(self):
        class BrokenDisconnect(ReceiptClient):
            def disconnect(self):
                self.connected = False
                raise OSError('private-socket-diagnostic')
        obs = self.enqueue()
        client = BrokenDisconnect()
        worker = self.worker(client)
        worker.start()
        self.await_condition(lambda: self.spool.get('M-1')['state'] == 'awaiting_receipt')
        receipt = self.receipt(obs)
        with patch.object(self.spool, 'apply_receipt', side_effect=ReliabilityError('ingest.spool_write_unavailable', 'test failure')):
            try:
                client.receive(receipt.topic('oilfield'), receipt.to_bytes())
            except OSError:
                self.fail('Socket teardown must not escape the receipt callback')
        worker.close()
        self.assertFalse(client.looping)
        self.assertEqual(client.acks, [])

    def test_qos_zero_receipt_commits_without_sending_protocol_ack(self):
        obs = self.enqueue()
        client = ReceiptClient()
        worker = self.worker(client)
        worker.start()
        self.await_condition(lambda: worker.snapshot()['ready'])
        receipt = self.receipt(obs)
        client.receive(receipt.topic('oilfield'), receipt.to_bytes(), qos=0, mid=0)
        self.assertEqual(self.spool.get('M-1')['state'], 'stored')
        self.assertEqual(client.acks, [])

    def test_remote_transport_preserves_required_certificate_verification(self):
        self.config = replace(self.config, host='broker.example.invalid', ca_cert='ca.crt',
                              client_cert='gateway.crt', client_key='gateway.key')
        client = ReceiptClient()
        worker = self.worker(client)
        worker.start()
        self.await_condition(lambda: worker.snapshot()['ready'])
        self.assertEqual(client.tls, {'ca_certs': 'ca.crt', 'certfile': 'gateway.crt', 'keyfile': 'gateway.key',
                                     'cert_reqs': ssl.CERT_REQUIRED, 'tls_version': ssl.PROTOCOL_TLS_CLIENT})
        properties = client.connect_options[1]['properties']
        self.assertEqual(properties.ReceiveMaximum, 20)
        self.assertEqual(properties.MaximumPacketSize, 8192)

    def test_commit_failure_and_lost_commit_response_never_ack_receipt_early(self):
        self.spool.close()
        connect = sqlite3.connect
        with patch('backend.gateway.reliable_spool.sqlite3.connect',
                   side_effect=lambda *args, **kwargs: connect(*args, factory=FaultConnection, **kwargs)):
            self.spool = ReliableSpool(self.path, limits=self.limits)
        self.addCleanup(self.spool.close)
        for index, fault in enumerate(('commit', 'committed')):
            with self.subTest(fault=fault):
                obs = self.enqueue('COMMIT-' + str(index))
                # No outbound queue transaction can consume the injected fault.
                client = ReceiptClient(suback=None)
                worker = self.worker(client)
                worker.start()
                self.assertTrue(client.entered_subscribe.wait(1))
                receipt = self.receipt(obs)
                self.spool._connection.fault = fault
                client.receive(receipt.topic('oilfield'), receipt.to_bytes())
                worker.close()
                self.assertEqual(client.acks, [])
                self.assertTrue(client.disconnected.is_set())
                self.assertEqual(self.spool.get('COMMIT-' + str(index))['state'],
                                 'pending' if fault == 'commit' else 'stored')
                # A fresh connection can durably apply/re-read the first result.
                replacement = ReceiptClient(suback=None)
                retry = self.worker(replacement)
                retry.start()
                self.assertTrue(replacement.entered_subscribe.wait(1))
                replacement.receive(receipt.topic('oilfield'), receipt.to_bytes())
                self.assertEqual(replacement.acks, [(91, 1)])
                self.assertEqual(self.spool.get('COMMIT-' + str(index))['state'], 'stored')
                retry.close()

    def test_snapshot_exposes_local_capacity_and_application_age_without_raw_payload(self):
        obs = observation('M-1')
        self.spool.enqueue('M-1', obs.topic, obs.payload, confirmation_mode='application', now=100)
        self.spool.enqueue('M-2', 'legacy/topic', b'private bytes', confirmation_mode='broker', now=110)
        self.spool.clock = lambda: 120
        worker = self.worker()
        result = worker.snapshot()
        self.assertIn('queue_status', result)
        status = result['queue_status']
        self.assertEqual(status['pending_application'], 1)
        self.assertEqual(status['oldest_application_wait_seconds'], 20)
        self.assertEqual(status['capacity']['used_messages'], 2)
        self.assertEqual(status['capacity']['used_payload_bytes'], len(obs.payload) + len(b'private bytes'))
        self.assertEqual(status['capacity']['max_messages'], 100)
        self.assertGreater(status['capacity']['free_disk_bytes'], 0)
        self.assertNotIn('private bytes', json.dumps(result))
        self.assertEqual(sum(result['spool'].values()), 2)

    def test_wait_once_does_not_count_an_empty_claim_started_before_new_data(self):
        first_claim, next_claim = threading.Event(), threading.Event()
        release_first, release_next = threading.Event(), threading.Event()
        waiter_started, waiter_done = threading.Event(), threading.Event()
        real_claim = self.spool.claim
        calls, completed = [], []
        def controlled_claim(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                result = real_claim(**kwargs)
                first_claim.set()
                release_first.wait(2)
                return result
            if len(calls) == 2:
                next_claim.set()
                release_next.wait(2)
            return real_claim(**kwargs)
        worker = self.worker()
        def wait_for_fresh_pass():
            with worker._condition:
                waiter_started.set()
                completed.append(worker.wait_once(timeout=1))
            waiter_done.set()
        waiter = threading.Thread(target=wait_for_fresh_pass)
        try:
            with patch.object(self.spool, 'claim', side_effect=controlled_claim):
                worker.start()
                self.assertTrue(first_claim.wait(1))
                self.enqueue()
                waiter.start()
                self.assertTrue(waiter_started.wait(1))
                with worker._condition:
                    release_first.set()
                self.assertTrue(next_claim.wait(1))
                self.assertFalse(waiter_done.wait(.05), 'An old empty claim cannot complete a fresh drain attempt')
                release_next.set()
                self.assertTrue(waiter_done.wait(1))
                self.assertEqual(completed, [True])
                self.assertEqual(self.spool.get('M-1')['state'], 'awaiting_receipt')
        finally:
            release_first.set()
            release_next.set()
            if waiter.ident:
                waiter.join(2)
            worker.close()

    def test_callback_overflow_is_bounded_and_cannot_turn_into_confirmation(self):
        self.enqueue()
        def storm(source, *_args):
            for mid in range(100, 200):
                source.on_publish(source, None, mid, reason(0), None)
        client = ReceiptClient(hook=storm)
        worker = self.worker(client)
        worker.start()
        self.await_condition(lambda: worker.snapshot()['error_code'] == 'mqtt.callback_overflow')
        worker.close()
        self.assertEqual(self.spool.counts()['stored'], 0)
        self.assertEqual(self.spool.counts()['awaiting_receipt'], 0)
        self.assertEqual(self.spool.get('M-1')['payload'], observation('M-1').payload)

    def test_status_clock_rollback_and_disk_reserve_are_warnings_not_false_health(self):
        self.enqueue()
        self.spool.clock = lambda: 1
        with patch('backend.gateway.reliable_spool.shutil.disk_usage', return_value=SimpleNamespace(free=512)):
            result = self.worker().snapshot()['queue_status']
        self.assertTrue(result['clock_rollback_detected'])
        self.assertIsNone(result['oldest_application_wait_seconds'])
        self.assertFalse(result['capacity']['disk_reserve_ok'])
        self.assertEqual(result['pending_application'], 1)

class ReliableRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='receipt-runtime-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'spool.db'
        self.limits = SpoolLimits(100, 2 * 1024 * 1024, 1024 * 1024)
        initialize_spool(self.path, limits=self.limits)
        self.poller_calls = 0
        point = PointDefinition('load', 1, 4, 0, 'float32', unit='kN')
        device = ModbusDevice('D', 'tcp', (point,), host='192.0.2.10')
        self.config = SimpleNamespace(gateway_id='GW', spool_database=self.path, poll_interval_seconds=5,
                                      topic_prefix='oilfield', devices=(device,), confirmation_mode='application',
                                      spool_limits=self.limits, receipt_timeout_seconds=60,
                                      base_backoff_seconds=.02, max_backoff_seconds=.05,
                                      mqtt=MqttConfig('127.0.0.1', 1883, 'GW', username='gateway', password='test-secret',
                                                      connect_timeout_seconds=.15, publish_timeout_seconds=.15))

    def make_runtime(self, *, client=None, config=None, poller_factory=None):
        parent = self
        class Poller:
            def __init__(self, _gateway, device):
                self.device = device
            def poll(self, *, sequence):
                parent.poller_calls += 1
                now = datetime(2026, 9, 24, tzinfo=timezone.utc)
                sample = TelemetrySample('GW', 'D', 'load', now, sequence, 41.0, 'good', 'kN', 'modbus_tcp')
                return PollResult('D', now, sequence, (sample,), ())
        module = importlib.import_module('backend.gateway.reliable_uplink')
        client = client or ReceiptClient()
        def factory(mqtt, spool, **kwargs):
            return module.ReliableUplink(mqtt, spool, client_factory=lambda _config: client, **kwargs)
        runtime = GatewayRuntime.__new__(GatewayRuntime)
        def cleanup():
            if hasattr(runtime, '_closed'):
                runtime.close()
                return
            # Test-only cleanup for an old constructor failing before its
            # lifecycle fields exist. This runs after assertions, so it cannot
            # hide the product's resource-ownership failure from the test.
            uplink = getattr(runtime, 'uplink', None)
            if uplink is not None:
                uplink.close()
            spool = getattr(runtime, 'spool', None)
            if spool is not None:
                spool.close()
        self.addCleanup(cleanup)
        GatewayRuntime.__init__(runtime, config or self.config, poller_factory=poller_factory or Poller, uplink_factory=factory)
        return runtime

    def await_condition(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            threading.Event().wait(.005)
        self.fail('Expected runtime state was not observed before its bounded deadline')

    def test_network_delay_does_not_serialize_poll_cycles(self):
        blocked = threading.Event()
        client = ReceiptClient(hook=lambda *_args: blocked.wait(3))
        runtime = self.make_runtime(client=client)
        try:
            started = time.monotonic()
            one = runtime.run_cycle()
            self.assertLess(time.monotonic() - started, .7)
            self.assertTrue(client.entered_publish.wait(1))
            started = time.monotonic()
            two, three = runtime.run_cycle(), runtime.run_cycle()
            self.assertLess(time.monotonic() - started, .7)
            self.assertEqual((one.messages_enqueued, two.messages_enqueued, three.messages_enqueued), (1, 1, 1))
            self.assertEqual(self.poller_calls, 3)
            self.assertEqual(runtime.spool.counts()['stored'], 0)
        finally:
            blocked.set()
            runtime.close()

    def test_broker_config_on_v2_never_downgrades_existing_application_records(self):
        obs = observation('EXISTING')
        with closing(ReliableSpool(self.path, limits=self.limits)) as spool:
            spool.enqueue('EXISTING', obs.topic, obs.payload, confirmation_mode='application')
        config = SimpleNamespace(**(vars(self.config) | {'confirmation_mode': 'broker'}))
        runtime = self.make_runtime(config=config)
        runtime.run_cycle()
        self.await_condition(lambda: runtime.spool.counts()['broker_confirmed'] == 1)
        self.assertEqual(runtime.spool.get('EXISTING')['confirmation_mode'], 'application')
        self.assertEqual(runtime.spool.get('EXISTING')['state'], 'awaiting_receipt')
        self.assertEqual(runtime.spool.counts()['stored'], 0)

    def test_application_mode_refuses_missing_queue_without_creating_one(self):
        missing = self.path.parent / 'missing.db'
        config = SimpleNamespace(**(vars(self.config) | {'spool_database': missing}))
        with self.assertRaises(ReliabilityError):
            self.make_runtime(config=config)
        self.assertFalse(missing.exists())

    def test_application_mode_refuses_v1_without_mutating_legacy_rows(self):
        path = self.path.parent / 'legacy.db'
        with closing(DurableSpool(path)) as old:
            old.enqueue('OLD', 'legacy/topic', b'preserve')
        config = SimpleNamespace(**(vars(self.config) | {'spool_database': path}))
        with self.assertRaises(ReliabilityError):
            self.make_runtime(config=config)
        self.assertEqual(read_spool_version(path), 1)
        with closing(DurableSpool(path)) as old:
            self.assertEqual(old.counts()['pending'], 1)

    def test_failed_runtime_construction_releases_queue_ownership(self):
        def broken(*_args):
            raise ValueError('invalid test poller')
        with self.assertRaisesRegex(ValueError, 'invalid test poller'):
            self.make_runtime(poller_factory=broken)
        with closing(ReliableSpool(self.path, limits=self.limits)) as spool:
            self.assertEqual(sum(spool.counts().values()), 0)

    def test_thread_start_failure_releases_queue_ownership(self):
        with patch('backend.gateway.reliable_uplink.threading.Thread.start', side_effect=RuntimeError('cannot start new thread')):
            with self.assertRaises(RuntimeError):
                self.make_runtime()
        with closing(ReliableSpool(self.path, limits=self.limits)) as queue:
            self.assertEqual(sum(queue.counts().values()), 0)

    def test_close_wait_failure_keeps_queue_open_and_second_close_finishes(self):
        gate = threading.Event()
        client = ReceiptClient(connect_gate=gate)
        runtime = self.make_runtime(client=client)
        real_close = runtime.uplink.close
        try:
            self.assertTrue(client.entered_connect.wait(1))
            with patch.object(runtime.uplink, 'close', side_effect=lambda: real_close(timeout=.03)):
                with self.assertRaises(ReliabilityError):
                    runtime.close()
            self.assertEqual(sum(runtime.spool.counts().values()), 0)
        finally:
            gate.set()
            runtime.close()
        with self.assertRaises(ReliabilityError):
            runtime.spool.counts()

    def test_closed_runtime_does_not_poll_or_enqueue_again(self):
        runtime = self.make_runtime()
        runtime.close()
        with self.assertRaises((ReliabilityError, RuntimeError)):
            runtime.run_cycle()
        self.assertEqual(self.poller_calls, 0)

    def test_once_reports_outstanding_application_confirmation_instead_of_all_delivered(self):
        entry = importlib.import_module('backend.gateway.__main__')
        output = io.StringIO()
        with patch.object(entry, 'load_gateway_config', return_value=self.config), \
             patch.object(entry, 'GatewayRuntime', side_effect=lambda config: self.make_runtime(config=config)), \
             patch.object(entry.signal, 'signal'), redirect_stdout(output):
            code = entry.main(['--config', 'not-opened.toml', '--once'])
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        cycle = next(event for event in events if event['event'] == 'gateway_cycle')
        self.assertEqual(cycle['confirmation_mode'], 'application')
        self.assertFalse(cycle['delivery_complete'])
        self.assertEqual(cycle['spool']['stored'], 0)
        self.assertEqual(cycle['spool']['awaiting_receipt'], 1)

    def test_cli_storage_failure_has_safe_nonzero_result_and_preserves_record(self):
        entry = importlib.import_module('backend.gateway.__main__')
        runtime = self.make_runtime(client=ReceiptClient(suback=None))
        runtime.run_cycle()
        output = io.StringIO()
        with patch.object(entry, 'load_gateway_config', return_value=self.config), \
             patch.object(entry, 'GatewayRuntime', return_value=runtime), \
             patch.object(runtime.spool, 'enqueue', side_effect=ReliabilityError('ingest.spool_capacity_exhausted', 'private payload')), \
             patch.object(entry.signal, 'signal'), redirect_stdout(output):
            try:
                code = entry.main(['--config', 'not-opened.toml', '--once'])
            except ReliabilityError:
                self.fail('Storage failure must produce a controlled, nonzero CLI result')
        self.assertNotEqual(code, 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[-1]['error_code'], 'ingest.spool_capacity_exhausted')
        self.assertNotIn('private payload', output.getvalue())
        with closing(ReliableSpool(self.path, limits=self.limits, base_backoff_seconds=.02, max_backoff_seconds=.05)) as queue:
            self.assertEqual(sum(queue.counts().values()), 1)

    def test_cli_shutdown_timeout_is_nonzero_and_keeps_live_worker_storage_open(self):
        entry = importlib.import_module('backend.gateway.__main__')
        gate = threading.Event()
        client = ReceiptClient(connect_gate=gate)
        runtime = self.make_runtime(client=client)
        wait_once, close = runtime.uplink.wait_once, runtime.uplink.close
        output = io.StringIO()
        try:
            self.assertTrue(client.entered_connect.wait(1))
            with patch.object(entry, 'load_gateway_config', return_value=self.config), \
                 patch.object(entry, 'GatewayRuntime', return_value=runtime), \
                 patch.object(runtime.uplink, 'wait_once', side_effect=lambda: wait_once(timeout=.01)), \
                 patch.object(runtime.uplink, 'close', side_effect=lambda: close(timeout=.02)), \
                 patch.object(entry.signal, 'signal'), redirect_stdout(output):
                try:
                    code = entry.main(['--config', 'not-opened.toml', '--once'])
                except ReliabilityError:
                    self.fail('Shutdown timeout must be a controlled nonzero CLI result')
            self.assertNotEqual(code, 0)
            event = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(event['error_code'], 'ingest.uplink_close_timeout')
            self.assertEqual(sum(runtime.spool.counts().values()), 1)
        finally:
            gate.set()
            runtime.close()


class GatewayReceiptConfigTests(unittest.TestCase):
    SETTINGS = ('confirmation_mode = "application"\nreceipt_timeout_seconds = 60\n'
                'base_backoff_seconds = 2\nmax_backoff_seconds = 300\n'
                '[gateway.spool_limits]\nmax_messages = 100\nmax_payload_bytes = 2097152\nmin_free_bytes = 1048576\n\n')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='receipt-config-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'gateway.toml'

    def load(self, settings=None, *, transform=None):
        raw = VALID_CONFIG.replace('[mqtt]', (self.SETTINGS if settings is None else settings) + '[mqtt]')
        if transform:
            raw = transform(raw)
        self.path.write_text(raw, encoding='utf-8')
        return load_gateway_config(self.path, env={'TEST_MQTT_PASSWORD': 'not-a-real-secret'}, project_root=self.root)

    def test_old_config_remains_explicitly_broker_without_creating_queue(self):
        config = self.load('')
        self.assertEqual(getattr(config, 'confirmation_mode', None), 'broker')
        self.assertIsNone(config.spool_limits)
        self.assertFalse(config.spool_database.exists())
        positional = GatewayConfig(config.gateway_id, config.spool_database, config.poll_interval_seconds,
                                   config.topic_prefix, config.mqtt, config.devices)
        self.assertEqual(positional.confirmation_mode, 'broker')

    def test_application_settings_load_explicit_limits_and_bounded_timing(self):
        config = self.load()
        self.assertEqual(getattr(config, 'confirmation_mode', None), 'application')
        self.assertEqual(config.spool_limits, SpoolLimits(100, 2097152, 1048576))
        self.assertEqual((config.receipt_timeout_seconds, config.base_backoff_seconds, config.max_backoff_seconds), (60, 2, 300))
        self.assertFalse(config.spool_database.exists())

    def test_application_without_limits_and_unknown_mode_are_rejected(self):
        for settings in ('confirmation_mode = "application"\n', self.SETTINGS.replace('"application"', '"auto"')):
            with self.subTest(settings=settings), self.assertRaises(GatewayConfigurationError):
                self.load(settings)

    def test_capacity_types_bounds_and_unknown_fields_are_rejected(self):
        for old, new in [('max_messages = 100', 'max_messages = true'), ('max_messages = 100', 'max_messages = 0'),
                         ('max_payload_bytes = 2097152', 'max_payload_bytes = -1'),
                         ('min_free_bytes = 1048576', 'min_free_bytes = 9223372036854775808'),
                         ('max_messages = 100', 'max_message_typo = 100')]:
            with self.subTest(new=new), self.assertRaises(GatewayConfigurationError):
                self.load(self.SETTINGS.replace(old, new))

    def test_impractical_nonfinite_and_boolean_timing_are_rejected_before_running(self):
        for old, new in [('receipt_timeout_seconds = 60', 'receipt_timeout_seconds = true'),
                         ('receipt_timeout_seconds = 60', 'receipt_timeout_seconds = 0.00001'),
                         ('receipt_timeout_seconds = 60', 'receipt_timeout_seconds = 301'),
                         ('base_backoff_seconds = 2', 'base_backoff_seconds = nan'),
                         ('max_backoff_seconds = 300', 'max_backoff_seconds = inf'),
                         ('max_backoff_seconds = 300', 'max_backoff_seconds = 1')]:
            with self.subTest(new=new), self.assertRaises(GatewayConfigurationError):
                self.load(self.SETTINGS.replace(old, new))

    def test_gateway_identity_and_mqtt_numeric_settings_fail_closed(self):
        for old, new in [('id = "GW-001"', 'id = "bad id"'), ('poll_interval_seconds = 5', 'poll_interval_seconds = nan'),
                         ('port = 1883', 'port = true'), ('host = "127.0.0.1"', 'host = 42')]:
            with self.subTest(new=new), self.assertRaises(GatewayConfigurationError):
                self.load(transform=lambda raw: raw.replace(old, new))

    def test_check_reports_mode_without_connecting_or_migrating_the_queue(self):
        config = self.load()
        entry = importlib.import_module('backend.gateway.__main__')
        output = io.StringIO()
        with patch.object(entry, 'load_gateway_config', return_value=config), \
             patch.object(entry, 'GatewayRuntime', side_effect=AssertionError('check must not construct runtime')), redirect_stdout(output):
            code = entry.main(['--config', str(self.path), '--check'])
        self.assertEqual(code, 0)
        event = json.loads(output.getvalue())
        self.assertEqual(event.get('confirmation_mode'), 'application')
        self.assertFalse(event['queue_checked'])
        self.assertFalse(event['network_checked'])
        self.assertFalse(config.spool_database.exists())
