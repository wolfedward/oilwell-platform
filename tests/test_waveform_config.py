"""Explicit component configuration: rejected identities and no unused secrets."""
from dataclasses import asdict
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.waveforms.errors import WaveformError
from tests.waveform_support import LIMITS


def config_sections():
    sections = {
        "waveforms": {"enabled": True, "topic_prefix": "oil", "assembly_workers": 2,
                      "max_db_connections": 4},
        "capacity.global": asdict(LIMITS.global_quota),
        "gateways.GW.quota": asdict(LIMITS.gateways["GW"]),
        "gateways.GW": {"source_ref": "ed1d031a-608e-437a-835d-88228f5955d4"},
    }
    for role in ("migration_owner", "receiver", "assembler", "receipt_publisher", "reader", "operator"):
        sections["postgres_" + role] = {"host": "127.0.0.1", "port": 3822,
            "database": "oilwell_waveforms_test", "user": "waveform_test_" + role,
            "password_env": "TEST_WAVE_" + role.upper(), "sslmode": "disable"}
    for role in ("receive", "publish", "sender"):
        sections["gateways.GW.mqtt_" + role] = {"host": "127.0.0.1", "port": 1883,
            "client_id": "waveform-GW-" + role, "username": "waveform-" + role,
            "password_env": "TEST_MQTT_" + role.upper(), "session_expiry_seconds": 0 if role == 'publish' else 3600,
            "keepalive_seconds": 10, "connect_timeout_seconds": 1}
    return sections


def toml_text(sections):
    return "\n\n".join("[" + name + "]\n" + "\n".join(
        key + " = " + json.dumps(value) for key, value in values.items())
        for name, values in sections.items()) + "\n"


class WaveformConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="waveform-config-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "waveforms.toml"
        self.sections = config_sections()
        self.environment = {"TEST_WAVE_RECEIVER": "x", "TEST_MQTT_RECEIVE": "x"}

    def load(self, component="receive", env=None):
        self.path.write_text(toml_text(self.sections), encoding="utf-8")
        module = importlib.import_module("backend.waveforms.config")
        self.assertTrue(hasattr(module, "load_waveform_config"), "component config loader is required")
        with patch.dict(os.environ, self.environment if env is None else env, clear=True):
            return module.load_waveform_config(self.path, component=component)

    def test_disabled_no_connections(self):
        self.sections = {"waveforms": {"enabled": False}}
        with patch("psycopg.connect", side_effect=AssertionError("disabled connection")):
            self.assertIsNone(self.load())

    def test_publisher_requires_zero_expiry_and_only_publisher_secrets(self):
        section=self.sections['gateways.GW.mqtt_publish']
        env={'TEST_WAVE_RECEIPT_PUBLISHER':'x','TEST_MQTT_PUBLISH':'x'}
        section['session_expiry_seconds']=0
        config=self.load('publish',env)
        self.assertEqual(set(config.postgres),{'receipt_publisher'})
        self.assertEqual(config.gateways[0].session_expiry_seconds,0)
        section['session_expiry_seconds']=3600
        with self.assertRaises(WaveformError): self.load('publish',env)

    def test_only_selected_credentials_are_resolved(self):
        config = self.load(env={"TEST_WAVE_RECEIVER": "private-value", "TEST_MQTT_RECEIVE": "private-mqtt"})
        self.assertEqual(set(config.postgres), {"receiver"})
        self.assertEqual(config.gateways[0].client_id, "waveform-GW-receive")
        self.assertNotIn("private", repr(config))
        assembly = self.load("assemble", {"TEST_WAVE_ASSEMBLER": "private-value"})
        self.assertEqual(set(assembly.postgres), {"assembler"})
        self.assertIsNone(assembly.gateways[0].mqtt)

    def test_nonloopback_requires_verified_tls(self):
        section = self.sections["gateways.GW.mqtt_receive"]
        section["host"] = "broker.example.test"
        base = dict(section)
        for changes in ({}, {"ca_cert": "ca.pem", "tls_insecure": True}, {"ca_cert": "ca.pem", "verify_certificate": False}):
            with self.subTest(changes=changes):
                section.clear()
                section.update(base | changes)
                with self.assertRaises(WaveformError):
                    self.load(env={"TEST_WAVE_RECEIVER": "x", "TEST_MQTT_RECEIVE": "x"})
        self.sections = config_sections()
        for name, values in self.sections.items():
            if name.startswith("postgres_"): values["host"] = "postgres.example.test"
        with self.assertRaises(WaveformError): self.load()

    def test_duplicate_role_accounts_and_wildcard_identity_rejected(self):
        self.sections["postgres_assembler"]["user"] = "waveform_test_receiver"
        with self.assertRaises(WaveformError): self.load()
        self.sections = config_sections()
        self.sections["gateways.GW.mqtt_receive"]["client_id"] = "waveform-+"
        with self.assertRaises(WaveformError): self.load()

    def test_assembly_reserves_nested_renewal_connections(self):
        self.sections["waveforms"]["max_db_connections"] = 3
        with self.assertRaises(WaveformError): self.load("assemble", {"TEST_WAVE_ASSEMBLER": "x"})
        self.sections["waveforms"]["max_db_connections"] = 4
        config = self.load("assemble", {"TEST_WAVE_ASSEMBLER": "x"})
        self.assertEqual((config.assembly_workers, config.max_db_connections), (2, 4))

    def test_quota_sum_and_duplicate_sessions_rejected(self):
        self.sections["capacity.global"]["facts"] = 1
        with self.assertRaises(WaveformError): self.load()
        self.sections = config_sections()
        for key, values in list(self.sections.items()):
            if key.startswith("gateways.GW"):
                self.sections[key.replace("gateways.GW", "gateways.OTHER")] = dict(values)
        self.sections["gateways.OTHER"]["source_ref"] = "88c5da01-fdc7-4327-b59e-a068c327fa9d"
        with self.assertRaises(WaveformError): self.load()

    def test_secret_resolution_errors_are_bounded(self):
        with self.assertRaises(WaveformError) as caught: self.load(env={})
        self.assertEqual(caught.exception.code, "waveform.configuration_invalid")
        self.assertNotIn("TEST_WAVE", str(caught.exception))

    def test_management_requires_explicit_accounting_bucket(self):
        with self.assertRaises(WaveformError): self.load("management")
        self.sections["management"] = {"shared_accounting_bucket": "UNKNOWN"}
        with self.assertRaises(WaveformError): self.load("management")

    def test_sender_needs_no_database_settings_or_credentials(self):
        self.sections = {key: value for key, value in self.sections.items() if not key.startswith("postgres_")}
        del self.sections["waveforms"]["max_db_connections"]
        config = self.load("sender", {"TEST_MQTT_SENDER": "sender-only"})
        self.assertEqual(dict(config.postgres), {})

    def test_connection_bound_and_source_identity_are_explicit(self):
        self.sections["waveforms"]["max_db_connections"] = 1
        with self.assertRaises(WaveformError):
            self.load("assemble", {"TEST_WAVE_ASSEMBLER": "x"})
        self.sections = config_sections()
        del self.sections["gateways.GW"]["source_ref"]
        with self.assertRaises(WaveformError): self.load()

    def test_unknown_tls_controls_cannot_silently_disable_validation(self):
        self.sections["gateways.GW.mqtt_receive"]["tls_verify"] = False
        with self.assertRaises(WaveformError): self.load()

    def test_unused_secret_references_are_not_resolved(self):
        for key, section in self.sections.items():
            if key.startswith("postgres_") and key != "postgres_receiver":
                section.pop("password_env")
                section["password_credential"] = "unavailable-unused-reference"
        with patch("backend.secret_store.read_windows_credential", side_effect=AssertionError("unused secret lookup")):
            self.assertEqual(set(self.load().postgres), {"receiver"})

    def test_enabled_management_has_named_quota_and_only_required_credentials(self):
        self.sections["management"] = {"shared_accounting_bucket": "GW"}
        config = self.load("management", {"TEST_WAVE_RECEIVER": "x", "TEST_WAVE_READER": "x", "TEST_WAVE_OPERATOR": "x"})
        self.assertEqual(config.shared_accounting_bucket, "GW")
        self.assertEqual(set(config.postgres), {"reader", "operator", "receiver"})

    def test_imports_and_disabled_cli_have_no_network_or_threads(self):
        import subprocess
        import sys
        script = """from unittest.mock import patch
with patch('psycopg.connect', side_effect=AssertionError('unexpected DB')), patch('socket.create_connection', side_effect=AssertionError('unexpected socket')), patch('threading.Thread.start', side_effect=AssertionError('unexpected thread')):
 import backend.waveforms.config
 import backend.waveforms.consumer
 import backend.waveforms.__main__
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        from backend.waveforms.__main__ import main
        from contextlib import redirect_stdout
        from io import StringIO
        self.path.write_text("[waveforms]\nenabled=false\n", encoding="utf-8")
        with patch("psycopg.connect", side_effect=AssertionError("disabled DB")), redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["--config", str(self.path), "receive"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "disabled")

    def test_gateway_sessions_and_role_secrets_are_independent(self):
        for key, values in list(self.sections.items()):
            if key.startswith("gateways.GW"):
                cloned = dict(values)
                for field in ("client_id", "username"):
                    if field in cloned: cloned[field] += "-other"
                self.sections[key.replace("gateways.GW", "gateways.OTHER")] = cloned
        self.sections["gateways.OTHER"]["source_ref"] = "88c5da01-fdc7-4327-b59e-a068c327fa9d"
        config = self.load()
        self.assertNotEqual(config.gateways[0].client_id, config.gateways[1].client_id)
        self.sections["gateways.GW.mqtt_publish"]["username"] = self.sections["gateways.GW.mqtt_receive"]["username"]
        with self.assertRaises(WaveformError): self.load()

    def test_cli_unexpected_failure_is_controlled_without_exception_text(self):
        from backend.waveforms.__main__ import main
        from contextlib import redirect_stdout
        from io import StringIO
        self.path.write_text(toml_text(self.sections), encoding="utf-8")
        with patch.dict(os.environ, self.environment), patch("backend.waveforms.__main__.run_receive", side_effect=RuntimeError("sensitive exception text")), redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["--config", str(self.path), "receive"]), 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "waveform.unavailable")
        self.assertNotIn("sensitive", output.getvalue())

    def test_worker_concurrent_events_remain_separate_json_lines(self):
        from contextlib import redirect_stdout
        import threading
        from tests.waveform_worker import emit

        class InterleavingStream:
            def __init__(self):
                self.parts = []
                self.lock = threading.Lock()
                self.first_json = threading.Event()
                self.second_json = threading.Event()

            def write(self, text):
                with self.lock:
                    self.parts.append(text)
                    first = text.startswith("{") and not self.first_json.is_set()
                    if first:
                        self.first_json.set()
                    elif text.startswith("{"):
                        self.second_json.set()
                if first:
                    # Force a competing JSON write before print's newline when
                    # emit is unprotected. A serialized emitter holds the peer
                    # outside write(), so this bounded gate then releases it.
                    self.second_json.wait(1)
                return len(text)

            def flush(self):
                pass

        stream = InterleavingStream()
        first = threading.Thread(target=emit, args=("renewal",), kwargs={"gateway": "A"})
        second = threading.Thread(target=emit, args=("renewal",), kwargs={"gateway": "B"})
        with redirect_stdout(stream):
            first.start()
            try:
                self.assertTrue(stream.first_json.wait(3))
                second.start()
                first.join(3)
                second.join(3)
            finally:
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        lines = [line for line in "".join(stream.parts).splitlines() if line]
        self.assertEqual(len(lines), 2, "concurrent event JSON objects were concatenated")
        self.assertEqual([json.loads(line) for line in lines],
                         [{"event": "renewal", "gateway": "A"}, {"event": "renewal", "gateway": "B"}])


if __name__ == "__main__":
    unittest.main()
