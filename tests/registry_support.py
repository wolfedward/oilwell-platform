"""Real, explicitly opted-in registry database fixture; never business DB cleanup."""
import importlib
import os
from pathlib import Path
import tomllib
import unittest
from uuid import UUID, uuid4

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
        # Commit pg_class ACL updates before taking TRUNCATE's exclusive locks;
        # ANALYZE may already hold a table lock while waiting for those updates.
        with self.connect("migrator") as conn:
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

    def command(self, action, target=None, *, key=None, actor=None, **body):
        from backend.registry.contracts import Actor, validate_command
        from backend.registry.service import RegistryService
        from backend.registry.transactions import RegistryTransactions
        actor = actor or Actor("platform-local", "101", "operator", "测试操作者", "administrator")
        body = {"reason": "隔离库验证", "idempotency_key": str(key or uuid4())} | body
        routes = {"well.create": "/wells", "well.change": f"/wells/{target}/changes",
                  "device.create": "/devices", "device.change": f"/devices/{target}/changes",
                  "device.state": f"/devices/{target}/state", "device.bind": f"/devices/{target}/bindings",
                  "device.unbind": f"/devices/{target}/unbind", "map.create": f"/devices/{target}/point-maps",
                  "map.assign": f"/devices/{target}/point-map-assignment",
                  "map.review": f"/devices/{target}/point-maps/{body.get('map_version', '')}/review"}
        command = validate_command(action, UUID(target) if target is not None else None, actor, body,
                                   method="POST", route="/api/registry" + routes[action])
        return RegistryService(RegistryTransactions(lambda: self.connect("writer"))).execute(command)

    def fixture(self, *, active=False):
        """Create only isolated fixtures through the real lifecycle service."""
        suffix = uuid4().hex
        well = self.command("well.create", area_code="A", well_code="W" + suffix, name="测试井")["entity"]
        device = self.command("device.create", gateway_id="GW", device_id="D" + suffix, name="测试终端",
                              transport="mqtt_direct", usage="commissioning")["entity"]
        point_map = self.command("map.create", device["id"], expected_version=device["version"], parent_version=None,
                                 points=[{"point_code": "load", "unit": "kN", "engineering_min": 0,
                                          "engineering_max": 80}])["entity"]
        reviewed = self.command("map.review", device["id"], expected_version=point_map["device_version"],
                                map_version=point_map["map_version"], sha256=point_map["sha256"], decision="approved")["entity"]
        bound = self.command("device.bind", device["id"], expected_version=reviewed["device_version"], well_id=well["id"])
        current = self.command("map.assign", device["id"], expected_version=bound["entity"]["version"],
                               map_version=point_map["map_version"])["entity"]
        if active:
            current = self.command("device.state", device["id"], expected_version=current["version"], status="active",
                                   check_note="隔离测试核对依据")["entity"]
        return {"well_id": well["id"], "device_id": device["id"], "version": current["version"],
                "map_version": point_map["map_version"], "sha256": point_map["sha256"], "binding_time": bound["committed_at"]}

    def read(self, resource, **filters):
        from backend.registry.queries import RegistryQueries
        return RegistryQueries(lambda: self.connect("reader"), online_seconds=120, delayed_seconds=900,
                               clock_tolerance_seconds=300).read(resource, filters)
