"""Protocol confirmation is downstream of proven application persistence."""

from datetime import datetime
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from backend.ingest_reliability.contracts import CommittedResult
from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_vectors import FIXED_TIME, observation
from tests.ingest_support import IngestDatabaseCase, LIMITS


def message(*, qos=1, retain=False, dup=False):
    obs = observation()
    return SimpleNamespace(topic=obs.topic, payload=obs.payload, qos=qos, retain=retain, dup=dup, mid=7)


class ReceiveWindowTests(unittest.TestCase):
    def setUp(self):
        from backend.center.mqtt_consumer import connect_persistent_session
        self.connect = connect_persistent_session
        self.client = Mock()
        self.client.connect.return_value = 0
        self.config = SimpleNamespace(host="127.0.0.1", port=1883, keepalive_seconds=30)

    def test_new_receive_window_is_encoded_in_connect_properties(self):
        self.assertEqual(self.connect(self.client, self.config, 604800, receive_maximum=7), 0)
        properties = self.client.connect.call_args.kwargs["properties"]
        self.assertEqual(properties.ReceiveMaximum, 7)
        self.assertEqual(properties.SessionExpiryInterval, 604800)

    def test_bad_window_is_rejected_before_connect_and_legacy_properties_are_unchanged(self):
        from backend.center.mqtt_consumer import MqttConsumerError
        for value in (True, 0, 1025, "20"):
            with self.subTest(value=value), self.assertRaises(MqttConsumerError):
                self.connect(self.client, self.config, 604800, receive_maximum=value)
        self.client.connect.assert_not_called()
        self.connect(self.client, self.config, 604800)
        self.assertIsNone(getattr(self.client.connect.call_args.kwargs["properties"], "ReceiveMaximum", None))


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.consumer_type = importlib.import_module("backend.ingest_reliability.consumer").ReliableConsumer
        self.client = Mock()
        self.client.ack.return_value = 0

    def test_persistence_precedes_manual_ack_and_keeps_all_transport_observations(self):
        events = []
        result = CommittedResult(uuid4(), "stored", uuid4(), False)
        repository = Mock()
        repository.ingest.side_effect = lambda obs: events.append(("committed", obs)) or result
        self.client.ack.side_effect = lambda mid, qos: events.append(("ack", mid, qos)) or 0
        returned = self.consumer_type(repository, clock=lambda: FIXED_TIME).handle_message(self.client, message(retain=True, dup=True))
        self.assertIs(returned, result)
        self.assertEqual([item[0] for item in events], ["committed", "ack"])
        obs = events[0][1]
        self.assertEqual((obs.qos, obs.retain, obs.dup, obs.received_at), (1, True, True, FIXED_TIME))
        self.assertEqual(obs.payload, observation().payload)

    def test_repository_failure_never_acknowledges_or_converts_the_failure_to_quarantine(self):
        for error in (OSError("injected storage failure"), RuntimeError("programming error"),
                      ReliabilityError("ingest.capacity_exhausted", "capacity")):
            repository = Mock()
            repository.ingest.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                self.consumer_type(repository).handle_message(self.client, message())
            self.client.ack.assert_not_called()
            self.assertEqual(repository.ingest.call_count, 1)

    def test_quarantined_commit_is_acknowledged_and_does_not_block_the_next_good_message(self):
        repository = Mock()
        repository.ingest.side_effect = [CommittedResult(uuid4(), "quarantined", None, False),
                                         CommittedResult(uuid4(), "stored", uuid4(), False)]
        consumer = self.consumer_type(repository)
        self.assertEqual(consumer.handle_message(self.client, message()).status, "quarantined")
        self.assertEqual(consumer.handle_message(self.client, message()).status, "stored")
        self.assertEqual(self.client.ack.call_count, 2)

    def test_qos_zero_is_observed_but_has_no_protocol_ack_to_send(self):
        repository = Mock()
        repository.ingest.return_value = CommittedResult(uuid4(), "quarantined", uuid4(), False)
        result = self.consumer_type(repository).handle_message(self.client, message(qos=0))
        self.assertEqual(result.status, "quarantined")
        self.assertEqual(repository.ingest.call_args.args[0].qos, 0)
        self.client.ack.assert_not_called()

    def test_naive_or_invalid_server_clock_never_reaches_the_repository(self):
        repository = Mock()
        for value in (datetime(2026, 9, 24), None, "not-a-clock"):
            with self.subTest(value=value), self.assertRaises(ReliabilityError):
                self.consumer_type(repository, clock=lambda: value).handle_message(self.client, message())
        repository.ingest.assert_not_called()
        self.client.ack.assert_not_called()

    def test_failed_ack_after_commit_remains_a_transport_error(self):
        repository = Mock()
        repository.ingest.return_value = CommittedResult(uuid4(), "stored", uuid4(), False)
        self.client.ack.return_value = 4
        with self.assertRaises(ReliabilityError) as caught:
            self.consumer_type(repository).handle_message(self.client, message())
        self.assertEqual(caught.exception.code, "ingest.ack_failed")
        repository.ingest.assert_called_once()

    def test_missing_committed_result_is_never_interpreted_as_success(self):
        repository = Mock()
        repository.ingest.return_value = None
        with self.assertRaises(ReliabilityError):
            self.consumer_type(repository).handle_message(self.client, message())
        self.client.ack.assert_not_called()

    def test_existing_paho_boundary_disconnects_on_failure_without_logging_payload(self):
        from backend.center.mqtt_consumer import create_paho_client
        from backend.gateway.mqtt_uplink import MqttConfig
        repository = Mock()
        repository.ingest.side_effect = ReliabilityError("ingest.storage_unavailable", "storage")
        client = create_paho_client(MqttConfig(host="127.0.0.1", port=1883, client_id="pure-test", username="test", password="test-only"),
                                    self.consumer_type(repository))
        with patch.object(client, "disconnect") as disconnect, patch.object(client, "ack") as ack:
            client.on_message(client, None, message())
        disconnect.assert_called_once()
        ack.assert_not_called()


