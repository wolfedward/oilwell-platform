"""Explicit independent credentials; disabled registry has no database side effects."""
import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.registry.errors import RegistryError
from backend.secret_store import StoredCredential


class RegistryConfigTests(unittest.TestCase):
    def setUp(self):
        try:
            self.config = importlib.import_module("backend.registry.config")
            self.runtime = importlib.import_module("backend.registry.runtime")
        except ModuleNotFoundError as error:
            if error.name not in {"backend.registry.config", "backend.registry.runtime"}:
                raise
            self.fail("Independent registry runtime configuration has not been implemented")
        self.temp = tempfile.TemporaryDirectory(prefix="registry-config-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.path = self.root / "config/center.local.toml"
        self.base = {
            "registry": {"enabled": True, "allowed_origins": ["http://127.0.0.1:8765"]},
            "postgres": {"host": "127.0.0.1", "port": 3822, "database": "oilwell_registry_test", "user": "ingestor", "password_env": "NEVER_RESOLVE_INGEST"},
            "postgres_registry": {"host": "127.0.0.1", "port": 3822, "database": "oilwell_registry_test", "user": "writer", "password_env": "WRITER_PASSWORD", "sslmode": "disable"},
            "postgres_query": {"host": "127.0.0.1", "port": 3822, "database": "oilwell_registry_test", "user": "reader", "password_env": "READER_PASSWORD", "sslmode": "disable"},
        }
        self.env = {"WRITER_PASSWORD": "writer-test-secret", "READER_PASSWORD": "reader-test-secret"}

    def write(self, raw=None):
        raw = self.base if raw is None else raw
        source = "\n".join("[" + section + "]\n" + "\n".join(key + " = " + json.dumps(value, ensure_ascii=False)
                              for key, value in body.items()) for section, body in raw.items())
        self.path.write_text(source, encoding="utf-8")
        return self.path

    def load(self, raw=None, **options):
        return self.config.load_registry_config(self.write(raw), env=self.env, **options)

    def test_absent_and_disabled_registry_sections_never_resolve_credentials(self):
        for raw in ({}, {"registry": {}}, {"registry": {"enabled": False}, "postgres_registry": {"password_credential": "must-not-read"}}):
            called = []
            def credential_reader(target):
                called.append(target)
                raise AssertionError("Disabled configuration read a credential")
            self.assertIsNone(self.load(raw, credential_reader=credential_reader))
            self.assertEqual(called, [])

    def test_enabled_configuration_keeps_distinct_secrets_private_and_times_bounded(self):
        config = self.load()
        self.assertEqual((config.writer["user"], config.reader["user"]), ("writer", "reader"))
        self.assertEqual((config.writer["password"], config.reader["password"]), (self.env["WRITER_PASSWORD"], self.env["READER_PASSWORD"]))
        self.assertEqual((config.writer["connect_timeout"], config.reader["connect_timeout"]), (3, 3))
        self.assertEqual(config.allowed_origins, ("http://127.0.0.1:8765",))
        self.assertEqual((config.online_seconds, config.delayed_seconds, config.clock_tolerance_seconds), (120, 900, 300))
        self.assertNotIn("writer-test-secret", repr(config))
        self.assertNotIn("reader-test-secret", repr(config))

    def test_no_query_identity_fallback_when_registry_is_enabled(self):
        raw = copy.deepcopy(self.base)
        raw.pop("postgres_query")
        with self.assertRaises(RegistryError) as caught:
            self.load(raw)
        self.assertEqual(caught.exception.code, "registry.query_identity_required")

    def test_missing_writer_or_center_identity_is_not_a_usable_configuration(self):
        for section in ("postgres_registry", "postgres"):
            raw = copy.deepcopy(self.base)
            raw.pop(section)
            with self.subTest(section=section), self.assertRaises(RegistryError):
                self.load(raw)

    def test_all_database_coordinates_must_match_without_dns_assumptions(self):
        for section in ("postgres_registry", "postgres_query"):
            for key, value in (("host", "localhost"), ("port", 5432), ("database", "another_database")):
                raw = copy.deepcopy(self.base)
                raw[section][key] = value
                with self.subTest(section=section, key=key), self.assertRaises(RegistryError) as caught:
                    self.load(raw)
                self.assertEqual(caught.exception.code, "registry.database_mismatch")

    def test_ingestor_writer_and_reader_usernames_must_be_distinct(self):
        for section, other in (("postgres_registry", "postgres"), ("postgres_query", "postgres"), ("postgres_query", "postgres_registry")):
            raw = copy.deepcopy(self.base)
            raw[section]["user"] = raw[other]["user"]
            with self.subTest(section=section, other=other), self.assertRaises(RegistryError):
                self.load(raw)

    def test_inline_passwords_are_rejected_even_for_unresolved_ingest_identity(self):
        for section in ("postgres", "postgres_registry", "postgres_query"):
            raw = copy.deepcopy(self.base)
            raw[section]["password"] = "inline-test-secret"
            with self.subTest(section=section), self.assertRaises(RegistryError) as caught:
                self.load(raw)
            self.assertNotIn("inline-test-secret", str(caught.exception))

    def test_passwords_use_explicit_environment_or_os_credential_references(self):
        raw = copy.deepcopy(self.base)
        raw["postgres_registry"].pop("password_env")
        raw["postgres_registry"]["password_credential"] = "test-writer-reference"
        calls = []
        def lookup(target):
            calls.append(target)
            return StoredCredential("writer", "credential-test-secret")
        config = self.load(raw, credential_reader=lookup)
        self.assertEqual(calls, ["test-writer-reference"])
        self.assertEqual(config.writer["password"], "credential-test-secret")
        with self.assertRaises(RegistryError):
            self.load(raw, credential_reader=lambda _: StoredCredential("different-user", "secret"))
        raw["postgres_registry"]["password_env"] = "WRITER_PASSWORD"
        with self.assertRaises(RegistryError):
            self.load(raw, credential_reader=lookup)

    def test_remote_connections_require_verified_tls_and_preserve_ca_path(self):
        raw = copy.deepcopy(self.base)
        for section in ("postgres", "postgres_registry", "postgres_query"):
            raw[section]["host"] = "db.example.test"
        with self.assertRaises(RegistryError):
            self.load(raw)
        for section in ("postgres_registry", "postgres_query"):
            raw[section].update(sslmode="verify-full", sslrootcert="certs/test-ca.pem")
        config = self.load(raw)
        for identity in (config.writer, config.reader):
            self.assertEqual(identity["sslmode"], "verify-full")
            self.assertEqual(Path(identity["sslrootcert"]), self.root / "certs/test-ca.pem")

    def test_bool_and_float_ports_and_malformed_enabled_values_are_rejected(self):
        for value in (True, 3822.0, "3822", 0, 65536):
            raw = copy.deepcopy(self.base)
            for section in ("postgres", "postgres_registry", "postgres_query"):
                raw[section]["port"] = value
            with self.subTest(value=value), self.assertRaises(RegistryError):
                self.load(raw)
        for value in (1, "true", "false"):
            raw = copy.deepcopy(self.base)
            raw["registry"]["enabled"] = value
            with self.subTest(value=value), self.assertRaises(RegistryError):
                self.load(raw)

    def test_time_thresholds_require_ordered_bounded_integers(self):
        for key, value in (("online_seconds", True), ("online_seconds", 0), ("online_seconds", 1.5),
                           ("delayed_seconds", 100), ("clock_tolerance_seconds", -1), ("clock_tolerance_seconds", 1000000000)):
            raw = copy.deepcopy(self.base)
            raw["registry"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(RegistryError):
                self.load(raw)

    def test_database_options_cannot_override_bounds_or_pass_malformed_credentials(self):
        for key, value in (("connect_timeout", 600), ("sslmode", "unknown"), ("password_env", ["WRITER_PASSWORD"])):
            raw = copy.deepcopy(self.base)
            raw["postgres_registry"][key] = value
            with self.subTest(key=key), self.assertRaises(RegistryError):
                self.load(raw)
        for value in ("", 123, {"password": "not-a-secret-string"}):
            self.env["WRITER_PASSWORD"] = value
            with self.subTest(value=value), self.assertRaises(RegistryError):
                self.load()

    def test_origin_whitelist_requires_exact_http_origin(self):
        for origins in ([], "http://127.0.0.1:8765", ["*"], ["null"], ["file:///tmp"], ["http://localhost:"],
                        ["http://name:password@localhost:8765"], ["http://localhost:8765/"], ["http://localhost:8765?x=1"],
                        ["http://localhost:8765#fragment"], ["http://localhost:0"], ["http://localhost:65536"]):
            raw = copy.deepcopy(self.base)
            raw["registry"]["allowed_origins"] = origins
            with self.subTest(origins=origins), self.assertRaises(RegistryError):
                self.load(raw)

    def test_default_ports_match_browser_serialization_and_deduplicate(self):
        raw = copy.deepcopy(self.base)
        raw["registry"]["allowed_origins"] = [
            "http://localhost", "http://LOCALHOST:80", "https://localhost", "https://LOCALHOST:443",
            "https://localhost:8443", "http://localhost:443", "https://localhost:80",
            "https://[0:0:0:0:0:0:0:1]:443", "https://[::1]", "http://[::1]:8765",
        ]
        self.assertEqual(self.load(raw).allowed_origins, (
            "http://localhost", "https://localhost", "https://localhost:8443",
            "http://localhost:443", "https://localhost:80", "https://[::1]", "http://[::1]:8765",
        ))

    def test_runtime_disabled_or_invalid_config_never_falls_back_to_other_credentials(self):
        path = self.write({"registry": {"enabled": False}})
        with patch.object(self.runtime, "probe_privileges", side_effect=AssertionError("Unexpected DB probe")):
            runtime = self.runtime.build_registry_runtime(path)
        self.assertEqual(runtime.status, "disabled")
        self.assertIsNone(runtime.service)
        self.assertIsNone(runtime.query)
        raw = copy.deepcopy(self.base)
        raw.pop("postgres_query")
        runtime = self.runtime.build_registry_runtime(self.write(raw))
        self.assertEqual((runtime.status, runtime.code), ("unavailable", "registry.query_identity_required"))
        self.assertIsNone(runtime.service)
        self.assertIsNone(runtime.query)

    def test_runtime_load_failure_is_isolated_and_does_not_print_secret_values(self):
        self.path.write_text('registry = "broken-test-password"\n', encoding="utf-8")
        runtime = self.runtime.build_registry_runtime(self.path)
        self.assertEqual(runtime.status, "unavailable")
        self.assertNotIn("broken-test-password", repr(runtime))
        self.assertIsNone(runtime.service)

    def test_missing_file_is_unavailable_but_no_config_path_is_disabled(self):
        self.assertEqual(self.runtime.build_registry_runtime(self.root / "missing.toml").status, "unavailable")
        self.assertEqual(self.runtime.build_registry_runtime(None).status, "disabled")
