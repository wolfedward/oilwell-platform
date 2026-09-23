"""Wire contract checks against real PostgreSQL session-local tables only."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest

from backend.center.config import load_query_postgres_config
from backend.center.ingest import CentralIngestService, IngestValidationError
from backend.center.postgres import PostgresTelemetryRepository


@unittest.skipUnless(os.environ.get("OILWELL_POSTGRES_TEST_CONFIG"), "PostgreSQL wire verification is opt-in")
class WirePostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        settings = load_query_postgres_config(os.environ["OILWELL_POSTGRES_TEST_CONFIG"])
        self.connection = psycopg.connect(**settings, autocommit=True)
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TEMP TABLE ingest_messages (LIKE public.ingest_messages INCLUDING ALL)")
        self.connection.execute("CREATE TEMP TABLE telemetry_samples (LIKE public.telemetry_samples INCLUDING ALL)")

        @contextmanager
        def factory(_dsn, _kwargs):
            with self.connection.transaction():
                yield self.connection

        self.service = CentralIngestService(
            PostgresTelemetryRepository(connect_kwargs=settings, connect_factory=factory),
            clock=lambda: datetime.now(timezone.utc))
        self.payload = (Path(__file__).resolve().parents[1] / "assets/open-interface/telemetry-example.json").read_bytes()
        self.topic = "oilfield/v1/GW-EXAMPLE/D-EXAMPLE/telemetry"

    def test_valid_unicode_bigint_and_float_extremes_round_trip(self):
        body = json.loads(self.payload)
        body["sequence"] = {"first": 9223372036854775807, "last": 9223372036854775807}
        for row, value in zip(body["samples"], [5e-324, 1.7976931348623157e308, -5.8]):
            row.update(sequence=9223372036854775807, unit="立方米/日\U0001f4a7", value=value)
        payload = json.dumps(body).encode()
        self.assertEqual(self.service.ingest(self.topic, payload).status, "stored")
        self.assertEqual(self.service.ingest(self.topic, payload).status, "duplicate")
        rows = self.connection.execute("SELECT sequence,value,unit FROM telemetry_samples ORDER BY sample_index").fetchall()
        self.assertEqual(rows, [(9223372036854775807, value, "立方米/日\U0001f4a7")
                                for value in [5e-324, 1.7976931348623157e308, -5.8]])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM ingest_messages").fetchone()[0], 1)

    def test_invalid_payloads_leave_no_partial_rows_and_next_valid_ingest_works(self):
        body = json.loads(self.payload)
        body["samples"][0]["unit"] = "\x00"
        for payload in (json.dumps(body).encode(), self.payload.replace(b'5.8', b'1e-100000'),
                        self.payload.replace(b'"schema":', b'"schema":"bad","schema":', 1)):
            with self.assertRaises(IngestValidationError):
                self.service.ingest(self.topic, payload)
            self.assertEqual(self.connection.execute("SELECT count(*) FROM ingest_messages").fetchone()[0], 0)
            self.assertEqual(self.connection.execute("SELECT count(*) FROM telemetry_samples").fetchone()[0], 0)
        self.assertEqual(self.service.ingest(self.topic, self.payload).status, "stored")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM telemetry_samples").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
