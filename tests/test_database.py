import http.client
import json
import sqlite3
import tempfile
import threading
import time
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
        with database.transaction(None, "seed", "unit", "U-1"):
            database.execute("INSERT INTO units(id, name) VALUES(?, ?)", ("U-1", "Unit 1"))
        with database.transaction(None, "seed", "well_group", "G-1"):
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
            with database.transaction(None, "seed", "well", "W-X"):
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

    def test_timestamps_use_shanghai_iso_format_and_update_without_recursion(self):
        database = self.create_database()

        with database.transaction(None, "seed", "unit", "U-1"):
            database.execute("INSERT INTO units(id, name) VALUES(?, ?)", ("U-1", "Unit 1"))
        inserted = database.query_one("SELECT created_at, updated_at FROM units WHERE id = ?", ("U-1",))
        time.sleep(0.01)
        with database.transaction(None, "rename", "unit", "U-1"):
            database.execute("UPDATE units SET name = ? WHERE id = ?", ("Renamed", "U-1"))
        updated = database.query_one("SELECT created_at, updated_at FROM units WHERE id = ?", ("U-1",))

        self.assertRegex(inserted["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+08:00$")
        self.assertEqual(inserted["created_at"], inserted["updated_at"])
        self.assertEqual(updated["created_at"], inserted["created_at"])
        self.assertGreater(updated["updated_at"], inserted["updated_at"])

    def test_every_core_table_has_updated_at_maintenance_trigger(self):
        database = self.create_database()

        trigger_names = set(
            database.column("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        )

        self.assertTrue(
            {f"{table_name}_set_updated_at" for table_name in CORE_TABLES}.issubset(trigger_names)
        )

    def test_migrate_normalizes_legacy_table_timestamps_without_rebuilding(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(
                """
                CREATE TABLE units (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    actor_id INTEGER,
                    source_type TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE audit_logs (
                    id INTEGER PRIMARY KEY,
                    actor_id INTEGER,
                    action TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT,
                    status TEXT NOT NULL DEFAULT 'recorded',
                    source_type TEXT NOT NULL DEFAULT 'system',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO units(id, name) VALUES('U-OLD', 'Legacy unit');
                """
            )
        finally:
            connection.close()
        database = Database(self.path)

        database.migrate()
        legacy = database.query_one("SELECT created_at, updated_at FROM units WHERE id = 'U-OLD'")
        with database.transaction(None, "seed", "unit", "U-NEW"):
            database.execute("INSERT INTO units(id, name) VALUES(?, ?)", ("U-NEW", "New unit"))
        inserted = database.query_one("SELECT created_at, updated_at FROM units WHERE id = 'U-NEW'")
        time.sleep(0.01)
        with database.transaction(None, "rename", "unit", "U-NEW"):
            database.execute("UPDATE units SET name = ? WHERE id = ?", ("Renamed", "U-NEW"))
        updated = database.query_one("SELECT created_at, updated_at FROM units WHERE id = 'U-NEW'")

        for timestamp in (*legacy, *inserted, *updated):
            self.assertRegex(timestamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+08:00$")
        self.assertEqual(updated["created_at"], inserted["created_at"])
        self.assertGreater(updated["updated_at"], inserted["updated_at"])

    def test_every_core_table_has_insert_timestamp_normalization_trigger(self):
        database = self.create_database()

        trigger_names = set(database.column("SELECT name FROM sqlite_master WHERE type = 'trigger'"))

        self.assertTrue(
            {f"{table_name}_normalize_inserted_timestamps" for table_name in CORE_TABLES}.issubset(
                trigger_names
            )
        )

    def test_query_and_execute_helpers_use_real_database_records(self):
        database = self.create_database()
        with database.transaction(None, "seed", "unit", None):
            database.executemany(
                "INSERT INTO units(id, name) VALUES(?, ?)",
                [("U-1", "Unit 1"), ("U-2", "Unit 2")],
            )

        row = database.query_one("SELECT id, name FROM units WHERE id = ?", ("U-1",))

        self.assertEqual(row["name"], "Unit 1")
        self.assertEqual([item["id"] for item in database.query_all("SELECT id FROM units ORDER BY id")], ["U-1", "U-2"])
        self.assertEqual(database.scalar("SELECT COUNT(*) FROM units"), 2)
        self.assertEqual(database.column("SELECT name FROM units ORDER BY id"), ["Unit 1", "Unit 2"])

    def test_execute_rejects_insert_outside_audited_transaction(self):
        database = self.create_database()

        with self.assertRaisesRegex(RuntimeError, "DML requires Database.transaction"):
            database.execute("INSERT INTO units(id, name) VALUES(?, ?)", ("U-1", "Unit 1"))

        self.assertEqual(database.scalar("SELECT COUNT(*) FROM units"), 0)

    def test_execute_rejects_comment_prefixed_insert_outside_audited_transaction(self):
        database = self.create_database()

        with self.assertRaisesRegex(RuntimeError, "DML requires Database.transaction"):
            database.execute(
                "-- preserve the SQL audit boundary\nINSERT INTO units(id, name) VALUES(?, ?)",
                ("U-1", "Unit 1"),
            )

        self.assertEqual(database.scalar("SELECT COUNT(*) FROM units"), 0)

    def test_read_helpers_reject_insert_returning_outside_audited_transaction(self):
        database = self.create_database()

        for index, helper in enumerate(
            (database.query_one, database.query_all, database.scalar, database.column), start=1
        ):
            with self.subTest(helper=helper.__name__):
                with self.assertRaisesRegex(RuntimeError, "DML requires Database.transaction"):
                    helper(
                        f"INSERT INTO units(id, name) VALUES('U-{index}', 'Unit {index}') RETURNING id"
                    )

        self.assertEqual(database.scalar("SELECT COUNT(*) FROM units"), 0)

    def test_execute_rejects_with_and_semicolon_prefixed_inserts_outside_transaction(self):
        database = self.create_database()

        for statement in (
            "WITH input(id, name) AS (VALUES('U-1', 'Unit 1')) "
            "INSERT INTO units(id, name) SELECT id, name FROM input",
            "; INSERT INTO units(id, name) VALUES('U-2', 'Unit 2')",
        ):
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(RuntimeError, "DML requires Database.transaction"):
                    database.execute(statement)

        self.assertEqual(database.scalar("SELECT COUNT(*) FROM units"), 0)

    def test_query_one_insert_returning_commits_with_its_audit_entry_inside_transaction(self):
        database = self.create_database()

        with database.transaction(None, "seed", "unit", "U-1"):
            row = database.query_one("INSERT INTO units(id, name) VALUES('U-1', 'Unit 1') RETURNING id")

        self.assertEqual(row["id"], "U-1")
        self.assertEqual(database.scalar("SELECT COUNT(*) FROM audit_logs"), 1)

    def test_execute_allows_ddl_outside_transaction(self):
        database = self.create_database()

        database.execute("CREATE TABLE temporary_read_model(id INTEGER PRIMARY KEY)")

        self.assertIn("temporary_read_model", database.table_names())

    def test_executemany_rejects_mutations_outside_audited_transaction(self):
        database = self.create_database()

        with self.assertRaisesRegex(RuntimeError, "DML requires Database.transaction"):
            database.executemany(
                "INSERT INTO units(id, name) VALUES(?, ?)",
                [("U-1", "Unit 1"), ("U-2", "Unit 2")],
            )

        self.assertEqual(database.scalar("SELECT COUNT(*) FROM units"), 0)

    def test_mutating_helpers_commit_and_audit_inside_transaction(self):
        database = self.create_database()

        with database.transaction(None, "seed", "unit", None):
            database.executemany(
                "INSERT INTO units(id, name) VALUES(?, ?)",
                [("U-1", "Unit 1"), ("U-2", "Unit 2")],
            )
            database.execute("UPDATE units SET name = ? WHERE id = ?", ("Renamed", "U-1"))

        self.assertEqual(database.column("SELECT name FROM units ORDER BY id"), ["Renamed", "Unit 2"])
        self.assertEqual(database.scalar("SELECT COUNT(*) FROM audit_logs"), 1)

    def test_transaction_commits_business_change_and_audit_entry(self):
        database = self.create_database()

        with database.transaction(7, "create", "unit", "U-1") as connection:
            connection.execute("INSERT INTO units(id, name) VALUES(?, ?)", ("U-1", "Unit 1"))

        self.assertEqual(database.scalar("SELECT COUNT(*) FROM units WHERE id = ?", ("U-1",)), 1)
        audit = database.query_one("SELECT actor_id, action, object_type, object_id FROM audit_logs")
        self.assertEqual(tuple(audit), (7, "create", "unit", "U-1"))

    def test_transaction_audit_provenance_defaults_to_system_even_for_seed_named_actions(self):
        """An action name alone must not silently change audit provenance."""
        database = self.create_database()

        with database.transaction(None, "seed.generate", "unit", "U-1") as connection:
            connection.execute("INSERT INTO units(id, name) VALUES(?, ?)", ("U-1", "Unit 1"))

        self.assertEqual(database.scalar("SELECT source_type FROM audit_logs"), "system")

    def test_transaction_rolls_back_business_change_and_audit_entry_on_error(self):
        database = self.create_database()

        with self.assertRaisesRegex(RuntimeError, "stop"):
            with database.transaction(7, "create", "unit", "U-1") as connection:
                database.execute("INSERT INTO units(id, name) VALUES(?, ?)", ("U-1", "Unit 1"))
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

    def test_health_returns_json_when_database_becomes_unavailable(self):
        server = create_server("127.0.0.1", 0, database_path=self.path)
        server.database.path = Path(self.temporary_directory.name)
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

        self.assertEqual(response.status, 503)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "database_unavailable")

    def test_health_returns_json_when_database_parent_is_a_file(self):
        server = create_server("127.0.0.1", 0, database_path=self.path)
        blocked_parent = Path(self.temporary_directory.name) / "not-a-directory"
        blocked_parent.write_text("file", encoding="utf-8")
        server.database.path = blocked_parent / "platform.db"
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

        self.assertEqual(response.status, 503)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "database_unavailable")


if __name__ == "__main__":
    unittest.main()
