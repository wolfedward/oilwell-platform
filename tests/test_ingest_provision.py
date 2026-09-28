"""Safety guards for the explicitly authorized local test fixture initializer."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

from tools.provision_ingest_test import validate_identity, validate_output


class ProvisionGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ingest-provision-guard-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "registry-test" / "identity.json"
        self.raw = {"database": "oilwell_registry_test", "host": "127.0.0.1", "port": 3822,
                    "data": str(self.path.parent / "data"), "roles": {"admin": "registry_test_admin"},
                    "credential_targets": {"admin": "oilwell-registry-test-" + "a" * 32 + "-admin"}}

    def test_accepts_only_the_recorded_local_fixture(self):
        result = validate_identity(self.path, self.raw)
        self.assertEqual(result["port"], 3822)
        self.assertEqual(result["data"], (self.path.parent / "data").resolve())
        self.assertEqual(result["database"], "oilwell_registry_test")

    def test_rejects_business_ports_hosts_databases_and_mismatched_data_directory(self):
        for fields in ({"port": 5432}, {"port": True}, {"port": "3822"},
                       {"host": "192.0.2.1"}, {"host": "localhost"},
                       {"database": "oilwell"}, {"data": str(self.root)},
                       {"data": "data"}, {"data": str(self.root / "other" / "data")}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_identity(self.path, dict(self.raw, **fields))

    def test_rejects_non_fixture_administrator_references_before_secret_lookup(self):
        for key, value in (("roles", {"admin": "postgres"}),
                           ("credential_targets", {"admin": "business-admin"}),
                           ("roles", {}), ("credential_targets", [])):
            raw = deepcopy(self.raw)
            raw[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_identity(self.path, raw)

    def test_output_cannot_escape_the_new_fixture_directory(self):
        expected = (self.root / "ingest-reliability-test").resolve()
        self.assertEqual(validate_output(self.path, expected), expected)
        for output in (self.root, self.path.parent, self.root / "business-data", Path("relative")):
            with self.subTest(output=output), self.assertRaises(ValueError):
                validate_output(self.path, output)

    def test_cli_rejects_wrong_identity_without_creating_output_or_exposing_inputs(self):
        self.path.parent.mkdir()
        raw = dict(self.raw, port=5432, private_note="must-not-be-printed")
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        output = self.root / "ingest-reliability-test"
        process = subprocess.run([sys.executable, "tools/provision_ingest_test.py", "--identity", str(self.path),
                                  "--output", str(output)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(output.exists())
        self.assertNotIn("must-not-be-printed", process.stdout + process.stderr)
        self.assertNotIn("Traceback", process.stderr)


class ProvisionIntegrationTests(unittest.TestCase):
    def test_explicit_initializer_creates_owned_roles_and_reruns_without_secret_rotation(self):
        from backend.center.config import _postgres_settings
        from backend.secret_store import read_windows_credential
        import psycopg
        identity_name = os.environ.get("OILWELL_INGEST_PROVISION_IDENTITY")
        self.assertTrue(identity_name, "Explicit local test provisioning opt-in is required")
        identity = Path(identity_name).resolve()
        output = identity.parent.parent / "ingest-reliability-test"
        command = [sys.executable, "tools/provision_ingest_test.py", "--identity", str(identity),
                   "--output", str(output)]
        first = subprocess.run(command, capture_output=True, text=True, timeout=45)
        self.assertEqual(first.returncode, 0, first.stderr)
        result = json.loads(first.stdout)
        self.assertEqual(result["database"], "oilwell_ingest_reliability_test")
        config_path = Path(result["config"])
        self.assertEqual(config_path, output / "ingest-test.local.toml")
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        roles = ("migrator", "ingestor", "publisher", "reader", "operator")
        before = []
        for role in roles:
            section = raw["postgres_" + role]
            self.assertNotIn("password", section)
            settings = _postgres_settings(section, config_path, os.environ, read_windows_credential)
            before.append((settings["user"], settings["password"]))
            with psycopg.connect(**(settings | {"connect_timeout": 3})) as conn:
                self.assertEqual(conn.execute("SELECT current_database()").fetchone()[0], "oilwell_ingest_reliability_test")
                powerful = conn.execute("SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
                                        "FROM pg_roles WHERE rolname=current_user").fetchone()[0]
                self.assertFalse(powerful)
        self.assertEqual(len({name for name, _ in before}), 5)
        second = subprocess.run(command, capture_output=True, text=True, timeout=45)
        self.assertEqual(second.returncode, 0, second.stderr)
        after_raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        after = []
        for role in roles:
            section = after_raw["postgres_" + role]
            credential = read_windows_credential(section["password_credential"])
            after.append((credential.username, credential.password))
        self.assertTrue(before == after, "An idempotent initializer must not rotate fixture credentials")
        # Never include secret values in assertion failure messages.
        self.assertTrue(all(secret not in first.stdout + first.stderr + second.stdout + second.stderr
                            for _, secret in before), "Initializer output exposed a secret")


if __name__ == "__main__":
    unittest.main()
