"""Real PostgreSQL queries against disposable session-local tables.

Opt in: OILWELL_POSTGRES_TEST_CONFIG=config/center.local.toml
No rows or schema objects in public are changed by these tests.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import unittest

from backend.center.config import load_center_config
from backend.center.postgres import PostgresRepositoryError, PostgresTelemetryQueryRepository

NOW = datetime(2026, 9, 23, 4, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get("OILWELL_POSTGRES_TEST_CONFIG"), "PostgreSQL integration is opt-in")
class CentralPostgresIntegrationTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.rows import dict_row
        self.psycopg = psycopg
        settings = load_center_config(os.environ["OILWELL_POSTGRES_TEST_CONFIG"]).postgres
        self.connection = psycopg.connect(**settings, row_factory=dict_row, autocommit=True)
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TEMP TABLE ingest_messages (LIKE public.ingest_messages INCLUDING DEFAULTS)")
        self.connection.execute("CREATE TEMP TABLE telemetry_samples (LIKE public.telemetry_samples INCLUDING DEFAULTS)")

        @contextmanager
        def factory(_dsn, _kwargs):
            with self.connection.transaction():
                yield self.connection

        self.repository = PostgresTelemetryQueryRepository(connect_kwargs=settings, connect_factory=factory)

    def add(self, ident, first, last=None, *, received=None, measured=None, boot="boot-a", device="D-001", gateway="GW-001", quality="good"):
        last = first if last is None else last
        received = received or NOW - timedelta(seconds=10)
        measured = measured or NOW - timedelta(seconds=15)
        self.connection.execute(
            """INSERT INTO ingest_messages(message_id,topic,gateway_id,device_id,boot_id,
                   sequence_first,sequence_last,created_at,received_at,payload_sha256,raw_payload)
               VALUES(%s,'oilfield/v1/telemetry',%s,%s,%s,%s,%s,%s,%s,%s,'{}')""",
            (ident, gateway, device, boot, first, last, received, received, "0" * 64),
        )
        with self.connection.cursor() as cursor:
            cursor.executemany(
            """INSERT INTO telemetry_samples(message_id,sample_index,gateway_id,device_id,point_code,
                   measured_at,sequence,value,quality,unit,source,received_at)
               VALUES(%s,%s,%s,%s,'active_power_kw',%s,%s,6.125,%s,'kW','modbus-tcp',%s)""",
            [(ident, seq-first, gateway, device, measured, seq, quality, received) for seq in range(first, last+1)],
            )

    def test_filtered_page_and_count_use_real_sql_and_preserve_precision(self):
        self.add("a", 1)
        self.add("b", 2, device="D-002")
        result = self.repository.list_samples(device_id="D-001", from_time=NOW - timedelta(hours=1), to_time=NOW)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["value"], 6.125)
        self.assertEqual(result["items"][0]["message_id"], "a")

    def test_buffered_old_measurement_is_delayed_despite_fresh_delivery(self):
        self.add("buffered", 1, measured=NOW - timedelta(hours=2))
        health = self.repository.health(now=NOW)
        self.assertEqual(health["links"][0]["status"], "delayed")
        self.assertEqual(health["links"][0]["last_measured_at"], "2026-09-23T02:00:00+00:00")

    def test_old_devices_still_count_in_inventory(self):
        self.add("old", 1, received=NOW - timedelta(days=2), measured=NOW - timedelta(days=2))
        health = self.repository.health(now=NOW)
        self.assertEqual(health["summary"]["device_count"], 1)
        self.assertEqual(health["summary"]["offline_devices"], 1)
        self.assertEqual(health["summary"]["samples_24h"], 0)

    def test_gap_ranges_are_merged_and_late_repair_closes_them(self):
        self.add("wide", 1, 10)
        self.add("overlap", 2, 3)
        self.add("next", 11)
        self.add("gap", 14)
        health = self.repository.health(now=NOW)
        self.assertEqual(health["summary"]["sequence_gap_count_24h"], 1)
        self.assertEqual(health["summary"]["missing_sequences_24h"], "2")
        self.add("repair", 12, 13)
        self.assertEqual(self.repository.health(now=NOW)["summary"]["missing_sequences_24h"], "0")

    def test_boots_do_not_share_sequence_space_and_future_times_are_flagged(self):
        self.add("before", 100, boot="old-boot")
        self.add("after", 1, boot="new-boot", measured=NOW + timedelta(minutes=10))
        health = self.repository.health(now=NOW)
        self.assertEqual(health["summary"]["missing_sequences_24h"], "0")
        self.assertEqual(health["summary"].get("clock_ahead_samples_24h"), 1)

    def test_query_transaction_rejects_persistent_writes(self):
        # Even an INSERT-capable ingestor credential cannot write in the query transaction.
        with self.assertRaises(PostgresRepositoryError) as caught:
            with self.repository._query_connection() as conn:
                conn.execute("INSERT INTO public.telemetry_samples SELECT * FROM public.telemetry_samples WHERE FALSE")
        self.assertIsInstance(caught.exception.__cause__, self.psycopg.errors.ReadOnlySqlTransaction)

    def test_sequences_above_javascript_integer_precision_are_serialized_exactly(self):
        self.add("large-sequence", 9007199254740993)
        result = self.repository.list_samples(from_time=NOW-timedelta(hours=1), to_time=NOW)
        self.assertEqual(result["items"][0]["sequence"], "9007199254740993")
        self.assertEqual(self.repository.health(now=NOW)["links"][0]["last_sequence"], "9007199254740993")

    def test_very_large_sequence_gap_count_is_exact_for_browser_consumers(self):
        self.add("first", 1)
        self.add("last", 9223372036854775807)
        health = self.repository.health(now=NOW)
        self.assertEqual(health["summary"]["missing_sequences_24h"], "9223372036854775805")

    def test_clock_correction_restores_freshness_without_losing_anomaly_count(self):
        self.add("future-clock", 1, received=NOW-timedelta(seconds=50), measured=NOW+timedelta(days=1))
        self.add("corrected", 2, measured=NOW-timedelta(seconds=15))
        health = self.repository.health(now=NOW)
        self.assertEqual(health["links"][0]["status"], "online")
        self.assertEqual(health["links"][0]["last_measured_at"], "2026-09-23T03:59:45+00:00")
        self.assertEqual(health["summary"]["clock_ahead_samples_24h"], 1)

    def test_historical_page_window_excludes_newly_received_backfill(self):
        self.add("old", 1, received=NOW-timedelta(seconds=20), measured=NOW-timedelta(seconds=30))
        self.add("newer", 2, received=NOW-timedelta(seconds=15), measured=NOW-timedelta(seconds=20))
        window = dict(from_time=NOW-timedelta(hours=1), to_time=NOW,
                      received_before=NOW-timedelta(seconds=10), page_size=1)
        first = self.repository.list_samples(**window, page=1)
        self.add("late-backfill", 3, received=NOW-timedelta(seconds=5))
        second = self.repository.list_samples(**window, page=2)
        self.assertEqual((first["total"], second["total"]), (2, 2))
        self.assertEqual([first["items"][0]["message_id"], second["items"][0]["message_id"]], ["newer", "old"])


if __name__ == "__main__":
    unittest.main()
