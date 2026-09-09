import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.database import Database
from backend.seed import SeedSummary, seed_database


class SeedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "platform.db")
        self.database.migrate()

    def test_seed_creates_consistent_project_history(self):
        """Removing the fixture generator must leave the database without the required history."""
        summary = seed_database(self.database)

        self.assertEqual(summary, SeedSummary(4, 8, 48, 48, 48 * 30 * 24))
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM wells WHERE is_low_output = 1"), 16)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM production_daily"), 48 * 90)
        self.assertGreaterEqual(self.database.scalar("SELECT COUNT(*) FROM telemetry"), 48 * 30 * 24)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM alarms"), 16)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM work_orders"), 16)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM energy_strategies"), 16)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM system_events"), 48)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM pragma_foreign_key_check"), 0)

    def test_seed_is_idempotent_deterministic_and_traceable(self):
        """Changing the seed path must not alter generated records or overwrite an existing fixture."""
        first = seed_database(self.database)
        first_daily = self.database.query_one(
            "SELECT daily_fluid FROM production_daily ORDER BY well_id, day LIMIT 1"
        )
        first_rows = self.database.scalar("SELECT COUNT(*) FROM production_daily")

        second = seed_database(self.database)

        self.assertEqual(first, second)
        self.assertEqual(round(first_daily["daily_fluid"], 3), 2.846)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM production_daily"), first_rows)
        self.assertEqual(
            self.database.scalar(
                "SELECT COUNT(*) FROM telemetry WHERE source_type <> 'simulator'"
            ),
            0,
        )
        self.assertEqual(
            self.database.scalar(
                "SELECT COUNT(*) FROM units WHERE created_at NOT LIKE '%+08:00'"
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