class StartupTests(unittest.TestCase):
    def setUp(self):
        from backend.center import __main__ as center_main
        from backend.gateway.mqtt_uplink import MqttConfig
        self.main = center_main
        self.config = SimpleNamespace(
            topic_prefix="oilfield", mqtt_session_expiry_seconds=604800,
            mqtt=MqttConfig(host="127.0.0.1", port=1883, client_id="startup-test", username="test", password="test-only"),
            postgres={"host": "127.0.0.1", "port": 3822, "dbname": "oilwell_test", "user": "reliable_ingestor"},
            reliability=SimpleNamespace(enabled=True, receipt_gateways=frozenset({"GW"}), limits=LIMITS,
                                        postgres_ingest={"host": "127.0.0.1", "port": 3822, "dbname": "oilwell_test",
                                                         "user": "reliable_ingestor", "password": "not-real"}))

    def invoke(self, *, arguments=(), probe_error=None, limits=None, subscription_code=1):
        from contextlib import ExitStack
        events = []
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.execute.return_value.fetchone.return_value = limits or (LIMITS.total_bytes, LIMITS.quarantine_bytes)
        client = Mock()
        client.subscribe.return_value = (0, 17)
        def session(*_args, **_kwargs):
            client.on_connect(client, None, None, SimpleNamespace(is_failure=False), None)
            client.on_subscribe(client, None, 17, [SimpleNamespace(value=subscription_code)], None)
            raise KeyboardInterrupt
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.main, "load_center_config", return_value=self.config))
            probe = stack.enter_context(patch.object(self.main, "probe_role", create=True, side_effect=probe_error))
            connect = stack.enter_context(patch("psycopg.connect", return_value=connection))
            create = stack.enter_context(patch.object(self.main, "create_paho_client", return_value=client))
            stack.enter_context(patch.object(self.main, "run_persistent_session", side_effect=session))
            stack.enter_context(patch.object(self.main, "_event", side_effect=lambda name, **fields: events.append((name, fields))))
            migrate = stack.enter_context(patch.object(self.main.PostgresTelemetryRepository, "migrate"))
            result = self.main.main(["--config", "not-a-real-config.toml", *arguments])
        return SimpleNamespace(result=result, probe=probe, connect=connect, create=create, client=client,
                               connection=connection, events=events, migrate=migrate)

    def test_enabled_startup_uses_only_ingestor_preflight_and_retains_publish_flags(self):
        result = self.invoke()
        self.assertEqual(result.result, 0)
        result.probe.assert_called_once()
        self.assertEqual(result.probe.call_args.args[1], "ingestor")
        consumer_type = importlib.import_module("backend.ingest_reliability.consumer").ReliableConsumer
        self.assertIsInstance(result.create.call_args.args[1], consumer_type)
        options = result.client.subscribe.call_args.kwargs["options"]
        self.assertEqual(options.QoS, 1)
        self.assertTrue(options.retainAsPublished)
        result.migrate.assert_not_called()

    def test_check_requires_schema_permissions_and_matching_capacity_without_opening_mqtt(self):
        result = self.invoke(arguments=("--check",))
        self.assertEqual(result.result, 0)
        result.probe.assert_called_once()
        result.create.assert_not_called()
        result.migrate.assert_not_called()

    def test_missing_schema_or_unsafe_permissions_prevent_startup_and_are_not_hidden(self):
        for code in ("ingest.schema_missing", "ingest.privilege_unsafe", "ingest.schema_incomplete"):
            with self.subTest(code=code):
                result = self.invoke(probe_error=ReliabilityError(code, "safe error"))
                self.assertNotEqual(result.result, 0)
                result.create.assert_not_called()
                result.migrate.assert_not_called()
                self.assertIn(code, str(result.events))
                self.assertNotIn("not-real", str(result.events))

    def test_capacity_mismatch_is_rejected_without_repairing_the_database(self):
        result = self.invoke(limits=(1, 1))
        self.assertNotEqual(result.result, 0)
        result.create.assert_not_called()
        sql_text = str(result.connection.execute.call_args_list).lower()
        self.assertNotIn("update ", sql_text)
        self.assertNotIn("create table", sql_text)

    def test_reliable_mode_does_not_use_runtime_credentials_for_migration(self):
        result = self.invoke(arguments=("--migrate",))
        self.assertNotEqual(result.result, 0)
        result.migrate.assert_not_called()
        result.create.assert_not_called()

    def test_rejected_subscription_never_reports_ready(self):
        result = self.invoke(subscription_code=128)
        self.assertNotIn("mqtt_subscribed", [name for name, _ in result.events])
        self.assertIn("mqtt_subscribe_rejected", [name for name, _ in result.events])
        self.assertGreaterEqual(result.client.disconnect.call_count, 1)

    def test_legacy_config_continues_to_use_legacy_consumer_without_new_db_preflight(self):
        from backend.center.mqtt_consumer import MqttConsumer
        self.config.reliability = None
        result = self.invoke()
        self.assertEqual(result.result, 0)
        result.probe.assert_not_called()
        self.assertIsInstance(result.create.call_args.args[1], MqttConsumer)
        self.assertEqual(result.client.subscribe.call_args.kwargs, {"qos": 1})


class LiveStartupTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        from pathlib import Path
        import socket
        import tempfile
        from tests.test_ingest_config import PG_SECTIONS, configuration
        temporary = tempfile.TemporaryDirectory(prefix="ingest-startup-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "center.toml"
        self.raw = configuration()
        reserved = socket.socket()
        reserved.bind(("127.0.0.1", 0))
        self.addCleanup(reserved.close)
        self.raw["mqtt"]["port"] = reserved.getsockname()[1]
        self.raw["mqtt_receipt_publisher"]["port"] = self.raw["mqtt"]["port"]
        self.raw["postgres"]["database"] = self.settings["ingestor"]["dbname"]
        for role, section in PG_SECTIONS.items():
            self.raw[section] = dict(self.raw_config["postgres_" + role])

    def run_check(self, flag="--check"):
        import os
        import subprocess
        import sys
        from tests.test_ingest_config import write_configuration
        write_configuration(self.path, self.raw)
        result = subprocess.run([sys.executable, "-m", "backend.center", "--config", str(self.path), flag],
                                env=os.environ | {"INCOMING_MQTT": "unused-loopback-test-secret"},
                                text=True, capture_output=True, timeout=20)
        self.assertTrue(all(section["password"] not in result.stdout + result.stderr for section in self.settings.values()),
                        "startup output exposed a database credential")
        return result

    def test_check_passes_against_real_schema_and_role_without_starting_network_client(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"configuration_valid"', result.stdout)
        self.assertNotIn('"mqtt_subscribed"', result.stdout)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.facts", role="migrator"), 0)

    def test_check_refuses_actual_privilege_drift_without_silently_revoking_it(self):
        from psycopg import sql
        name = sql.Identifier(self.settings["ingestor"]["user"])
        with self.connect("migrator") as conn:
            conn.execute(sql.SQL("GRANT UPDATE(raw_payload) ON ingest_reliability.facts TO {}").format(name))
            conn.commit()
            try:
                result = self.run_check()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ingest.privilege_unsafe", result.stdout)
                self.assertTrue(self.scalar("SELECT has_column_privilege(current_user,'ingest_reliability.facts','raw_payload','UPDATE')",
                                            role="ingestor"))
            finally:
                conn.execute(sql.SQL("REVOKE UPDATE(raw_payload) ON ingest_reliability.facts FROM {}").format(name))
                # A failing assertion must not roll back restoration of the
                # deliberately introduced privilege drift.
                conn.commit()

    def test_check_refuses_capacity_mismatch_without_changing_persisted_configuration(self):
        self.raw["reliability"]["total_bytes"] += 1
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ingest.capacity_mismatch", result.stdout)
        self.assertEqual(self.scalar("SELECT total_bytes FROM ingest_reliability.capacity", role="migrator"), LIMITS.total_bytes)

    def test_runtime_migrate_flag_is_rejected_before_any_ddl(self):
        history = self.guard.execute('SELECT * FROM ingest_reliability.schema_migrations ORDER BY version').fetchall()
        result = self.run_check("--migrate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ingest.explicit_migration_required", result.stdout)
        self.assertEqual(self.guard.execute('SELECT * FROM ingest_reliability.schema_migrations ORDER BY version').fetchall(), history)
