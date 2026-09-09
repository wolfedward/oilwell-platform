import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from backend.database import Database
from backend.http_server import create_server


CORE_TABLES = {
    "units",
    "well_groups",
    "wells",
    "devices",
    "device_channels",
    "device_heartbeats",
    "telemetry",
    "raw_messages",
    "data_quality_events",
    "dynagraphs",
    "dynagraph_features",
    "diagnoses",
    "model_runs",
    "recovery_tests",
    "energy_strategies",
    "strategy_versions",
    "device_commands",
    "command_attempts",
    "alarms",
    "approvals",
    "work_orders",
    "work_order_logs",
    "reports",
    "users",
    "roles",
    "audit_logs",
    "system_events",
}


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "platform.db"

    def create_database(self):
        database = Database(self.path)
        database.migrate()
        return database

    def add_well_parent_records(self, database):
        database.execute("INSERT INTO units(id, name) VALUES(?, ?)", ("U-1", "Unit 1"))
        database.execute(
            "INSERT INTO well_groups(id, unit_id, name) VALUES(?, ?, ?)",
            ("G-1", "U-1", "Group 1"),
        )

    def test_migrate_creates_all_core_tables_and_is_idempotent(self):
        database = Database(self.path)

        database.migrate()
        database.migrate()

        self.assertTrue(CORE_TABLES.issubset(database.table_names()))

    def test_foreign_keys_are_enforced(self):
        database = self.create_database()

        with self.assertRaises(sqlite3.IntegrityError):
            database.execute(
                "INSERT INTO wells(id, group_id, name) VALUES(?, ?, ?)",
                ("W-X", "missing", "invalid"),
            )

    def test_core_tables_include_traceability_metadata(self):
        database = self.create_database()

        for table_name in CORE_TABLES:
            columns = database.column(f"SELECT name FROM pragma_table_info('{table_name}')")
            self.assertTrue(
                {"created_at", "updated_at", "status", "actor_id", "source_type"}.issubset(columns),
                table_name,
            )

    def test_connections_enable_wal_foreign_keys_and_busy_timeout(self):
        database = self.create_database()

        with database.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_query_and_execute_helpers_use_real_database_records(self):
        database = self.create_database()
        database.executemany(
            "INSERT INTO units(id, name) VALUES(?, ?)",
            [("U-1", "Unit 1"), ("U-2", "Unit 2")],
        )

        row = database.query_one("SELECT id, name FROM units WHERE id = ?", ("U-1",))

        self.assertEqual(row["name"], "Unit 1")
        self.assertEqual([item["id"] for item in database.query_all("SELECT id FROM units ORDER BY id")], ["U-1", "U-2"])
        self.assertEqual(database.scalar("SELECT COUNT(*) FROM units"), 2)
        self.assertEqual(database.column("SELECT name FROM units ORDER BY id"), ["Unit 1", "Unit 2"])

    def test_transaction_commits_business_change_and_audit_entry(self):
        database = self.create_database()

        with database.transaction(7, "create", "unit", "U-1") as connection:
            connection.execute("INSERT INTO units(id, name) VALUES(?, ?)", ("U-1", "Unit 1"))

        self.assertEqual(database.scalar("SELECT COUNT(*) FROM units WHERE id = ?", ("U-1",)), 1)
        audit = database.query_one("SELECT actor_id, action, object_type, object_id FROM audit_logs")
        self.assertEqual(tuple(audit), (7, "create", "unit", "U-1"))

    def test_transaction_rolls_back_business_change_and_audit_entry_on_error(self):
        database = self.create_database()

        with self.assertRaisesRegex(RuntimeError, "stop"):
            with database.transaction(7, "create", "unit", "U-1") as connection:
                connection.execute("INSERT INTO units(id, name) VALUES(?, ?)", ("U-1", "Unit 1"))
                raise RuntimeError("stop")

        self.assertEqual(database.scalar("SELECT COUNT(*) FROM units"), 0)
        self.assertEqual(database.scalar("SELECT COUNT(*) FROM audit_logs"), 0)

    def test_health_server_migrates_and_reports_its_real_database(self):
        server = create_server("127.0.0.1", 0, database_path=self.path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["data"]["database"], "ok")
        self.assertIn("audit_logs", server.database.table_names())


if __name__ == "__main__":
    unittest.main()
