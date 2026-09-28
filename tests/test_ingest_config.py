"""Component-scoped credentials and explicit, bounded reliability settings."""

from copy import deepcopy
import importlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.ingest_reliability.errors import ReliabilityError
from backend.secret_store import StoredCredential


PG_SECTIONS = {"ingestor": "postgres_reliable_ingest", "publisher": "postgres_receipt_publisher",
               "reader": "postgres_ingest_reader", "operator": "postgres_ingest_operator"}


def configuration():
    result = {
        "center": {"topic_prefix": "oilfield"},
        "reliability": {"enabled": True, "receipt_gateways": ["GW"], "total_bytes": 16777216,
                        "quarantine_bytes": 8388608, "allowed_origins": ["http://localhost:80", "https://example.test:443"]},
        "postgres": {"host": "127.0.0.1", "port": 3822, "database": "oilwell_test",
                     "user": "legacy_ingestor", "password_env": "UNUSED_OLD_POSTGRES", "sslmode": "disable"},
        "mqtt": {"host": "127.0.0.1", "port": 1883, "client_id": "center-ingest", "username": "center-ingest",
                 "password_env": "INCOMING_MQTT"},
        "mqtt_receipt_publisher": {"host": "127.0.0.1", "port": 1883, "client_id": "center-receipts",
                                   "username": "center-receipts", "password_env": "RECEIPT_MQTT"},
    }
    for role, section in PG_SECTIONS.items():
        result[section] = {"host": "127.0.0.1", "port": 3822, "database": "oilwell_test",
                           "user": "reliable_" + role, "password_env": role.upper() + "_PG", "sslmode": "disable"}
    return result


