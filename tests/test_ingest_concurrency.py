"""Competing real connections, never separate fixtures racing their resets."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib
from threading import Barrier

from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_support import IngestDatabaseCase, LIMITS
from tests.ingest_vectors import observation


class ConcurrentRepositoryTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        self.module = importlib.import_module("backend.ingest_reliability.repository")

    def race(self, observations):
        barrier = Barrier(len(observations))
        def run(obs):
            def connect():
                connection = self.connect()
                barrier.wait(timeout=10)
                return connection
            repository = self.module.ReliableRepository(connect, prefix="oilfield",
                receipt_gateways=frozenset({"GW"}), limits=LIMITS)
            try:
                return repository.ingest(obs)
            except ReliabilityError as error:
                return error
        with ThreadPoolExecutor(max_workers=len(observations)) as executor:
            futures = [executor.submit(run, obs) for obs in observations]
            return [future.result(timeout=15) for future in futures]

    def test_same_raw_race_has_one_fact_receipt_and_measurement(self):
        results = self.race([observation()] * 4)
        self.assertTrue(all(not isinstance(item, ReliabilityError) for item in results))
        self.assertEqual(len({item.fact_id for item in results}), 1)
        self.assertEqual(len({item.receipt_id for item in results}), 1)
        self.assertEqual(sum(not item.duplicate for item in results), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 1)
        self.assertEqual(self.scalar("SELECT duplicate_count FROM ingest_reliability.fact_observations", role="migrator"), 3)

    def test_different_bytes_same_id_race_preserves_the_winner_and_isolates_the_other(self):
        results = self.race([observation(value=40.0), observation(value=43.0)])
        self.assertEqual(sorted(item.status for item in results), ["quarantined", "stored"])
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.facts", role="migrator"), 2)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.receipts", role="migrator"), 2)

    def test_total_quota_cannot_be_overdrawn_by_two_accepted_transactions(self):
        first, second = observation("M-A"), observation("M-B")
        self.assertEqual(len(first.payload), len(second.payload))
        with self.connect("migrator") as conn:
            conn.execute("UPDATE ingest_reliability.capacity SET used_total_bytes=total_bytes-%s", (len(first.payload),))
        results = self.race([first, second])
        self.assertEqual(sum(isinstance(item, ReliabilityError) for item in results), 1)
        self.assertEqual(self.scalar("SELECT used_total_bytes FROM ingest_reliability.capacity", role="migrator"), LIMITS.total_bytes)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.facts", role="migrator"), 1)

    def test_quarantine_subquota_cannot_be_overdrawn_or_misreported_as_success(self):
        with self.connect("migrator") as conn:
            conn.execute("UPDATE ingest_reliability.capacity SET used_total_bytes=quarantine_bytes-3,used_quarantine_bytes=quarantine_bytes-3")
        results = self.race([replace(observation(), payload=b"bad"), replace(observation(), payload=b"err")])
        self.assertEqual(sum(isinstance(item, ReliabilityError) for item in results), 1)
        self.assertEqual(self.scalar("SELECT used_quarantine_bytes FROM ingest_reliability.capacity", role="migrator"), LIMITS.quarantine_bytes)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.cases", role="migrator"), 1)
