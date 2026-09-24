"""Atomic durability and stable facts, using only the isolated real database."""

from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import importlib
import json
import unittest
from unittest.mock import patch

import psycopg

from backend.center.postgres import PostgresTelemetryRepository
from backend.gateway.contracts import UplinkBatch
from backend.ingest_reliability.contracts import Receipt
from backend.ingest_reliability.errors import ReliabilityError
from backend.ingest_reliability.wire import classify
from tests.ingest_support import IngestDatabaseCase, LIMITS
from tests.ingest_vectors import observation


MAX_COUNTER = 9_223_372_036_854_775_807


class InjectedWriteFailure(RuntimeError):
    pass


class FaultCursor:
    def __init__(self, cursor, fragment):
        self.cursor, self.fragment = cursor, fragment

    def __enter__(self):
        self.cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self.cursor.__exit__(*args)

    def execute(self, statement, *args, **kwargs):
        if self.fragment in " ".join(str(statement).split()):
            raise InjectedWriteFailure("injected transaction write failure")
        self.cursor.execute(statement, *args, **kwargs)
        return self

    def executemany(self, statement, *args, **kwargs):
        if self.fragment in " ".join(str(statement).split()):
            raise InjectedWriteFailure("injected transaction write failure")
        return self.cursor.executemany(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class FaultConnection:
    def __init__(self, connection, fragment=None, lost_commit_response=False):
        self.connection, self.fragment = connection, fragment
        self.lost_commit_response = lost_commit_response

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        result = self.connection.__exit__(*args)
        if args[0] is None and self.lost_commit_response:
            raise psycopg.OperationalError("injected lost commit response")
        return result

    def cursor(self, *args, **kwargs):
        return FaultCursor(self.connection.cursor(*args, **kwargs), self.fragment or "NEVER_MATCH")

    def __getattr__(self, name):
        return getattr(self.connection, name)


class RepositoryConfigTests(unittest.TestCase):
    def test_legacy_invalid_utf8_still_fails_before_opening_a_database_connection(self):
        def forbidden_factory(_dsn, _settings):
            self.fail("legacy invalid UTF-8 must fail before connecting")
        repository = PostgresTelemetryRepository(connect_kwargs={"dbname": "not-used"}, connect_factory=forbidden_factory)
        obs = observation()
        with self.assertRaises(UnicodeDecodeError):
            repository.store(UplinkBatch.from_json(obs.payload), topic=obs.topic, payload_sha256="0" * 64,
                             raw_payload=b"\xff", received_at=obs.received_at)

    def test_constructor_checks_configuration_without_opening_database_connections(self):
        module = importlib.import_module("backend.ingest_reliability.repository")
        def forbidden_connect():
            self.fail("constructor must not connect or migrate")
        module.ReliableRepository(forbidden_connect, prefix="oilfield", receipt_gateways=frozenset(), limits=LIMITS)
        changes = ({"prefix": "bad/prefix"}, {"prefix": ""}, {"prefix": "a" * 65},
                   {"receipt_gateways": "GW"}, {"receipt_gateways": frozenset({"../other"})},
                   {"receipt_gateways": frozenset({True})}, {"limits": None}, {"connect": None})
        for change in changes:
            kwargs = dict(connect=forbidden_connect, prefix="oilfield", receipt_gateways=frozenset({"GW"}), limits=LIMITS)
            kwargs.update(change)
            with self.subTest(change=change), self.assertRaises((ValueError, ReliabilityError)):
                module.ReliableRepository(**kwargs)


class RepositoryTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        self.module = importlib.import_module("backend.ingest_reliability.repository")

    def one(self, statement, params=(), role="migrator"):
        with self.connect(role) as conn:
            return conn.execute(statement, params).fetchone()

    def empty(self):
        for table in ("facts", "cases", "fact_observations", "receipts", "deliveries", "audit_events"):
            self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability." + table, role="migrator"), 0, table)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.ingest_messages", role="migrator"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 0)
        self.assertEqual(self.one("SELECT used_total_bytes,used_quarantine_bytes FROM ingest_reliability.capacity"), (0, 0))

    def faulty_repo(self, fragment=None, *, lost_commit_response=False):
        return self.module.ReliableRepository(
            lambda: FaultConnection(self.connect(), fragment, lost_commit_response),
            prefix="oilfield", receipt_gateways=frozenset({"GW"}), limits=LIMITS)

    def contract_bad(self):
        obs = observation()
        return replace(obs, payload=json.dumps(dict(json.loads(obs.payload), samples=[])).encode())

    def legacy_store(self, obs, *, topic=None):
        PostgresTelemetryRepository(connect_kwargs=self.settings["migrator"]).store(
            UplinkBatch.from_json(obs.payload), topic=topic or obs.topic,
            payload_sha256=sha256(obs.payload).hexdigest(), raw_payload=obs.payload, received_at=obs.received_at)

    def test_new_acceptance_commits_samples_exact_raw_and_receipt_together(self):
        obs = observation()
        result = self.repository().ingest(obs)
        self.assertEqual((result.status, result.duplicate), ("stored", False))
        raw = self.one("SELECT raw_payload,payload_sha256,status,accepted_message_id FROM ingest_reliability.facts WHERE fact_id=%s",
                       (result.fact_id,))
        self.assertEqual(raw, (obs.payload, sha256(obs.payload).hexdigest(), "stored", "M-1"))
        topic, payload = self.one("SELECT topic,payload FROM ingest_reliability.receipts WHERE receipt_id=%s", (result.receipt_id,))
        receipt = Receipt.from_bytes(payload)
        self.assertTrue(receipt.matches(topic, obs))
        self.assertEqual(receipt.status, "stored")
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 1)
        self.assertEqual(self.one("SELECT used_total_bytes,used_quarantine_bytes FROM ingest_reliability.capacity"),
                         (len(obs.payload), 0))

    def test_duplicate_reuses_result_but_different_bytes_are_quarantined(self):
        repo, obs = self.repository(), observation()
        first, second = repo.ingest(obs), repo.ingest(obs)
        changed = repo.ingest(replace(obs, payload=obs.payload + b" "))
        self.assertEqual((first.status, second.fact_id, second.receipt_id, second.duplicate),
                         ("stored", first.fact_id, first.receipt_id, True))
        self.assertEqual(changed.status, "quarantined")
        self.assertEqual(self.scalar("SELECT reason_code FROM ingest_reliability.facts WHERE fact_id=%s",
                                     (changed.fact_id,), role="migrator"), "idempotency_conflict")
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 1)
        self.assertEqual(self.scalar("SELECT duplicate_count FROM ingest_reliability.fact_observations WHERE fact_id=%s",
                                     (first.fact_id,), role="migrator"), 1)

    def test_cross_gateway_reused_id_is_quarantined_without_exposing_the_old_identity(self):
        self.repository().ingest(observation())
        obs = observation(gateway="OTHER")
        result = self.repository(receipt_gateways=frozenset({"OTHER"})).ingest(obs)
        payload = self.scalar("SELECT payload FROM ingest_reliability.receipts WHERE receipt_id=%s", (result.receipt_id,), role="publisher")
        receipt = Receipt.from_bytes(payload)
        self.assertEqual((receipt.gateway_id, receipt.reason_code), ("OTHER", "idempotency_conflict"))
        self.assertTrue(receipt.matches(receipt.topic("oilfield"), obs))
        self.assertEqual(self.scalar("SELECT gateway_id FROM public.ingest_messages", role="migrator"), "GW")

    def test_bad_json_is_preserved_without_guessing_a_reply_target(self):
        obs = replace(observation(), payload=b'{"gateway_id":"GW","message_id":"M-1",broken')
        result = self.repository().ingest(obs)
        self.assertEqual((result.status, result.receipt_id), ("quarantined", None))
        self.assertEqual(self.scalar("SELECT raw_payload FROM ingest_reliability.facts", role="migrator"), obs.payload)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.cases", role="migrator"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 0)

    def test_correlatable_contract_error_produces_rejection_and_no_samples(self):
        obs = self.contract_bad()
        result = self.repository().ingest(obs)
        payload = self.scalar("SELECT payload FROM ingest_reliability.receipts WHERE receipt_id=%s", (result.receipt_id,), role="publisher")
        receipt = Receipt.from_bytes(payload)
        self.assertEqual((receipt.status, receipt.reason_code), ("quarantined", "contract_invalid"))
        self.assertTrue(receipt.matches(receipt.topic("oilfield"), obs))
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 0)

    def test_empty_allowlist_still_persists_facts_and_duplicate_statistics(self):
        repo, obs = self.repository(receipt_gateways=frozenset()), observation()
        for _ in range(3):
            result = repo.ingest(obs)
            self.assertIsNone(result.receipt_id)
        self.assertEqual(self.scalar("SELECT duplicate_count FROM ingest_reliability.fact_observations", role="migrator"), 2)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.receipts", role="migrator"), 0)

    def test_allowlist_removal_does_not_delete_or_change_an_existing_receipt(self):
        first = self.repository().ingest(observation())
        second = self.repository(receipt_gateways=frozenset()).ingest(observation())
        self.assertEqual(second.receipt_id, first.receipt_id)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.receipts", role="migrator"), 1)

    def test_enabling_receipts_later_reuses_the_persisted_fact_without_reinserting_samples(self):
        obs = observation()
        first = self.repository(receipt_gateways=frozenset()).ingest(obs)
        second = self.repository().ingest(obs)
        self.assertEqual((second.fact_id, second.status, second.duplicate), (first.fact_id, "stored", True))
        self.assertIsNotNone(second.receipt_id)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.receipts", role="migrator"), 1)

    def test_already_pending_receipt_requests_coalesce_without_replacing_the_original_body(self):
        obs = observation()
        first = self.repository().ingest(obs)
        body = self.scalar("SELECT payload FROM ingest_reliability.receipts", role="publisher")
        for _ in range(3):
            result = self.repository().ingest(obs)
            self.assertEqual(result.receipt_id, first.receipt_id)
        self.assertEqual(self.one("SELECT requested_generation,sent_generation FROM ingest_reliability.deliveries"), (1, 0))
        self.assertEqual(self.scalar("SELECT payload FROM ingest_reliability.receipts", role="publisher"), body)

    def test_receipt_request_during_a_lease_cannot_be_lost_by_its_completion(self):
        from uuid import uuid4
        obs = observation()
        self.repository().ingest(obs)
        with self.connect("publisher") as conn:
            conn.execute("UPDATE ingest_reliability.deliveries SET lease_token=%s,lease_generation=1,"
                         "lease_until=CURRENT_TIMESTAMP+interval '30 seconds'", (uuid4(),))
        self.repository().ingest(obs)
        self.assertEqual(self.one("SELECT requested_generation,sent_generation,lease_generation FROM ingest_reliability.deliveries"), (2, 0, 1))

    def test_generation_exhaustion_never_wraps_or_changes_evidence(self):
        obs = observation()
        self.repository().ingest(obs)
        with self.connect("migrator") as conn:
            conn.execute("UPDATE ingest_reliability.deliveries SET requested_generation=%s,sent_generation=%s",
                         (MAX_COUNTER, MAX_COUNTER))
        with self.assertRaises(ReliabilityError):
            self.repository().ingest(obs)
        self.assertEqual(self.one("SELECT requested_generation,sent_generation FROM ingest_reliability.deliveries"),
                         (MAX_COUNTER, MAX_COUNTER))
        self.assertEqual(self.scalar("SELECT duplicate_count FROM ingest_reliability.fact_observations", role="migrator"), 0)

    def test_database_constraint_failure_is_not_reclassified_as_bad_input(self):
        # No partition exists for this measurement. It is syntactically valid
        # telemetry, but cannot be accepted by the configured storage layout.
        obs = observation()
        body = json.loads(obs.payload)
        body["samples"][0]["measured_at"] = "1990-01-01T00:00:00+00:00"
        obs = replace(obs, payload=json.dumps(body).encode())
        self.assertIsNone(classify(obs, prefix="oilfield").reason_code)
        with self.assertRaises((ReliabilityError, psycopg.Error)):
            self.repository().ingest(obs)
        self.empty()

    def test_every_accepted_write_failure_rolls_back_all_evidence_and_capacity(self):
        fragments = ("INSERT INTO ingest_messages", "INSERT INTO telemetry_samples", "INSERT INTO ingest_reliability.facts",
                     "INSERT INTO ingest_reliability.fact_observations", "INSERT INTO ingest_reliability.receipts",
                     "INSERT INTO ingest_reliability.deliveries", "UPDATE ingest_reliability.capacity")
        for fragment in fragments:
            with self.subTest(fragment=fragment), self.assertRaises(InjectedWriteFailure):
                self.faulty_repo(fragment).ingest(observation())
            self.empty()

    def test_every_quarantine_write_failure_rolls_back_the_whole_transaction(self):
        for fragment in ("INSERT INTO ingest_reliability.cases", "INSERT INTO ingest_reliability.receipts",
                         "INSERT INTO ingest_reliability.deliveries", "UPDATE ingest_reliability.capacity"):
            with self.subTest(fragment=fragment), self.assertRaises(InjectedWriteFailure):
                self.faulty_repo(fragment).ingest(self.contract_bad())
            self.empty()

    def test_commit_response_lost_is_recovered_by_a_new_connection_without_duplicate_samples(self):
        obs = observation()
        with self.assertRaises((ReliabilityError, psycopg.Error)):
            self.faulty_repo(lost_commit_response=True).ingest(obs)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 1)
        result = self.repository().ingest(obs)
        self.assertTrue(result.duplicate)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.facts", role="migrator"), 1)

    def test_legacy_jsonb_only_record_gets_exact_bytes_only_from_matching_retransmission(self):
        obs = observation()
        self.legacy_store(obs)
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.facts", role="migrator"), 0)
        result = self.repository().ingest(obs)
        self.assertEqual(result.status, "stored")
        self.assertEqual(self.scalar("SELECT raw_payload FROM ingest_reliability.facts", role="migrator"), obs.payload)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 1)

    def test_matching_legacy_acceptance_wins_over_later_transport_flags(self):
        obs = observation()
        self.legacy_store(obs)
        result = self.repository().ingest(replace(obs, qos=2, retain=True))
        self.assertEqual(result.status, "stored")
        self.assertEqual(self.scalar("SELECT transport_anomaly_count FROM ingest_reliability.fact_observations", role="migrator"), 1)

    def test_legacy_same_hash_but_different_topic_cannot_be_adopted(self):
        obs = observation()
        self.legacy_store(obs, topic="other/v1/GW/D/telemetry")
        result = self.repository().ingest(obs)
        self.assertEqual(result.status, "quarantined")
        self.assertEqual(self.scalar("SELECT reason_code FROM ingest_reliability.facts", role="migrator"), "idempotency_conflict")

    def test_committed_rejection_is_not_reclassified_after_validator_change(self):
        obs = observation()
        candidate = replace(classify(obs, prefix="oilfield"), reason_code="contract_invalid", validator_version="older/1")
        with patch.object(self.module, "classify", return_value=candidate):
            original = self.repository().ingest(obs)
        later = self.repository().ingest(obs)
        self.assertEqual((original.status, later.status, later.fact_id), ("quarantined", "quarantined", original.fact_id))
        self.assertEqual(self.scalar("SELECT validator_version FROM ingest_reliability.facts", role="migrator"), "older/1")
        self.assertEqual(self.scalar("SELECT count(*) FROM public.telemetry_samples", role="migrator"), 0)

    def test_committed_acceptance_stays_stored_with_later_transport_anomaly(self):
        obs = observation()
        first = self.repository().ingest(obs)
        later = self.repository().ingest(replace(obs, qos=0, retain=True, dup=True))
        self.assertEqual((later.status, later.fact_id, later.receipt_id), ("stored", first.fact_id, first.receipt_id))
        self.assertEqual(self.scalar("SELECT transport_anomaly_count FROM ingest_reliability.fact_observations", role="migrator"), 1)

    def test_unknown_device_and_bad_quality_are_stored_without_any_registry(self):
        obs = observation(gateway="UNKNOWN", device="UNBOUND", quality="bad")
        result = self.repository().ingest(obs)
        self.assertEqual(result.status, "stored")
        self.assertEqual(self.scalar("SELECT quality FROM public.telemetry_samples", role="migrator"), "bad")

    def test_closed_quarantine_reappearance_reopens_and_appends_system_audit(self):
        obs = self.contract_bad()
        first = self.repository().ingest(obs)
        with self.connect("operator") as conn:
            conn.execute("UPDATE ingest_reliability.cases SET state='closed',version=2 WHERE fact_id=%s", (first.fact_id,))
        later = self.repository().ingest(obs)
        self.assertEqual((later.fact_id, later.receipt_id), (first.fact_id, first.receipt_id))
        self.assertEqual(self.one("SELECT state,version FROM ingest_reliability.cases"), ("open", 3))
        action, actor = self.one("SELECT action,actor FROM ingest_reliability.audit_events")
        self.assertEqual(action, "case.repeat_reopen")
        self.assertEqual(actor["domain"], "system")

    def test_duplicate_counters_saturate_without_breaking_ingestion(self):
        obs = observation()
        first = self.repository().ingest(obs)
        with self.connect("migrator") as conn:
            conn.execute("UPDATE ingest_reliability.fact_observations SET duplicate_count=%s,transport_anomaly_count=%s",
                         (MAX_COUNTER, MAX_COUNTER))
        result = self.repository().ingest(replace(obs, retain=True))
        self.assertEqual(result.fact_id, first.fact_id)
        self.assertEqual(self.one("SELECT duplicate_count,transport_anomaly_count,counters_saturated FROM ingest_reliability.fact_observations"),
                         (MAX_COUNTER, MAX_COUNTER, True))

    def test_repeated_fact_needs_no_additional_payload_budget(self):
        obs = observation()
        first = self.repository().ingest(obs)
        with self.connect("migrator") as conn:
            conn.execute("UPDATE ingest_reliability.capacity SET used_total_bytes=total_bytes")
        result = self.repository().ingest(obs)
        self.assertEqual(result.fact_id, first.fact_id)
        self.assertEqual(self.scalar("SELECT used_total_bytes FROM ingest_reliability.capacity", role="migrator"), LIMITS.total_bytes)

    def test_capacity_failure_is_not_quarantine_and_can_be_retried_after_capacity_recovers(self):
        with self.connect("migrator") as conn:
            conn.execute("UPDATE ingest_reliability.capacity SET used_total_bytes=total_bytes")
        with self.assertRaises(ReliabilityError):
            self.repository().ingest(observation())
        self.assertEqual(self.scalar("SELECT count(*) FROM ingest_reliability.facts", role="migrator"), 0)
        with self.connect("migrator") as conn:
            conn.execute("UPDATE ingest_reliability.capacity SET used_total_bytes=0")
        self.assertEqual(self.repository().ingest(observation()).status, "stored")

    def test_empty_payload_is_preserved_and_has_a_positive_capacity_charge(self):
        obs = replace(observation(), payload=b"")
        result = self.repository().ingest(obs)
        self.assertEqual(result.status, "quarantined")
        self.assertEqual(self.one("SELECT raw_payload,payload_bytes FROM ingest_reliability.facts"), (b"", 0))
        self.assertEqual(self.one("SELECT used_total_bytes,used_quarantine_bytes FROM ingest_reliability.capacity"), (1, 1))

    def test_backwards_observation_does_not_regress_first_and_last_seen(self):
        obs = observation()
        self.repository().ingest(obs)
        self.repository().ingest(replace(obs, received_at=obs.received_at-timedelta(seconds=30)))
        self.assertEqual(self.one("SELECT first_seen,last_seen FROM ingest_reliability.fact_observations"),
                         (obs.received_at, obs.received_at))

    def test_invalid_server_clock_and_oversize_hard_limit_never_persist_a_rejection(self):
        for obs in (replace(observation(), received_at=observation().received_at.replace(tzinfo=None)),
                    replace(observation(), payload=b"x" * 2_097_153)):
            with self.subTest(length=len(obs.payload)), self.assertRaises(ReliabilityError):
                self.repository().ingest(obs)
            self.empty()