def write_configuration(path, raw):
    path.write_text("\n\n".join("[" + section + "]\n" + "\n".join(
        key + " = " + json.dumps(value, ensure_ascii=False) for key, value in values.items())
        for section, values in raw.items()) + "\n", encoding="utf-8")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ingest-config-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "center.toml"
        self.raw = configuration()
        self.loader = importlib.import_module("backend.ingest_reliability.config").load_reliability_config

    def load(self, component="ingestor", *, env=None, raw=None, credential_reader=None):
        write_configuration(self.path, self.raw if raw is None else raw)
        return self.loader(self.path, component=component,
                           env={"INGESTOR_PG": "ingest-secret"} if env is None else env,
                           credential_reader=credential_reader)

    def test_disabled_or_absent_feature_does_not_resolve_any_secrets(self):
        def forbidden(_name):
            self.fail("disabled feature must not resolve credentials")
        raw = deepcopy(self.raw)
        raw.pop("reliability")
        self.assertIsNone(self.load(raw=raw, env={}, credential_reader=forbidden))
        raw["reliability"] = {"enabled": False}
        self.assertIsNone(self.load(raw=raw, env={}, credential_reader=forbidden))

    def test_ingestor_resolves_only_its_own_role_and_hides_secrets_in_repr(self):
        config = self.load()
        self.assertTrue(config.enabled)
        self.assertEqual(config.receipt_gateways, frozenset({"GW"}))
        self.assertEqual(config.limits.total_bytes, 16777216)
        self.assertEqual(config.postgres_ingest["password"], "ingest-secret")
        self.assertIsNone(config.postgres_publisher)
        self.assertIsNone(config.postgres_reader)
        self.assertIsNone(config.postgres_operator)
        self.assertIsNone(config.mqtt_publisher)
        self.assertNotIn("ingest-secret", repr(config))

    def test_center_loader_in_enabled_mode_does_not_require_old_ingestor_credentials(self):
        from backend.center.config import load_center_config
        write_configuration(self.path, self.raw)
        config = load_center_config(self.path, env={"INGESTOR_PG": "new-pg-secret", "INCOMING_MQTT": "incoming-secret"})
        self.assertTrue(config.reliability.enabled)
        self.assertEqual(config.postgres["user"], "reliable_ingestor")
        self.assertNotIn("new-pg-secret", repr(config))

    def test_unresolved_web_secrets_and_bad_origins_do_not_break_ingestion(self):
        self.raw["reliability"]["allowed_origins"] = ["*"]
        self.raw["postgres_query"] = {"password_env": "UNRESOLVED_UNUSED_QUERY"}
        self.raw["postgres_ingest_reader"]["password_credential"] = "unresolved-reader"
        self.assertIsNotNone(self.load())

    def test_only_required_role_sections_need_to_be_present(self):
        self.raw.pop("postgres_receipt_publisher")
        self.raw.pop("postgres_ingest_reader")
        self.raw.pop("postgres_ingest_operator")
        self.raw.pop("mqtt_receipt_publisher")
        self.assertIsNotNone(self.load())
        with self.assertRaises(ReliabilityError):
            self.load("publisher", env={})

    def test_publisher_resolves_only_publisher_database_and_mqtt_secrets(self):
        config = self.load("publisher", env={"PUBLISHER_PG": "publisher-secret", "RECEIPT_MQTT": "receipt-secret"})
        self.assertEqual(config.postgres_publisher["user"], "reliable_publisher")
        self.assertEqual(config.mqtt_publisher.password, "receipt-secret")
        self.assertIsNone(config.postgres_ingest)
        self.assertIsNone(config.postgres_reader)
        self.assertNotIn("publisher-secret", repr(config))
        self.assertNotIn("receipt-secret", repr(config))

    def test_management_requires_two_distinct_roles_and_canonical_exact_origins(self):
        config = self.load("management", env={"READER_PG": "read-secret", "OPERATOR_PG": "operate-secret"})
        self.assertEqual(config.allowed_origins, ("http://localhost", "https://example.test"))
        self.assertIsNone(config.postgres_ingest)
        self.assertIsNone(config.mqtt_publisher)
        self.assertEqual(config.postgres_reader["user"], "reliable_reader")
        self.assertEqual(config.postgres_operator["user"], "reliable_operator")
        self.assertNotIn("operate-secret", repr(config))
        self.raw["reliability"]["allowed_origins"] = ["*"]
        with self.assertRaises(ReliabilityError):
            self.load("management", env={"READER_PG": "read-secret", "OPERATOR_PG": "operate-secret"})

    def test_unknown_component_and_invalid_enabled_type_are_errors_not_disabled(self):
        with self.assertRaises(ReliabilityError):
            self.load("administrator")
        for enabled in ("true", 1, [], "false"):
            self.raw["reliability"]["enabled"] = enabled
            with self.subTest(enabled=enabled), self.assertRaises(ReliabilityError):
                self.load()

    def test_budgets_are_positive_bounded_integers_with_quarantine_subset(self):
        for changes in ({"total_bytes": 0}, {"total_bytes": True}, {"total_bytes": 2**63},
                        {"quarantine_bytes": -1}, {"quarantine_bytes": 16777217}, {"total_bytes": 1.5}):
            raw = deepcopy(self.raw)
            raw["reliability"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ReliabilityError):
                self.load(raw=raw)

    def test_gateway_allowlist_is_explicit_validated_and_defaults_empty(self):
        self.raw["reliability"].pop("receipt_gateways")
        self.assertEqual(self.load().receipt_gateways, frozenset())
        for gateways in ("GW", ["../other"], [True], ["GW", "GW"], [""]):
            self.raw["reliability"]["receipt_gateways"] = gateways
            with self.subTest(gateways=gateways), self.assertRaises(ReliabilityError):
                self.load()

    def test_configured_pg_endpoints_and_identities_cannot_diverge(self):
        for changes in ({"port": 5432}, {"host": "localhost"}, {"database": "different"},
                        {"user": "reliable_ingestor"}):
            raw = deepcopy(self.raw)
            raw["postgres_receipt_publisher"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ReliabilityError):
                self.load(raw=raw)

    def test_remote_database_requires_full_hostname_and_ca_verification(self):
        for section in ("postgres", *PG_SECTIONS.values()):
            self.raw[section]["host"] = "db.example.test"
        with self.assertRaises(ReliabilityError):
            self.load()
        self.raw["postgres_reliable_ingest"].update(sslmode="verify-full", sslrootcert="certs/test-ca.pem")
        self.assertEqual(self.load().postgres_ingest["sslmode"], "verify-full")

    def test_required_secrets_cannot_be_inline_empty_unresolved_or_wrong_user(self):
        for secret in ({}, {"INGESTOR_PG": ""}, {"INGESTOR_PG": "\x00"}):
            with self.subTest(secret_names=tuple(secret)), self.assertRaises(ReliabilityError):
                self.load(env=secret)
        raw = deepcopy(self.raw)
        raw["postgres_reliable_ingest"]["password"] = "do-not-print-this"
        with self.assertRaises(ReliabilityError) as caught:
            self.load(raw=raw)
        self.assertNotIn("do-not-print-this", str(caught.exception))
        self.raw["postgres_reliable_ingest"].pop("password_env")
        self.raw["postgres_reliable_ingest"]["password_credential"] = "ingestor-entry"
        with self.assertRaises(ReliabilityError):
            self.load(env={}, credential_reader=lambda _: StoredCredential("different-user", "private"))

    def test_only_the_needed_windows_credential_is_requested(self):
        self.raw["postgres_reliable_ingest"].pop("password_env")
        self.raw["postgres_reliable_ingest"]["password_credential"] = "ingestor-entry"
        calls = []
        def read(target):
            calls.append(target)
            return StoredCredential("reliable_ingestor", "windows-secret")
        config = self.load(env={}, credential_reader=read)
        self.assertEqual(calls, ["ingestor-entry"])
        self.assertEqual(config.postgres_ingest["password"], "windows-secret")

    def test_publisher_identity_and_remote_tls_cannot_be_reused_or_downgraded(self):
        env = {"PUBLISHER_PG": "publisher-secret", "RECEIPT_MQTT": "receipt-secret"}
        for changes in ({"username": "center-ingest"}, {"client_id": "center-ingest"}, {"host": "mqtt.example.test"}):
            raw = deepcopy(self.raw)
            raw["mqtt_receipt_publisher"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ReliabilityError):
                self.load("publisher", raw=raw, env=env)

    def test_component_thresholds_reject_booleans_zero_and_unbounded_values(self):
        for field in ("publish_batch_size", "lease_seconds", "retry_max_seconds", "health_stale_seconds"):
            for value in (True, 0, 10**9):
                raw = deepcopy(self.raw)
                raw["reliability"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ReliabilityError):
                    self.load("publisher", raw=raw, env={"PUBLISHER_PG": "p", "RECEIPT_MQTT": "m"})

    def test_ingestor_receive_window_is_bounded_and_does_not_accept_boolean_numbers(self):
        self.assertEqual(self.load().receive_maximum, 20)
        for value in (True, 0, 1025, 2.5):
            self.raw["reliability"]["receive_maximum"] = value
            with self.subTest(value=value), self.assertRaises(ReliabilityError):
                self.load()

    def test_enabled_ingestion_rejects_unbounded_or_boolean_mqtt_timeouts_and_ports(self):
        for field, value in (("port", True), ("keepalive_seconds", True), ("connect_timeout_seconds", 10**9),
                             ("publish_timeout_seconds", 0), ("connect_timeout_seconds", True)):
            raw = deepcopy(self.raw)
            raw["mqtt"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ReliabilityError):
                self.load(raw=raw)

    def test_unreadable_or_malformed_configuration_is_not_disabled(self):
        for content in ("[reliability\n", "reliability = 42\n"):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(ReliabilityError):
                self.loader(self.path, component="ingestor", env={})
