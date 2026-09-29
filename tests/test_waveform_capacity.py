"""Capacity and durable-ack boundaries against the actual receiver database."""
from dataclasses import replace
from unittest.mock import patch
from backend.waveforms.errors import WaveformError
from backend.waveforms.config import CapacityLimits
import psycopg
from tests.waveform_support import WaveformDatabaseCase, LIMITS
from tests.test_waveform_repository import capture, observation


class WaveformCapacityTests(WaveformDatabaseCase):
    def repo(self, **kwargs):
        from backend.waveforms.repository import WaveformRepository
        return WaveformRepository(kwargs.pop("connect", self.connect), limits=kwargs.pop("limits", LIMITS),
                                  ingress_gateway_id=kwargs.pop("gateway", "GW"), prefix="oil")

    def usage(self):
        with self.connect() as conn:
            return conn.execute("SELECT used_raw_bytes,reserved_raw_bytes,used_facts,used_orphan_bytes,used_quarantine_bytes FROM waveforms.capacity WHERE bucket='GW'").fetchone()

    def test_orphan_transfer_and_exact_billing(self):
        raw, chunk = capture()
        self.repo().ingest(observation(chunk))
        self.assertEqual(self.usage(), (1024, 0, 1, 1024, 0))
        self.repo().ingest(observation(raw))
        # Hand-derived fixture: 1326 manifest bytes and a 403-byte chunk billed at 1024.
        self.assertEqual(self.usage(), (2350, 33552082, 2, 0, 0))

    def test_quarantine_membership_survives_acceptance_without_raw_double_charge(self):
        raw, _ = capture()
        self.repo().ingest(observation(raw, retain=True))
        self.repo().ingest(observation(raw))
        self.assertEqual(self.usage(), (1326, 33553106, 1, 0, 1326))

    def test_transaction_overrides_session_async_commit(self):
        def connect():
            conn = self.connect()
            conn.execute("SET SESSION synchronous_commit=off")
            conn.commit()
            return conn
        repo = self.repo(connect=connect)
        with connect() as conn:
            pending = repo.store_in_transaction(conn.cursor(), observation(capture()[0]))
            self.assertEqual(conn.execute("SHOW synchronous_commit").fetchone()[0], "on")
            result = repo.commit_pending(conn, pending)
            self.assertEqual(result.status, "stored")
            self.assertEqual(conn.execute("SHOW synchronous_commit").fetchone()[0], "off")

    def test_unsafe_settings_fail_before_writes(self):
        from backend.waveforms import capacity
        for settings in (("off", "on", "on"), ("on", "off", "on")):
            with patch.object(capacity, "_durability_settings", return_value=settings):
                with self.assertRaises(WaveformError):
                    self.repo().ingest(observation(capture()[0]))
        self.assertEqual(self.usage(), (0, 0, 0, 0, 0))

    def test_gateway_capacity_isolated(self):
        quota = replace(LIMITS.gateways["GW"], raw_bytes=2048, orphan_bytes=1024,
                        orphan_bytes_per_capture=1024, quarantine_bytes=2048)
        limits = CapacityLimits(LIMITS.global_quota, {"GW": quota, "OTHER": quota})
        with self.capacity_fixture(limits):
            a, b = self.repo(limits=limits), self.repo(limits=limits, gateway="OTHER")
            a.ingest(observation(b"bad"))
            a.ingest(observation(b"bad2"))
            with self.assertRaises(WaveformError):
                a.ingest(observation(b"bad3"))
            b.ingest(observation(b"bad", gateway="OTHER"))
            self.assertEqual(self.scalar("SELECT used_raw_bytes FROM waveforms.capacity WHERE bucket='OTHER'", role="receiver"), 1024)
            self.assertEqual(self.scalar("SELECT used_raw_bytes+reserved_raw_bytes+used_facts+used_metadata_rows FROM waveforms.capacity WHERE bucket='*'", role="receiver"), 0)

    def test_byte_capacity_boundaries(self):
        cases = ("raw_bytes", "quarantine_bytes", "orphan_bytes", "orphan_bytes_per_capture")
        for field in cases:
            for limit in (1023, 1024, 1025):
                with self.subTest(field=field, limit=limit):
                    changes = {field: limit}
                    if field == "raw_bytes":
                        changes.update(orphan_bytes=1, orphan_bytes_per_capture=1, quarantine_bytes=limit)
                    if field == "orphan_bytes":
                        changes["orphan_bytes_per_capture"] = limit
                    quota = replace(LIMITS.gateways["GW"], **changes)
                    limits = CapacityLimits(LIMITS.global_quota, {"GW": quota})
                    with self.capacity_fixture(limits):
                        payload = capture()[1] if field.startswith("orphan") else b"bad"
                        if limit == 1023:
                            with self.assertRaises(WaveformError):
                                self.repo(limits=limits).ingest(observation(payload))
                            self.assertEqual(self.usage(), (0, 0, 0, 0, 0))
                        else:
                            self.repo(limits=limits).ingest(observation(payload))
                            self.assertEqual(self.usage()[0], 1024)

    def test_reservation_boundaries(self):
        for limit in (33554431, 33554432, 33554433):
            with self.subTest(limit=limit):
                limits = CapacityLimits(LIMITS.global_quota, {"GW": replace(LIMITS.gateways["GW"], raw_bytes=limit)})
                with self.capacity_fixture(limits):
                    if limit < 33554432:
                        with self.assertRaises(WaveformError):
                            self.repo(limits=limits).ingest(observation(capture()[0]))
                    else:
                        self.repo(limits=limits).ingest(observation(capture()[0]))
                        self.assertEqual(sum(self.usage()[:2]), 33554432)

    def test_fact_incomplete_and_metadata_bounds(self):
        for field, payloads, minimum in (("facts", [b"bad", b"other"], 2),
                                          ("incomplete_captures", [capture()[0], capture()[0]], 2),
                                          ("metadata_rows", [b"bad"], 2)):
            for limit in (minimum-1, minimum, minimum+1):
                with self.subTest(field=field, limit=limit):
                    limits = CapacityLimits(LIMITS.global_quota, {"GW": replace(LIMITS.gateways["GW"], **{field: limit})})
                    with self.capacity_fixture(limits):
                        for index, payload in enumerate(payloads):
                            if limit < minimum and index == len(payloads)-1:
                                with self.assertRaises(WaveformError):
                                    self.repo(limits=limits).ingest(observation(payload))
                            else:
                                self.repo(limits=limits).ingest(observation(payload))

    def test_accept_then_reject_exact_billing(self):
        raw, _ = capture()
        self.repo().ingest(observation(raw))
        self.repo().ingest(observation(raw, retain=True))
        self.assertEqual(self.usage(), (1326, 33553106, 1, 0, 1326))

    def test_failed_external_store_cannot_commit_partial_unbilled_facts(self):
        limits = CapacityLimits(LIMITS.global_quota, {"GW": replace(LIMITS.gateways["GW"], raw_bytes=33554431)})
        with self.capacity_fixture(limits):
            with self.connect() as conn:
                original = self.repo(limits=limits).store_in_transaction(conn.cursor(), observation(b"bad"))
                with self.assertRaises(WaveformError):
                    self.repo(limits=limits).store_in_transaction(conn.cursor(), observation(capture()[0]))
                self.repo(limits=limits).commit_pending(conn, original)
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.facts", role="receiver"), 1)
            self.assertEqual(self.usage(), (1024, 0, 1, 0, 1024))

    def test_wrong_orphan_retains_membership_and_uses_capture_allocation_once(self):
        import json
        from tests.waveform_vectors import encoded
        raw, chunk = capture()
        body = json.loads(chunk)
        body["manifest_sha256"] = "f" * 64
        self.repo().ingest(observation(encoded(body)))
        original = observation(raw)
        self.repo().ingest(original)
        self.assertEqual(self.usage(), (2350, 33552082, 2, 1024, 1024))
        self.repo().ingest(original)
        self.assertEqual(self.usage(), (2350, 33552082, 2, 1024, 1024))
        self.repo().ingest(observation(chunk))
        self.assertEqual(self.usage(), (3374, 33551058, 3, 1024, 1024))

    def test_shared_quarantined_chunk_consumes_reservation_without_new_raw(self):
        raw, chunk = capture()
        self.repo().ingest(observation(raw))
        self.repo().ingest(observation(chunk, retain=True))
        self.assertEqual(self.usage(), (2350, 33553106, 2, 0, 1024))
        self.repo().ingest(observation(chunk))
        self.assertEqual(self.usage(), (2350, 33552082, 2, 0, 1024))

    def test_external_batch_keeps_import_line_outcomes_and_commit_boundary(self):
        from uuid import uuid4
        from psycopg.types.json import Jsonb
        from hashlib import sha256
        from backend.waveforms.wire import parse_frame
        raw, chunk = capture()
        repo = self.repo()
        import_id = uuid4()
        with self.connect() as conn:
            pending = [repo.store_in_transaction(conn.cursor(), observation(payload, transport="file")) for payload in (raw, chunk)]
            key = parse_frame(raw).key
            conn.execute("INSERT INTO waveforms.imports(import_id,intake_gateway_id,actor,file_sha256,file_bytes,state,line_count,recorded_at,final_lf,device_id,capture_id) VALUES(%s,'GW',%s,%s,%s,'recorded',2,%s,false,%s,%s)",
                         (import_id, Jsonb(dict(domain="test",user_id="1",username="synthetic",role_code="operator")), sha256(raw+b'\n'+chunk).hexdigest(), len(raw)+len(chunk)+1, observation(raw).received_at, key.device_id,key.capture_id))
            for index, result in enumerate(pending, 1):
                conn.execute("INSERT INTO waveforms.import_lines(import_id,intake_gateway_id,line_number,fact_id,outcome_id) VALUES(%s,%s,%s,%s,%s)",
                             (import_id,result.intake_gateway_id,index,result.fact_id,result.outcome_id))
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.import_lines", role="receiver"), 0)
            stored = repo.commit_pending(conn, pending)
        self.assertEqual(len(stored), 2)
        self.assertTrue(all(result.receipt_id is None for result in stored))
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.import_lines l JOIN waveforms.message_outcomes o USING(outcome_id,fact_id,intake_gateway_id)", role="receiver"), 2)

    def test_unlogged_storage_rejected_without_data_loss(self):
        try:
            with self.connect("migration_owner") as conn:
                conn.execute("ALTER TABLE waveforms.state_head SET UNLOGGED")
            with self.assertRaises(WaveformError):
                self.repo().ingest(observation(capture()[0]))
            self.assertEqual(self.usage(), (0, 0, 0, 0, 0))
        finally:
            with self.connect("migration_owner") as conn:
                conn.execute("ALTER TABLE waveforms.state_head SET LOGGED")

    def test_pending_cannot_convert_after_rollback(self):
        repo = self.repo()
        with self.connect() as conn:
            pending = repo.store_in_transaction(conn.cursor(), observation(capture()[0]))
            conn.rollback()
            with self.assertRaises(WaveformError):
                repo.commit_pending(conn, pending)
            conn.execute("SELECT 1")
            with self.assertRaises(WaveformError):
                repo.commit_pending(conn, pending)

    def test_pending_cannot_convert_after_savepoint_rollback(self):
        repo = self.repo()
        with self.connect() as conn:
            conn.execute("SAVEPOINT caller_scope")
            pending = repo.store_in_transaction(conn.cursor(), observation(capture()[0]))
            conn.execute("ROLLBACK TO SAVEPOINT caller_scope")
            with self.assertRaises(WaveformError):
                repo.commit_pending(conn, pending)

    def test_pending_cannot_convert_after_earlier_commit(self):
        repo = self.repo()
        with self.connect() as conn:
            pending = repo.store_in_transaction(conn.cursor(), observation(capture()[0]))
            conn.commit()
            conn.execute("SELECT 1")
            with self.assertRaises(WaveformError):
                repo.commit_pending(conn, pending)

    def test_guard_bounds_waits_and_preserves_stricter_settings(self):
        from backend.waveforms.capacity import prepare_transaction
        with self.connect() as conn:
            conn.execute("SET LOCAL synchronous_commit=remote_apply")
            prepare_transaction(conn.cursor())
            self.assertEqual(conn.execute("SHOW synchronous_commit").fetchone()[0], "remote_apply")
            self.assertEqual(conn.execute("SHOW lock_timeout").fetchone()[0], "3s")
            self.assertEqual(conn.execute("SHOW statement_timeout").fetchone()[0], "15s")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            conn.execute("SET LOCAL statement_timeout='200ms'")
            prepare_transaction(conn.cursor())
            self.assertEqual(conn.execute("SHOW lock_timeout").fetchone()[0], "100ms")
            self.assertEqual(conn.execute("SHOW statement_timeout").fetchone()[0], "200ms")

    def test_commit_failures_before_and_after_server_commit(self):
        for after in (False, True):
            with self.subTest(after=after):
                value = observation(capture()[0])
                real = self.connect()
                class Cursor:
                    def __init__(inner, target, conn):
                        inner.target, inner.connection = target, conn
                    def __getattr__(inner, name): return getattr(inner.target, name)
                    def __enter__(inner): return inner
                    def __exit__(inner, *args): inner.target.close()
                class Connection:
                    def __getattr__(inner, name): return getattr(real, name)
                    def cursor(inner): return Cursor(real.cursor(), inner)
                    def __enter__(inner): return inner
                    def __exit__(inner, *args): return real.__exit__(*args)
                    def commit(inner):
                        # Another real connection must see no uncommitted fact.
                        self.assertEqual(self.count_facts_for(value), 0)
                        if after: real.commit()
                        raise psycopg.OperationalError("synthetic commit uncertainty")
                with self.assertRaises(WaveformError):
                    self.repo(connect=Connection).ingest(value)
                self.assertEqual(self.count_facts_for(value), int(after))
                retry = self.repo().ingest(value)
                self.assertEqual(retry.duplicate, after)
                self.assertEqual(self.count_facts_for(value), 1)
