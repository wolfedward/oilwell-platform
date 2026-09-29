"""Actual isolated durable intake and MQTT lifecycle boundaries."""
import importlib
import json
import threading
import time
from types import SimpleNamespace
import unittest
from uuid import UUID

import psycopg
from backend.waveforms.errors import WaveformError
from backend.waveforms.repository import WaveformRepository
from tests.waveform_support import WaveformDatabaseCase, LIMITS
from tests.test_waveform_repository import capture


class AckPeer:
    def __init__(self): self.ack_calls = []
    def ack(self, mid, qos): self.ack_calls.append((mid, qos)); return 0
    def is_connected(self): return True


class WaveformConsumerTests(WaveformDatabaseCase):
    def consumer(self, connect=None):
        self.assertIsNotNone(importlib.util.find_spec("backend.waveforms.consumer"), "manual ACK boundary is required")
        module = importlib.import_module("backend.waveforms.consumer")
        repository = WaveformRepository(connect or self.connect, limits=LIMITS, prefix="oil", ingress_gateway_id="GW")
        return module.WaveformConsumer(repository, source_ref=UUID("ed1d031a-608e-437a-835d-88228f5955d4"))

    def message(self, payload, **changes):
        return SimpleNamespace(**(dict(payload=payload, topic="oil/v2/GW/PUMP-001/waveform",
                                      qos=1, retain=False, dup=False, mid=12) | changes))

    def test_no_ack_on_unconfirmed_commit(self):
        class Cursor:
            def __init__(self, target, connection): self.target, self.connection = target, connection
            def __getattr__(self, key): return getattr(self.target, key)
            def __enter__(self): return self
            def __exit__(self, *args): self.target.close()
        class UncertainCommit:
            def __init__(self, connection): self.connection = connection
            def __getattr__(self, key): return getattr(self.connection, key)
            def __enter__(self): return self
            def __exit__(self, *args): return self.connection.__exit__(*args)
            def cursor(self): return Cursor(self.connection.cursor(), self)
            def commit(self):
                self.connection.commit()
                raise psycopg.OperationalError("injected lost commit response")
        peer = AckPeer()
        consumer = self.consumer(lambda: UncertainCommit(self.connect()))
        with self.assertRaises(WaveformError): consumer.handle_message(peer, self.message(capture()[0]))
        self.assertEqual(peer.ack_calls, [])
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 1)

    def test_committed_transport_rejection_then_valid_message(self):
        consumer, peer = self.consumer(), AckPeer()
        raw = capture()[0]
        rejected = consumer.handle_message(peer, self.message(raw, retain=True))
        accepted = consumer.handle_message(peer, self.message(raw))
        self.assertEqual((rejected.reason_code, accepted.status), ("retained_publish", "stored"))
        self.assertEqual(peer.ack_calls, [(12, 1), (12, 1)])
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 1)

    def test_retries_keep_one_controlled_source(self):
        consumer, peer = self.consumer(), AckPeer()
        message = self.message(capture()[0])
        consumer.handle_message(peer, message)
        consumer.handle_message(peer, message)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.observations", role="receiver"), 1)
        self.assertEqual(self.scalar("SELECT duplicate_count FROM waveforms.observations", role="receiver"), 1)

    def test_startup_is_read_only_and_rejects_wrong_role_and_capacity(self):
        from dataclasses import replace
        from backend.waveforms import consumer
        self.assertTrue(hasattr(consumer, "check_component"), "real startup gates are required")
        from tests.waveform_stack import runtime_config
        config = runtime_config(self, "receive")
        consumer.check_component(config)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.worker_health", role="receiver"), 0)
        self.assertEqual(self.scalar("SELECT version FROM waveforms.state_head", role="receiver"), 1)
        wrong = replace(config, postgres={"receiver": self.settings["assembler"]})
        with self.assertRaises(WaveformError): consumer.check_component(wrong)
        limits = replace(LIMITS, gateways={"GW": replace(LIMITS.gateways["GW"], facts=1)})
        with self.assertRaises(WaveformError): consumer.check_component(replace(config, limits=limits))

    def test_gateway_a_backpressure_does_not_disconnect_b(self):
        from dataclasses import replace
        from backend.waveforms import consumer
        self.assertTrue(hasattr(consumer, "run_receive"), "gateway owner loops are required")
        from tests.waveform_stack import Stack
        limits = replace(LIMITS, gateways={"A": replace(LIMITS.gateways["GW"], incomplete_captures=1),
                                          "B": LIMITS.gateways["GW"]})
        with self.capacity_fixture(limits), Stack(self, limits=limits) as stack:
            stack.launch("receive")
            stack.await_event("receive", "subscribed", gateway="A")
            stack.await_event("receive", "subscribed", gateway="B")
            stack.publish("A", capture("A")[0])
            stack.await_count("facts", 1)
            stack.publish("A", capture("A")[0])
            stack.await_event("receive", "retry", gateway="A")
            for _ in range(3): stack.publish("B", capture("B")[0])
            stack.await_count("facts", 4)
            self.assertEqual(len(stack.events("receive", "subscribed", gateway="B")), 1)
            stack.stop("receive")
            self.assertEqual(stack.processes["receive"].returncode, 0)

    def test_real_receiver_transport_rejection_and_assembly_loop(self):
        from backend.waveforms import consumer
        self.assertTrue(hasattr(consumer, "run_assemble"), "actual assembly pool is required")
        from tests.waveform_stack import Stack
        with Stack(self) as stack:
            stack.launch("receive")
            stack.await_event("receive", "subscribed", gateway="GW")
            raw, chunk = capture()
            stack.publish("GW", raw, retain=True)
            stack.await_count("message_outcomes", 1)
            stack.publish("GW", raw)
            stack.publish("GW", chunk)
            stack.await_count("message_outcomes", 3)
            stack.launch("assemble")
            stack.await_count("completion", 1, role="assembler")
            self.assertEqual(self.scalar("SELECT status FROM waveforms.completion", role="assembler"), "complete")
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.message_outcomes WHERE reason_code='retained_publish'", role="receiver"), 1)
            stack.stop("assemble")
            stack.stop("receive")
            self.assertTrue(all(process.returncode == 0 for process in stack.processes.values()))

    def test_old_delivery_completion_cannot_ack_reused_mid_in_new_broker_session(self):
        from tests.waveform_stack import Stack
        with Stack(self) as stack:
            stack.launch("receive", fault="pause_deliveries")
            stack.await_event("receive", "subscribed", gateway="GW")
            stack.publish("GW", capture()[0])
            first = stack.await_event("receive", "delivery_paused", sequence=1)
            stack.broker.stop()
            stack.broker.start()  # persistence=false deliberately creates a new broker session
            self.assertEqual(len(stack.events("receive", "subscribed")), 1)
            stack.command("receive", "release")
            stack.await_condition(lambda: len(stack.events("receive", "subscribed")) >= 2, "fresh receiver session")
            stack.await_count("facts", 1)
            stack.reset_peer("GW")
            stack.publish("GW", capture()[0])
            second = stack.await_event("receive", "delivery_paused", sequence=2)
            self.assertEqual(first["mid"], second["mid"])
            self.assertNotEqual(first["client_generation"], second["client_generation"])
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 1)
            self.assertFalse(stack.events("receive", "ack", client_generation=second["client_generation"]))
            stack.command("receive", "release")
            stack.await_count("facts", 2)
            stack.await_event("receive", "ack", client_generation=second["client_generation"])
            stack.stop("receive")
            self.assertEqual(stack.await_event("receive", "stopped")["remaining_threads"], [])

    def test_assembly_round_robin_does_not_drain_gateway_a_first(self):
        from dataclasses import replace
        from tests.waveform_stack import Stack
        from tests.test_waveform_repository import observation
        limits = replace(LIMITS, gateways={"A": LIMITS.gateways["GW"], "B": LIMITS.gateways["GW"]})
        with self.capacity_fixture(limits), Stack(self, limits=limits) as stack:
            for gateway in ("A", "A", "A", "B"):
                repo = WaveformRepository(self.connect, limits=limits, prefix="oil", ingress_gateway_id=gateway)
                for payload in capture(gateway): repo.ingest(observation(payload, gateway=gateway))
            stack.sections["waveforms"]["assembly_workers"] = 1
            stack.save_config()
            stack.launch("assemble")
            stack.await_count("completion", 4, role="assembler")
            self.assertEqual([item["gateway"] for item in stack.events("assemble", "completed")][:2], ["A", "B"])

    def test_stopped_connection_factory_never_opens_another_connection(self):
        from backend.waveforms.consumer import DatabaseConnections
        from tests.waveform_stack import runtime_config
        stop = threading.Event()
        factory = DatabaseConnections(runtime_config(self, "receive"))
        factory.stop = stop
        stop.set()
        with self.assertRaises(WaveformError):
            with factory.factory("receiver")(): pass

    def test_receiver_health_preserves_assembler_rollback_high_water(self):
        from backend.waveforms.consumer import _receiver_health
        from tests.waveform_stack import runtime_config
        from uuid import uuid4
        with self.connect("assembler") as connection:
            connection.execute("INSERT INTO waveforms.worker_health(worker_id,component,gateway_id,last_seen,error_code) VALUES(%s,'assembler','GW','2099-01-01T00:00:00Z','clock_rollback')", (uuid4(),))
        _receiver_health(self.connect, runtime_config(self, "receive"), "GW", success=True)
        with self.connect("assembler") as connection:
            row = connection.execute("SELECT extract(year from last_seen),error_code FROM waveforms.worker_health WHERE component='assembler'").fetchone()
        self.assertEqual(tuple(row), (2099, "clock_rollback"))

    def test_shutdown_cancels_real_active_query_and_prevents_later_sql(self):
        from backend.waveforms import consumer
        from tests.waveform_stack import runtime_config
        self.assertTrue(hasattr(consumer.DatabaseConnections, "cancel_active"), "bounded owned-query cancellation is required")
        stop, entered = threading.Event(), threading.Event()
        factory = consumer.DatabaseConnections(runtime_config(self, "receive"), stop)
        errors = []
        def query():
            try:
                with factory.factory("receiver")() as connection:
                    entered.set()
                    connection.execute("SELECT pg_sleep(30)")
            except Exception as error: errors.append(type(error))
        thread = threading.Thread(target=query)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            # Observe the actual query, rather than guessing that cancel beats execute.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with self.connect("receiver") as connection:
                    active = connection.execute("SELECT count(*) FROM pg_stat_activity WHERE usename=current_user AND query='SELECT pg_sleep(30)' AND state='active'").fetchone()[0]
                if active: break
                time.sleep(.02)
            self.assertTrue(active)
            stop.set()
            factory.cancel_active(time.monotonic() + 3)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [psycopg.errors.QueryCanceled])
            self.assertEqual(factory.active_count, 0)
        finally:
            stop.set()
            factory.cancel_active(time.monotonic() + 3)
            thread.join(35)
        stop.clear()
        with factory.factory("receiver")() as connection:
            stop.set()
            with self.assertRaises(WaveformError): connection.execute("SELECT 1")

    def test_shutdown_deadline_is_shared_and_incomplete_is_not_success(self):
        from backend.waveforms import consumer
        from tests.waveform_stack import runtime_config
        self.assertTrue(hasattr(consumer, "_shutdown"), "bounded shutdown result is required")
        release, stop = threading.Event(), threading.Event()
        factory = consumer.DatabaseConnections(runtime_config(self, "receive"), stop)
        pool = consumer._OwnedPool(2, "waveform-deadline-test")
        futures = [pool.submit(release.wait) for _ in range(2)]
        started = time.monotonic()
        try:
            with self.assertRaises(WaveformError) as caught:
                consumer._shutdown(pool, futures, factory, stop, timeout=.15)
            self.assertEqual(caught.exception.code, "waveform.shutdown_incomplete")
            self.assertLess(time.monotonic() - started, .6)
            self.assertTrue(stop.is_set())
        finally:
            release.set()
            pool.shutdown(wait=True)

    def test_persistent_offline_session_and_source_survive_worker_restart(self):
        from tests.waveform_stack import Stack
        with Stack(self) as stack:
            stack.launch("receive")
            stack.await_event("receive", "subscribed")
            stack.stop("receive")
            raw = capture()[0]
            stack.publish("GW", raw)
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 0)
            stack.launch("receive")
            stack.await_count("facts", 1)
            stack.publish("GW", raw)
            stack.await_condition(lambda: self.scalar("SELECT duplicate_count FROM waveforms.observations", role="receiver") == 1, "stable restarted source")
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.observations", role="receiver"), 1)
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.worker_health WHERE component='receiver'", role="receiver"), 1)

    def test_hard_limit_is_not_durable_quarantine_and_qos0_is_evidence_only(self):
        consumer, peer = self.consumer(), AckPeer()
        with self.assertRaises(WaveformError): consumer.handle_message(peer, self.message(b"x" * 262145))
        self.assertEqual(peer.ack_calls, [])
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 0)
        result = consumer.handle_message(peer, self.message(capture()[0], qos=0))
        self.assertEqual(result.reason_code, "qos_not_supported")
        self.assertEqual(peer.ack_calls, [])

    def test_real_cli_component_checks(self):
        from tests.waveform_stack import Stack
        import os
        import subprocess
        import sys
        with Stack(self) as stack:
            env = dict(os.environ, TEST_WAVEFORM_MQTT=stack.broker.password, PYTHONUTF8="1")
            for component, expected in (("receive", 0), ("assemble", 0), ("publish", 0)):
                result = subprocess.run([sys.executable, "-m", "backend.waveforms", "--config", str(stack.config), component, "--check"],
                    capture_output=True, text=True, env=env, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], "checked" if expected == 0 else "unavailable")
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.worker_health", role="receiver"), 0)

    def test_connection_limit_applies_to_real_concurrent_sessions(self):
        from concurrent.futures import ThreadPoolExecutor
        from dataclasses import replace
        from backend.waveforms.consumer import DatabaseConnections
        from tests.waveform_stack import runtime_config
        factory = DatabaseConnections(replace(runtime_config(self, "receive"), max_db_connections=2))
        maximum = []
        def work():
            with factory.factory("receiver")() as connection:
                maximum.append(factory.active_count)
                connection.execute("SELECT pg_sleep(0.1)")
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: work(), range(6)))
        self.assertLessEqual(max(maximum), 2)
        self.assertEqual(factory.active_count, 0)

    def test_readiness_rejects_unlogged_storage_without_repair(self):
        from backend.waveforms.consumer import check_component
        from tests.waveform_stack import runtime_config
        config = runtime_config(self, "receive")
        try:
            with self.connect("migration_owner") as connection:
                connection.execute("ALTER TABLE waveforms.state_head SET UNLOGGED")
            with self.assertRaises(WaveformError): check_component(config)
            self.assertEqual(self.scalar("SELECT relpersistence FROM pg_class WHERE oid='waveforms.state_head'::regclass", role="receiver"), "u")
        finally:
            with self.connect("migration_owner") as connection:
                connection.execute("ALTER TABLE waveforms.state_head SET LOGGED")

    def test_stop_during_receiver_query_keeps_delivery_replayable(self):
        from tests.waveform_stack import Stack
        with Stack(self) as stack:
            stack.launch("receive", fault="query_in_ingest")
            stack.await_event("receive", "subscribed")
            stack.publish("GW", capture()[0])
            stack.await_event("receive", "query_started")
            stack.await_condition(lambda: self.scalar("SELECT count(*) FROM pg_stat_activity WHERE usename=current_user AND query='SELECT pg_sleep(30)' AND state='active'", role="receiver") == 1, "real intake query")
            started = time.monotonic()
            stack.stop("receive")
            self.assertLess(time.monotonic() - started, 8)
            self.assertEqual(stack.await_event("receive", "stopped")["remaining_threads"], [])
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 0)
            stack.launch("receive")
            stack.await_count("facts", 1)

    def test_stop_between_assembly_reads_leaves_reclaimable_lease(self):
        from datetime import datetime, timedelta, timezone
        from backend.waveforms.assembly import Assembler
        from tests.waveform_stack import Stack
        from tests.test_waveform_repository import observation
        repo = WaveformRepository(self.connect, limits=LIMITS, prefix="oil", ingress_gateway_id="GW")
        for payload in capture(): repo.ingest(observation(payload))
        with Stack(self) as stack:
            stack.launch("assemble", fault="pause_assembly")
            stack.await_event("assemble", "assembly_paused")
            stack.stop("assemble")
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.completion", role="assembler"), 0)
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.assembly_jobs WHERE lease_until IS NOT NULL", role="assembler"), 1)
            now = datetime.now(timezone.utc) + timedelta(seconds=31)
            assembler = Assembler(lambda: self.connect("assembler"), clock=lambda: now, limits=LIMITS)
            from uuid import uuid4
            lease = assembler.claim(uuid4(), gateway_id="GW")
            self.assertIsNotNone(lease)
            self.assertEqual(assembler.run(lease).status, "complete")

    def test_late_database_connect_is_closed_by_its_owner_without_work(self):
        from unittest.mock import patch
        from backend.waveforms.consumer import DatabaseConnections
        from tests.waveform_stack import runtime_config
        stop, opened, release = threading.Event(), threading.Event(), threading.Event()
        factory = DatabaseConnections(runtime_config(self, "receive"), stop)
        real_connect, actual, failures = psycopg.connect, [], []
        def delayed(**kwargs):
            connection = real_connect(**kwargs)
            actual.append(connection)
            opened.set()
            release.wait(5)
            return connection
        def worker():
            try:
                with factory.factory("receiver")() as connection: connection.execute("SELECT 1")
            except WaveformError as error: failures.append(error.code)
        with patch("backend.waveforms.consumer.psycopg.connect", side_effect=delayed):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(opened.wait(3))
                stop.set()
                release.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, ["waveform.stopping"])
                self.assertTrue(actual[0].closed)
                self.assertEqual(factory.active_count, 0)
            finally:
                release.set()
                thread.join(5)

    def test_concurrent_renewals_fit_minimum_accepted_connection_budget(self):
        from dataclasses import replace
        from tests.waveform_stack import Stack
        from tests.test_waveform_repository import observation
        limits = replace(LIMITS, gateways={"A": LIMITS.gateways["GW"], "B": LIMITS.gateways["GW"]})
        with self.capacity_fixture(limits), Stack(self, limits=limits) as stack:
            for gateway in ("A", "B"):
                repo = WaveformRepository(self.connect, limits=limits, prefix="oil", ingress_gateway_id=gateway)
                for payload in capture(gateway): repo.ingest(observation(payload, gateway=gateway))
            stack.launch("assemble", fault="concurrent_renewals")
            stack.await_count("completion", 2, role="assembler")
            events = stack.events("assemble", "nested_renewal_connection")
            self.assertEqual(len(events), 2)
            self.assertEqual(max(item["active"] for item in events), 4)

    def test_keepalive_disconnect_during_database_delay_replays_safely(self):
        from tests.waveform_stack import Stack
        with Stack(self) as stack:
            stack.launch("receive", fault="pause_deliveries")
            stack.await_event("receive", "subscribed")
            stack.publish("GW", capture()[0])
            stack.await_event("receive", "delivery_paused", sequence=1)
            client_id = stack.sections["gateways.GW.mqtt_receive"]["client_id"]
            stack.await_condition(lambda: any(client_id in line and "exceeded timeout" in line for line in
                (stack.root / "broker.log").read_text(encoding="utf-8", errors="replace").splitlines()), "broker keepalive expiry", timeout=25)
            stack.command("receive", "release")
            stack.await_event("receive", "delivery_paused", sequence=2)
            stack.command("receive", "release")
            stack.await_condition(lambda: self.scalar("SELECT duplicate_count FROM waveforms.observations", role="receiver") == 1, "safe replay after keepalive loss")
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 1)

    def test_idle_and_cancelled_pool_workers_are_joined(self):
        from backend.waveforms.consumer import _OwnedPool, _shutdown, DatabaseConnections
        from tests.waveform_stack import runtime_config
        stop, entered = threading.Event(), threading.Event()
        factory = DatabaseConnections(runtime_config(self, "receive"), stop)
        pool = _OwnedPool(2, "waveform-idle-test")
        def busy():
            entered.set()
            stop.wait(3)
        futures = [pool.submit(busy), pool.submit(lambda: None)]
        self.assertTrue(entered.wait(3))
        futures[1].result(timeout=3)
        futures.extend(pool.submit(lambda: None) for _ in range(3))
        _shutdown(pool, futures, factory, stop, timeout=3)
        self.assertFalse(any(owner.is_alive() for owner in pool.owned_threads()))
        self.assertEqual(factory.active_count, 0)

    def test_startup_rejects_async_session_without_repairing_it(self):
        from dataclasses import replace
        from backend.waveforms.consumer import check_component
        from tests.waveform_stack import runtime_config
        config = runtime_config(self, "receive")
        settings = dict(config.postgres["receiver"], options="-c synchronous_commit=off")
        with self.assertRaises(WaveformError) as caught:
            check_component(replace(config, postgres={"receiver": settings}))
        self.assertEqual(caught.exception.code, "waveform.durability_unavailable")
        with psycopg.connect(**settings) as connection:
            self.assertEqual(connection.execute("SHOW synchronous_commit").fetchone()[0], "off")
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.worker_health", role="receiver"), 0)

    def test_failed_test_context_stops_only_owned_worker_and_broker(self):
        from tests.waveform_stack import Stack
        with self.assertRaisesRegex(RuntimeError, "intentional context failure"):
            with Stack(self) as stack:
                stack.launch("receive")
                stack.await_event("receive", "subscribed")
                process, broker_process, directory = stack.processes["receive"], stack.broker.process, stack.root
                raise RuntimeError("intentional context failure")
        self.assertEqual(process.returncode, 0)
        self.assertIsNotNone(broker_process.poll())
        self.assertFalse(directory.exists())

    def test_would_block_disconnect_closes_network_and_wakeup_sockets(self):
        from unittest.mock import patch
        from backend.waveforms import consumer
        from backend.waveforms.config import load_waveform_config
        from tests.waveform_stack import Stack
        import os
        stop, records = threading.Event(), []
        create = consumer.create_client
        def traced(gateway):
            client = create(gateway)
            disconnect, send = client.disconnect, client._sock_send
            def blocked(data):
                if data[0] == 0xe0:
                    raise BlockingIOError()
                return send(data)
            def disconnect_traced():
                sockets = (client.socket(), client._sockpairR, client._sockpairW)
                result = disconnect()
                records.append((client, sockets, result, client.want_write(), threading.current_thread()))
                return result
            client._sock_send, client.disconnect = blocked, disconnect_traced
            return client
        with Stack(self) as stack, patch.dict(os.environ, {"TEST_WAVEFORM_MQTT": stack.broker.password}):
            config = load_waveform_config(stack.config, component="receive")
            try:
                with patch.object(consumer, "create_client", side_effect=traced):
                    consumer.run_receive(config, stop, on_event=lambda event, **_: stop.set() if event == "subscribed" else None)
                self.assertEqual(len(records), 1)
                client, sockets, result, queued, owner = records[0]
                self.assertEqual(result, 0)
                self.assertTrue(queued, "DISCONNECT must really remain queued")
                self.assertTrue(all(sock is not None for sock in sockets))
                self.assertEqual([sock.fileno() for sock in sockets], [-1, -1, -1])
                self.assertIsNone(client.socket())
                self.assertFalse(owner.is_alive())
            finally:
                for client, *_ in records: client._reset_sockets()

    def test_failed_socket_disposal_reports_incomplete_without_replacement(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        from backend.waveforms import consumer, __main__ as cli
        from backend.waveforms.config import load_waveform_config
        from tests.waveform_stack import Stack
        import os
        records = []
        create, run = consumer.create_client, consumer.run_receive
        def traced(gateway):
            client = create(gateway)
            reset, disconnect, send = client._reset_sockets, client.disconnect, client._sock_send
            def blocked(data):
                if data[0] == 0xe0: raise BlockingIOError()
                return send(data)
            def disconnect_traced():
                records.append((client, client.socket(), reset))
                client._reset_sockets = lambda: None  # A disposal that fails to close.
                return disconnect()
            client._sock_send, client.disconnect = blocked, disconnect_traced
            return client
        def bounded(config, stop):
            return run(config, stop, on_event=lambda event, **_: stop.set() if event == "subscribed" else None)
        with Stack(self) as stack, patch.dict(os.environ, {"TEST_WAVEFORM_MQTT": stack.broker.password}):
            config = load_waveform_config(stack.config, component="receive")
            try:
                with patch.object(consumer, "create_client", side_effect=traced), patch.object(cli, "load_waveform_config", return_value=config), patch.object(cli, "run_receive", side_effect=bounded), redirect_stdout(StringIO()) as output:
                    result = cli.main(["--config", str(stack.config), "receive"])
                self.assertEqual(result, 2)
                self.assertEqual(json.loads(output.getvalue())["code"], "waveform.shutdown_incomplete")
                self.assertEqual(len(records), 1)
                self.assertNotEqual(records[0][1].fileno(), -1)
            finally:
                for _, _, reset in records: reset()

    def test_assembly_nonretryable_errors_reach_cli_from_claim_and_run(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        from backend.waveforms import consumer, __main__ as cli
        from backend.waveforms.assembly import Assembler
        from tests.waveform_stack import runtime_config
        from tests.test_waveform_repository import observation
        repo = WaveformRepository(self.connect, limits=LIMITS, prefix="oil", ingress_gateway_id="GW")
        for payload in capture(): repo.ingest(observation(payload))
        config = runtime_config(self, "assemble")
        run = consumer.run_assemble
        def bounded(config, stop):
            timer = threading.Timer(2, stop.set)  # Bounds the known pre-fix endless retry.
            timer.start()
            try: return run(config, stop)
            finally:
                timer.cancel()
                timer.join()
        for method in ("claim", "run"):
            for code in ("waveform.invariant", "waveform.capacity_configuration"):
                with self.subTest(method=method, code=code):
                    # Run injection needs a real committed claim each time.
                    with self.connect("migration_owner") as connection:
                        connection.execute("UPDATE waveforms.assembly_jobs SET lease_token=NULL,lease_until=NULL,lease_generation=NULL")
                    with patch.object(Assembler, method, side_effect=WaveformError(code, "private detail")), patch.object(cli, "load_waveform_config", return_value=config), patch.object(cli, "run_assemble", side_effect=bounded), redirect_stdout(StringIO()) as output:
                        result = cli.main(["--config", "injected-config", "assemble"])
                    self.assertEqual(result, 2)
                    self.assertEqual(json.loads(output.getvalue()), {"status": "unavailable", "code": code})

    def test_assembly_unavailable_retries_then_completes(self):
        from unittest.mock import patch
        from backend.waveforms.consumer import run_assemble
        from backend.waveforms.assembly import Assembler
        from tests.waveform_stack import runtime_config
        from tests.test_waveform_repository import observation
        repo = WaveformRepository(self.connect, limits=LIMITS, prefix="oil", ingress_gateway_id="GW")
        for payload in capture(): repo.ingest(observation(payload))
        stop, events = threading.Event(), []
        claim, attempts = Assembler.claim, []
        def transient(assembler, *args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1: raise WaveformError("waveform.unavailable", "transient")
            return claim(assembler, *args, **kwargs)
        def event(name, **fields):
            events.append((name, fields))
            if name == "completed": stop.set()
        timer = threading.Timer(8, stop.set)
        timer.start()
        try:
            with patch.object(Assembler, "claim", transient):
                run_assemble(runtime_config(self, "assemble"), stop, on_event=event)
        finally:
            timer.cancel()
            timer.join()
        self.assertEqual([name for name, _ in events], ["retry", "completed"])
        self.assertEqual(events[0][1]["code"], "waveform.unavailable")
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.completion", role="assembler"), 1)

    def test_blocked_socket_disposal_obeys_shared_shutdown_deadline(self):
        from unittest.mock import patch
        from backend.waveforms import consumer
        from backend.waveforms.config import load_waveform_config
        from tests.waveform_stack import Stack
        import os
        stop, ready, closing, release = (threading.Event() for _ in range(4))
        records, clients = [], []
        create = consumer.create_client
        def traced(gateway):
            client = create(gateway)
            clients.append(client)
            reset, disconnect, send = client._reset_sockets, client.disconnect, client._sock_send
            def blocked(data):
                if data[0] == 0xe0: raise BlockingIOError()
                return send(data)
            def blocked_reset():
                closing.set()
                release.wait(5)
                reset()
            def disconnect_traced():
                records.append((client.socket(), client._sockpairR, client._sockpairW))
                client._reset_sockets = blocked_reset
                return disconnect()
            client._sock_send, client.disconnect = blocked, disconnect_traced
            return client
        with Stack(self) as stack, patch.dict(os.environ, {"TEST_WAVEFORM_MQTT": stack.broker.password}):
            config = load_waveform_config(stack.config, component="receive")
            connections = consumer.DatabaseConnections(config, stop)
            pool = consumer._OwnedPool(1, "waveform-socket-test")
            with patch.object(consumer, "create_client", side_effect=traced):
                future = pool.submit(consumer._receive_gateway, config, config.gateways[0],
                    connections.factory("receiver"), stop,
                    lambda event, **_: ready.set() if event == "subscribed" else None)
                try:
                    self.assertTrue(ready.wait(5))
                    started = time.monotonic()
                    with self.assertRaises(WaveformError) as caught:
                        consumer._shutdown(pool, [future], connections, stop, timeout=.3)
                    self.assertEqual(caught.exception.code, "waveform.shutdown_incomplete")
                    self.assertLess(time.monotonic() - started, 1)
                    self.assertTrue(closing.is_set())
                    self.assertTrue(any(owner.is_alive() for owner in pool.owned_threads()))
                    self.assertEqual(len(clients), 1, "replacement must not start during disposal")
                finally:
                    release.set()
                    consumer._shutdown(pool, [future], connections, stop, timeout=5)
                    for client in clients: client._reset_sockets()
                future.result()
                self.assertEqual([[sock.fileno() for sock in sockets] for sockets in records], [[-1, -1, -1]])


if __name__ == "__main__": unittest.main()
