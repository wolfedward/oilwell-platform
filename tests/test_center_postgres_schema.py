"""Production PostgreSQL schema contract tests."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent


class PostgresSchemaTests(unittest.TestCase):
    def test_telemetry_is_range_partitioned_and_ingest_identity_is_unique(self):
        sql = (ROOT / "backend" / "center" / "schema.sql").read_text(encoding="utf-8").lower()
        self.assertIn("create table if not exists ingest_messages", sql)
        self.assertIn("message_id text primary key", sql)
        self.assertIn("partition by range (measured_at)", sql)
        self.assertIn("foreign key (message_id)", sql)
        self.assertIn("using brin (measured_at)", sql)


if __name__ == "__main__":
    unittest.main()
