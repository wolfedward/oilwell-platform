"""Real WP-03 database fixture, isolated from both business and WP-02 data."""

from hashlib import sha256
import importlib
import os
from pathlib import Path
import tomllib
import unittest
from uuid import uuid4

import psycopg

from backend.center.config import _postgres_settings
from backend.ingest_reliability.contracts import CapacityLimits
from backend.secret_store import read_windows_credential
from tests.ingest_vectors import FIXED_TIME


ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = "oilwell_ingest_reliability_test"
TEST_LOCK = 499001210
LIMITS = CapacityLimits(16 * 1024 * 1024, 8 * 1024 * 1024)
ROLES = ("migrator", "ingestor", "publisher", "reader", "operator")
RESET_SQL = """TRUNCATE ingest_reliability.deliveries, ingest_reliability.receipts,
    ingest_reliability.operation_receipts, ingest_reliability.audit_events,
    ingest_reliability.cases, ingest_reliability.fact_observations,
    ingest_reliability.worker_health, ingest_reliability.facts,
    public.telemetry_samples, public.ingest_messages"""


class IngestDatabaseCase(unittest.TestCase):
    def setUp(self):
        config = os.environ.get("OILWELL_INGEST_TEST_CONFIG")
        self.assertTrue(config, "WP-03 requires the explicitly provisioned isolated test configuration")
        self.config_path = Path(config).resolve()
        self.raw_config = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.settings = {
            role: _postgres_settings(self.raw_config["postgres_" + role], self.config_path,
                                     os.environ, read_windows_credential) | {"connect_timeout": 3}
            for role in ROLES}
        for settings in self.settings.values():
            self.assertEqual((settings["host"], settings["port"], settings["dbname"]),
                             ("127.0.0.1", 3822, TEST_DATABASE), "Never reset a different database")
            self.assertTrue(settings["user"].startswith("ingest_test_"))
        self.assertEqual(len({settings["user"] for settings in self.settings.values()}), 5)
        self.connections = []
        self.addCleanup(self._close_connections)
        self.guard = self.connect("migrator")
        self.guard.autocommit = True
        self.guard.execute("SET statement_timeout = '30s'")
        self.guard.execute("SELECT pg_advisory_lock(%s)", (TEST_LOCK,))
        self.guard.execute((ROOT / "backend/center/schema.sql").read_text(encoding="utf-8"))
        self.migrations = importlib.import_module("backend.ingest_reliability.migrate")
        result = self.migrations.migrate(self.settings["migrator"], expected_database=TEST_DATABASE, limits=LIMITS)
        if result["applied"]:
            self.assertEqual(self.guard.execute("SELECT count(*) FROM ingest_reliability.facts").fetchone()[0], 0)
        with self.connect("migrator") as conn:
            self.migrations.grant_roles(conn, roles={role: self.settings[role]["user"] for role in ROLES[1:]})
            self.assertEqual(conn.execute("SELECT current_database()").fetchone()[0], TEST_DATABASE)
            conn.execute(RESET_SQL)
            conn.execute("UPDATE ingest_reliability.capacity SET used_total_bytes=0, used_quarantine_bytes=0, "
                         "last_clock_seen=CURRENT_TIMESTAMP WHERE singleton=1")

    def _close_connections(self):
        for conn in reversed(self.connections):
            conn.close()

    def connect(self, role="ingestor"):
        if role not in ROLES:
            raise ValueError("Unknown isolated test role")
        conn = psycopg.connect(**self.settings[role])
        self.connections.append(conn)
        if conn.execute("SELECT current_database()").fetchone()[0] != TEST_DATABASE:
            conn.close()
            raise AssertionError("Refusing a non-test connection")
        conn.rollback()
        return conn

    def admin(self):
        settings = _postgres_settings(self.raw_config["postgres_test_admin"], self.config_path,
                                      os.environ, read_windows_credential)
        self.assertEqual((settings["host"], settings["port"], settings["dbname"]),
                         ("127.0.0.1", 3822, TEST_DATABASE))
        conn = psycopg.connect(**(settings | {"connect_timeout": 3}))
        self.connections.append(conn)
        self.assertEqual(conn.execute("SELECT current_database()").fetchone()[0], TEST_DATABASE)
        conn.rollback()
        return conn

    def scalar(self, statement, params=(), *, role="reader"):
        with self.connect(role) as conn:
            return conn.execute(statement, params).fetchone()[0]

    def repository(self, receipt_gateways=frozenset({"GW"})):
        from backend.ingest_reliability.repository import ReliableRepository
        return ReliableRepository(lambda: self.connect("ingestor"), prefix="oilfield",
                                  receipt_gateways=receipt_gateways, limits=LIMITS)

    def outbox(self):
        from backend.ingest_reliability.outbox import OutboxRepository
        return OutboxRepository(lambda: self.connect("publisher"))

    def operations(self):
        from backend.ingest_reliability.operations import ReliabilityOperations
        return ReliabilityOperations(lambda: self.connect("operator"))

    def insert_fact(self, *, role="ingestor", topic="oilfield/v1/GW/D/telemetry", payload=b"bad", **overrides):
        """DDL fixture only: later business tests must use ReliableRepository."""
        from psycopg import sql
        values = dict(fact_id=uuid4(), topic=topic, topic_sha256=sha256(topic.encode()).hexdigest(),
                      raw_payload=payload, payload_sha256=sha256(payload).hexdigest(), payload_bytes=len(payload),
                      qos=1, retain=False, dup=False, received_at=FIXED_TIME,
                      validator_version="OWP-INGEST/1.0", status="quarantined", reason_code="json_invalid",
                      accepted_message_id=None, gateway_id=None, device_id=None, message_id=None)
        values.update(overrides)
        statement = sql.SQL("INSERT INTO ingest_reliability.facts ({}) VALUES ({})").format(
            sql.SQL(",").join(map(sql.Identifier, values)), sql.SQL(",").join(sql.Placeholder() for _ in values))
        with self.connect(role) as conn:
            conn.execute(statement, tuple(values.values()))
        return values["fact_id"]
