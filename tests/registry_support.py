"""Real, explicitly opted-in registry database fixture; never business DB cleanup."""
import importlib
import os
from pathlib import Path
import tomllib
import unittest

import psycopg

from backend.center.config import _postgres_settings
from backend.secret_store import read_windows_credential


ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = "oilwell_registry_test"
TEST_LOCK = 499001110
RESET_SQL = """TRUNCATE device_registry.operation_receipts,
    device_registry.point_map_assignments, device_registry.point_map_versions,
    device_registry.well_bindings, device_registry.device_state_periods,
    device_registry.audit_events, device_registry.devices, device_registry.wells,
    public.telemetry_samples, public.ingest_messages"""


class RegistryDatabaseCase(unittest.TestCase):
    def setUp(self):
        config = os.environ.get("OILWELL_REGISTRY_TEST_CONFIG")
        if not config:
            self.skipTest("Real registry database verification requires OILWELL_REGISTRY_TEST_CONFIG")
        self.config_path = Path(config).resolve()
        raw = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.settings = {
            role: _postgres_settings(raw["postgres_" + role], self.config_path,
                                     os.environ, read_windows_credential) | {"connect_timeout": 3}
            for role in ("migrator", "writer", "reader")}
        for item in self.settings.values():
            self.assertEqual(item["dbname"], TEST_DATABASE, "Never reset any non-test database")
        self.assertEqual(len({(item["host"], item["port"], item["dbname"])
                              for item in self.settings.values()}), 1)
        self.connections = []
        self.addCleanup(self._close_connections)
        self.guard = self.connect("migrator")
        self.guard.autocommit = True
        self.guard.execute("SET statement_timeout = '30s'")
        self.guard.execute("SELECT pg_advisory_lock(%s)", (TEST_LOCK,))
        try:
            self.migrations = importlib.import_module("backend.registry.migrate")
        except ModuleNotFoundError as error:
            if error.name != "backend.registry.migrate":
                raise
            self.fail("Complete registry migration has not been implemented")
        self.guard.execute((ROOT / "backend/center/schema.sql").read_text(encoding="utf-8"))
        migration = self.migrations.migrate(self.settings["migrator"], expected_database=TEST_DATABASE)
        if migration["applied"]:
            # Check before any fixture cleanup can conceal accidental seed rows.
            self.assertEqual(self.guard.execute("SELECT count(*) FROM device_registry.wells").fetchone()[0], 0)
            self.assertEqual(self.guard.execute("SELECT count(*) FROM device_registry.devices").fetchone()[0], 0)
        with self.connect("migrator") as conn:
            self.migrations.grant_roles(conn, writer=self.settings["writer"]["user"],
                                       reader=self.settings["reader"]["user"])
            # Identity must be checked on the exact connection doing the reset.
            self.assertEqual(conn.execute("SELECT current_database()").fetchone()[0], TEST_DATABASE)
            conn.execute(RESET_SQL)
            conn.execute("UPDATE device_registry.registry_head SET revision=1 WHERE singleton=1")

    def _close_connections(self):
        for conn in reversed(self.connections):
            conn.close()

    def tearDown(self):
        self._close_connections()

    def connect(self, role="writer"):
        if role not in self.settings:
            raise ValueError("Unknown test connection role")
        conn = psycopg.connect(**self.settings[role])
        self.connections.append(conn)
        if conn.execute("SELECT current_database()").fetchone()[0] != TEST_DATABASE:
            conn.close()
            raise AssertionError("Refusing connection to a non-test database")
        conn.rollback()
        return conn

    def scalar(self, statement, params=(), *, role="reader"):
        with self.connect(role) as conn:
            return conn.execute(statement, params).fetchone()[0]
