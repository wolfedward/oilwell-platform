"""Synthetic captures exercised through real receiver and assembler roles."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from dataclasses import replace
from hashlib import sha256
import json
import multiprocessing
from unittest.mock import patch
from threading import Event
from uuid import uuid4
import psycopg

from tests.waveform_support import WaveformDatabaseCase, FIXED_TIME, LIMITS
from tests.test_waveform_repository import capture, capture_512, observation
from tests.waveform_vectors import encoded
from backend.waveforms.repository import WaveformRepository
from backend.waveforms.contracts import WaveReceipt
from backend.waveforms.errors import WaveformError


def _worker_claim(settings, queue, stop):
    """Child uses only the already pinned fixture's assembler credentials."""
    from backend.waveforms.assembly import Assembler
    assembler = Assembler(lambda: psycopg.connect(**settings), clock=lambda: FIXED_TIME, limits=LIMITS)
    queue.put(assembler.claim(uuid4()))
    stop.wait(30)


class WaveformAssemblyTests(WaveformDatabaseCase):
    def setUp(self):
        super().setUp()
        self.now = FIXED_TIME

    def assembler(self, **kwargs):
        from backend.waveforms.assembly import Assembler
        return Assembler(kwargs.pop("connect", lambda: self.connect("assembler")),
                         clock=kwargs.pop("clock", lambda: self.now),
                         limits=kwargs.pop("limits", LIMITS), **kwargs)

    def ingest(self, payload, **kwargs):
        gateway = kwargs.get("gateway", "GW")
        repo = WaveformRepository(self.connect, limits=kwargs.pop("limits", LIMITS),
                                  prefix="oil", ingress_gateway_id=gateway)
        return repo.ingest(observation(payload, **kwargs))

    def ready(self, *, transport="mqtt", gateway="GW", limits=LIMITS):
        raw, piece = capture(gateway)
        for payload in (raw, piece):
            self.ingest(payload, transport=transport, gateway=gateway, limits=limits)
        return raw, piece

    def result_row(self):
        with self.connect("assembler") as conn:
            return conn.execute("SELECT status,reason_code,result,completed_at FROM waveforms.completion").fetchone()

    def count(self, table):
        return self.scalar("SELECT count(*) FROM waveforms." + table, role="assembler")

    def test_all_chunks_but_missing_samples_still_complete(self):
        raw, piece = capture()
        body, chunk = json.loads(raw), json.loads(piece)
        data = b"\0\0\0\0\1" + b"\3\0\0\0\0"
        body["streams"][0]["data_sha256"] = sha256(data).hexdigest()
        raw = encoded(body)
        chunk.update(manifest_sha256=sha256(raw).hexdigest(), data_sha256=sha256(data).hexdigest(),
                     data_base64=base64.b64encode(data).decode())
        self.ingest(raw)
        assembler = self.assembler()
        self.assertIsNone(assembler.claim(uuid4()))
        self.assertEqual(self.scalar("SELECT state FROM waveforms.captures", role="assembler"), "collecting")
        self.ingest(encoded(chunk))
        lease = assembler.claim(uuid4())
        before = self.scalar("SELECT version FROM waveforms.state_head", role="assembler")
        result = assembler.run(lease)
        self.assertEqual((result.status, result.reason_code), ("complete", "accepted"))
        self.assertEqual(result.manifest_sha256, sha256(raw).hexdigest())
        self.assertEqual(self.result_row()[2]["quality_counts"], {"good": 1, "missing": 1, "clipped": 0, "bad": 0, "uncertain": 0})
        self.assertEqual(self.scalar("SELECT version FROM waveforms.state_head", role="assembler"), before + 1)
        with self.connect("assembler") as conn:
            self.assertEqual(conn.execute("SELECT reserved_raw_bytes,used_incomplete_captures FROM waveforms.capacity WHERE bucket='GW'").fetchone(), (0, 0))
            payload, topic = conn.execute("SELECT payload,topic FROM waveforms.receipts WHERE scope='capture'").fetchone()
        receipt = WaveReceipt.from_bytes(bytes(payload))
        self.assertEqual((receipt.status, receipt.manifest_sha256, topic), ("complete", sha256(raw).hexdigest(), "oil/v2/GW/PUMP-001/waveform-ack"))
        self.assertIsNone(assembler.run(lease))
        self.assertEqual(self.scalar("SELECT version FROM waveforms.state_head", role="assembler"), before + 1)

    def test_full_hash_mismatch_invalid(self):
        raw, piece = capture()
        body, chunk = json.loads(raw), json.loads(piece)
        body["streams"][0]["data_sha256"] = "f" * 64
        raw = encoded(body)
        chunk["manifest_sha256"] = sha256(raw).hexdigest()
        self.ingest(raw)
        self.ingest(encoded(chunk))
        assembler = self.assembler()
        result = assembler.run(assembler.claim(uuid4()))
        self.assertEqual((result.status, result.reason_code), ("invalid", "digest_mismatch"))

    def test_stale_lease_cannot_seal(self):
        self.ready()
        old = self.assembler()
        lease = old.claim(uuid4())
        self.now += timedelta(seconds=31)
        restarted = self.assembler()
        replacement = restarted.claim(uuid4())
        self.assertNotEqual(lease.token, replacement.token)
        self.assertIsNone(old.run(lease))
        self.assertEqual(self.count("completion"), 0)
        self.assertEqual(restarted.run(replacement).status, "complete")
        self.assertIsNone(old.run(lease))

    def test_late_conflict_does_not_rewrite_completion(self):
        _, piece = self.ready()
        assembler = self.assembler()
        result = assembler.run(assembler.claim(uuid4()))
        original = self.result_row()
        rejected = self.ingest(json.dumps(json.loads(piece), separators=(", ", ":")).encode())
        self.assertEqual(rejected.reason_code, "idempotency_conflict")
        self.assertEqual(self.count("capture_conflicts"), 1)
        self.assertEqual(self.result_row(), original)
        self.assertEqual(result.status, "complete")

    def test_two_workers_only_one_claims(self):
        self.ready()
        with ThreadPoolExecutor(max_workers=2) as pool:
            leases = list(pool.map(lambda _: self.assembler().claim(uuid4()), range(2)))
        self.assertEqual(sum(lease is not None for lease in leases), 1)

    def test_offline_seal_then_online_manifest_creates_only_missing_ack(self):
        raw, piece = self.ready(transport="file")
        assembler = self.assembler()
        first = assembler.run(assembler.claim(uuid4()))
        self.assertEqual(self.count("receipts"), 0)
        self.ingest(piece)
        self.ingest(raw, retain=True)
        self.assertIsNone(assembler.claim(uuid4()))
        self.ingest(raw)
        second = assembler.run(assembler.claim(uuid4()))
        self.assertEqual(first, second)
        self.assertEqual(self.count("completion"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.receipts WHERE scope='capture'", role="assembler"), 1)
        self.assertEqual(self.scalar("SELECT used_incomplete_captures FROM waveforms.capacity WHERE bucket='GW'", role="assembler"), 0)

    def test_conflicts_anchored_to_accepted_manifest_and_slot_invalidate(self):
        for which in (0, 1):
            raw, piece = self.ready()
            self.ingest(json.dumps(json.loads((raw, piece)[which]), separators=(", ", ":")).encode())
            assembler = self.assembler()
            result = assembler.run(assembler.claim(uuid4()))
            self.assertEqual((result.status, result.reason_code), ("invalid", "idempotency_conflict"))

    def test_invalid_orphan_evidence_does_not_invalidate_selected_slots(self):
        raw, piece = capture()
        wrong = json.loads(piece)
        wrong["sample_start"] = 1
        self.ingest(encoded(wrong))
        self.ingest(piece)  # conflict against unaccepted orphan remains evidence
        self.ingest(raw)
        self.ingest(piece)
        assembler = self.assembler()
        self.assertEqual(assembler.run(assembler.claim(uuid4())).status, "complete")
        self.assertEqual(self.count("capture_conflicts"), 2)

    def test_renewal_is_fenced_and_capped_at_five_minutes(self):
        self.ready()
        assembler = self.assembler()
        original = lease = assembler.claim(uuid4())
        for seconds in range(20, 300, 20):
            self.now = FIXED_TIME + timedelta(seconds=seconds)
            lease = assembler.renew(lease)
            self.assertIsNotNone(lease)
            self.assertLessEqual(lease.expires_at, FIXED_TIME + timedelta(minutes=5))
        self.assertIsNone(assembler.run(original))
        self.now = FIXED_TIME + timedelta(minutes=5)
        self.assertIsNone(assembler.renew(lease))
        self.assertIsNone(assembler.run(lease))
        self.assertEqual(self.count("completion"), 0)
        self.assertIsNotNone(self.assembler().claim(uuid4()))

    def test_clock_rollback_fences_two_workers_across_restart(self):
        self.ready()
        self.ready()
        first, second = self.assembler(), self.assembler()
        leases = (first.claim(uuid4()), second.claim(uuid4()))
        self.now -= timedelta(seconds=1)
        self.assertIsNone(self.assembler().claim(uuid4()))
        with self.connect("assembler") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM waveforms.assembly_jobs WHERE lease_token IS NOT NULL").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT last_seen,error_code FROM waveforms.worker_health").fetchone(), (FIXED_TIME, "clock_rollback"))
            self.assertEqual(conn.execute("SELECT min(next_attempt_at) FROM waveforms.assembly_jobs").fetchone()[0], FIXED_TIME)
            self.assertEqual(conn.execute("SELECT used_incomplete_captures FROM waveforms.capacity WHERE bucket='GW'").fetchone()[0], 2)
        self.now = FIXED_TIME + timedelta(seconds=1)
        replacement = self.assembler()
        renewed = replacement.claim(uuid4())
        self.assertIsNone(first.run(leases[0]))
        self.assertIsNone(second.run(leases[1]))
        self.assertEqual(replacement.run(renewed).status, "complete")
        self.assertEqual(self.count("worker_health"), 1)

    def test_invalid_clock_rejected_without_claim(self):
        self.ready()
        for value in (None, FIXED_TIME.replace(tzinfo=None), "2026-09-29", FIXED_TIME.astimezone(timezone(timedelta(hours=8)))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.assembler(clock=lambda: value).claim(uuid4())
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.assembly_jobs WHERE lease_token IS NOT NULL", role="assembler"), 0)

    def test_monotonic_expiry_blocks_seal_when_wall_clock_does_not_advance(self):
        self.ready()
        assembler = self.assembler()
        with patch("backend.waveforms.assembly.monotonic", return_value=100):
            lease = assembler.claim(uuid4())
        with patch("backend.waveforms.assembly.monotonic", return_value=131):
            self.assertIsNone(assembler.run(lease))
        self.assertEqual(self.count("completion"), 0)

    def test_temporary_connection_failure_retains_reservation_and_retry(self):
        self.ready()
        assembler = self.assembler()
        lease = assembler.claim(uuid4())
        with patch.object(assembler, "_connect", side_effect=psycopg.OperationalError("injected temporary outage")):
            with self.assertRaises(WaveformError):
                assembler.run(lease)
        self.assertEqual(self.count("completion"), 0)
        self.assertGreater(self.scalar("SELECT reserved_raw_bytes FROM waveforms.capacity WHERE bucket='GW'", role="assembler"), 0)
        self.assertEqual(assembler.run(lease).status, "complete")

    def test_mixed_transport_and_rejected_shared_bytes_keep_content_valid(self):
        raw, piece = capture()
        self.ingest(raw, retain=True)
        self.ingest(raw)
        self.ingest(piece, transport="file")
        assembler = self.assembler()
        self.assertEqual(assembler.run(assembler.claim(uuid4())).status, "complete")
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.receipts WHERE scope='capture'", role="assembler"), 1)

    def test_gateway_filter_claims_b_despite_earlier_a_backlog(self):
        from backend.waveforms.config import CapacityLimits
        limits = CapacityLimits(global_quota=LIMITS.global_quota,
                                gateways={"GW": LIMITS.gateways["GW"], "B": LIMITS.gateways["GW"]})
        with self.capacity_fixture(limits):
            self.ready(limits=limits)
            self.now += timedelta(seconds=1)
            self.ready(gateway="B", limits=limits)
            assembler = self.assembler(limits=limits)
            lease = assembler.claim(uuid4(), gateway_id="B")
            self.assertEqual(lease.key.gateway_id, "B")
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.assembly_jobs WHERE intake_gateway_id='GW' AND lease_token IS NOT NULL", role="assembler"), 0)

    def test_ownership_ceiling_race_expired_reclaim_and_gateway_rollback(self):
        from backend.waveforms.config import CapacityLimits
        quota = replace(LIMITS.gateways["GW"], raw_bytes=33554432*70, incomplete_captures=70)
        limits = CapacityLimits(global_quota=replace(LIMITS.global_quota, raw_bytes=33554432*140, incomplete_captures=140),
                                gateways={"GW": quota, "B": quota})
        with self.capacity_fixture(limits):
            repo = WaveformRepository(self.connect, limits=limits, prefix="oil", ingress_gateway_id="GW")
            with self.connect() as conn:
                pending = []
                for _ in range(66):
                    for payload in capture():
                        pending.append(repo.store_in_transaction(conn.cursor(), observation(payload, transport="file")))
                repo.commit_pending(conn, pending)
            assembler = self.assembler(limits=limits)
            old = [assembler.claim(uuid4(), gateway_id="GW") for _ in range(63)]
            with ThreadPoolExecutor(max_workers=2) as pool:
                raced = list(pool.map(lambda _: self.assembler(limits=limits).claim(uuid4(), gateway_id="GW"), range(2)))
            self.assertEqual(sum(lease is not None for lease in raced), 1)
            self.assertIsNone(assembler.claim(uuid4(), gateway_id="GW"))
            self.ready(gateway="B", limits=limits)
            b_lease = assembler.claim(uuid4(), gateway_id="B")
            self.assertIsNotNone(b_lease)
            self.now += timedelta(seconds=31)
            replacement = assembler.claim(uuid4(), gateway_id="GW")
            self.assertIsNotNone(replacement)
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.assembly_jobs WHERE intake_gateway_id='GW' AND lease_token IS NOT NULL", role="assembler"), 64)
            self.now -= timedelta(seconds=1)
            self.assertIsNone(assembler.claim(uuid4(), gateway_id="GW"))
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.assembly_jobs WHERE intake_gateway_id='GW' AND lease_token IS NOT NULL", role="assembler"), 0)
            self.assertEqual(self.scalar("SELECT lease_token FROM waveforms.assembly_jobs WHERE intake_gateway_id='B'", role="assembler"), b_lease.token)
            self.now += timedelta(seconds=2)
            for lease in old + [item for item in raced if item is not None] + [replacement]:
                self.assertIsNone(assembler.renew(lease))
            self.assertEqual(self.count("completion"), 0)
            # Simulate persisted ownership from an incompatible prior worker;
            # neither ordinary claim nor rollback may silently truncate it.
            with self.connect("assembler") as conn:
                conn.execute("UPDATE waveforms.assembly_jobs SET lease_token=gen_random_uuid(),lease_generation=requested_generation,lease_until=%s,version=version+1 WHERE capture_pk IN (SELECT capture_pk FROM waveforms.assembly_jobs WHERE intake_gateway_id='GW' ORDER BY capture_pk LIMIT 65)", (self.now + timedelta(seconds=30),))
            for clock in (self.now, self.now - timedelta(seconds=2)):
                with self.assertRaises(WaveformError) as caught:
                    self.assembler(limits=limits, clock=lambda: clock).claim(uuid4(), gateway_id="GW")
                self.assertEqual(caught.exception.code, "waveform.invariant")
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.assembly_jobs WHERE intake_gateway_id='GW' AND lease_token IS NOT NULL", role="assembler"), 65)

    def test_conflict_arriving_during_streaming_is_seen_at_final_seal(self):
        _, piece = self.ready()
        assembler = self.assembler()
        lease = assembler.claim(uuid4())
        reading, proceed = Event(), Event()
        original = assembler._read_fact
        def delayed(connection, fact):
            payload = original(connection, fact)
            if payload == piece:
                reading.set()
                if not proceed.wait(10):
                    raise TimeoutError("Concurrent intake failed to progress")
            return payload
        with patch.object(assembler, "_read_fact", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(assembler.run, lease)
            try:
                self.assertTrue(reading.wait(10))
                rejected = self.ingest(json.dumps(json.loads(piece), separators=(", ", ":")).encode())
                self.assertEqual(rejected.reason_code, "idempotency_conflict")
            finally:
                proceed.set()
            self.assertEqual(future.result().reason_code, "idempotency_conflict")

    def test_commit_uncertainty_never_returns_success_or_double_releases(self):
        for after in (False, True):
            raw, _ = self.ready()
            assembler = self.assembler()
            lease = assembler.claim(uuid4())
            head = self.scalar("SELECT version FROM waveforms.state_head", role="assembler")
            def connect():
                real = self.connect("assembler")
                class Connection:
                    def __getattr__(inner, name):
                        return getattr(real, name)
                    def __enter__(inner):
                        return real
                    def __exit__(inner, exc_type, exc, tb):
                        if exc_type is None and real.execute("SELECT count(*) FROM waveforms.completion WHERE capture_pk=%s", (lease.capture_pk,)).fetchone()[0]:
                            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.completion WHERE capture_pk=%s", (lease.capture_pk,), role="assembler"), 0)
                            if after:
                                real.commit()
                            else:
                                real.rollback()
                            real.close()
                            raise psycopg.OperationalError("Injected lost commit response")
                        return real.__exit__(exc_type, exc, tb)
                return Connection()
            with patch.object(assembler, "_connect", side_effect=connect), self.assertRaises(WaveformError):
                assembler.run(lease)
            self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.completion WHERE capture_pk=%s", (lease.capture_pk,), role="assembler"), int(after))
            self.assertEqual(self.scalar("SELECT version FROM waveforms.state_head", role="assembler"), head + int(after))
            if after:
                self.assertIsNone(assembler.run(lease))
                self.ingest(raw)
            else:
                self.assertEqual(assembler.run(lease).status, "complete")
            with self.connect("assembler") as conn:
                self.assertEqual(conn.execute("SELECT reserved_raw_bytes,used_incomplete_captures FROM waveforms.capacity WHERE bucket='GW'").fetchone(), (0, 0))

    def test_newer_requested_generation_remains_pending(self):
        self.ready()
        assembler = self.assembler()
        lease = assembler.claim(uuid4())
        with self.connect() as conn:
            conn.execute("UPDATE waveforms.assembly_jobs SET requested_generation=requested_generation+1")
        self.assertEqual(assembler.run(lease).status, "complete")
        with self.connect("assembler") as conn:
            self.assertEqual(conn.execute("SELECT requested_generation,finished_generation FROM waveforms.assembly_jobs").fetchone(), (2, 1))
        self.assertEqual(assembler.run(assembler.claim(uuid4())).status, "complete")
        self.assertEqual(self.count("completion"), 1)

    def test_final_transaction_expiry_rolls_back_completion_receipt_and_release(self):
        self.ready()
        assembler = self.assembler()
        lease = assembler.claim(uuid4())
        original = assembler._receipt
        before = self.scalar("SELECT version FROM waveforms.state_head", role="assembler")
        def delayed(*args):
            original(*args)
            self.now += timedelta(seconds=31)
        with patch.object(assembler, "_receipt", side_effect=delayed):
            self.assertIsNone(assembler.run(lease))
        self.assertEqual(self.count("completion"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.receipts WHERE scope='capture'", role="assembler"), 0)
        self.assertGreater(self.scalar("SELECT reserved_raw_bytes FROM waveforms.capacity WHERE bucket='GW'", role="assembler"), 0)
        self.assertEqual(self.scalar("SELECT version FROM waveforms.state_head", role="assembler"), before)
        self.assertEqual(assembler.run(assembler.claim(uuid4())).status, "complete")

    def test_killed_worker_recovery_uses_persisted_ownership(self):
        self.ready()
        context = multiprocessing.get_context("spawn")
        queue, stop = context.Queue(), context.Event()
        process = context.Process(target=_worker_claim, args=(self.settings["assembler"], queue, stop))
        try:
            process.start()
            lease = queue.get(timeout=15)
            process.terminate()
            process.join(timeout=10)
            self.assertFalse(process.is_alive())
            self.assertIsNone(self.assembler().claim(uuid4()))
            self.now += timedelta(seconds=31)
            restarted = self.assembler()
            replacement = restarted.claim(uuid4())
            self.assertNotEqual(replacement.token, lease.token)
            self.assertIsNone(restarted.run(lease))
            self.assertEqual(restarted.run(replacement).status, "complete")
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
            queue.close()

    def test_512_slots_assemble_with_retained_extra_orphan(self):
        raw, chunks = capture_512()
        bad = json.loads(chunks[0])
        bad["manifest_sha256"] = "f"*64
        self.ingest(encoded(bad), transport="file")
        self.ingest(raw, transport="file")
        repo = WaveformRepository(self.connect, limits=LIMITS, prefix="oil", ingress_gateway_id="GW")
        with self.connect() as conn:
            pending = [repo.store_in_transaction(conn.cursor(), observation(chunk, transport="file")) for chunk in chunks]
            repo.commit_pending(conn, pending)
        assembler = self.assembler()
        self.assertEqual(assembler.run(assembler.claim(uuid4())).status, "complete")
        self.assertEqual(self.count("chunks"), 513)
        self.assertEqual(self.count("chunk_slots"), 512)
        self.assertEqual(self.result_row()[2]["quality_counts"]["good"], 1024)
        with self.connect("assembler") as conn:
            self.assertEqual(conn.execute("SELECT used_orphan_bytes,used_quarantine_bytes,reserved_raw_bytes,used_incomplete_captures FROM waveforms.capacity WHERE bucket='GW'").fetchone(), (1024, 1024, 0, 0))

    def test_layout_precedes_digest_and_anchored_conflict(self):
        raw, piece = capture()
        manifest, bad = json.loads(raw), json.loads(piece)
        manifest["streams"][0]["data_sha256"] = "f"*64
        raw = encoded(manifest)
        bad.update(manifest_sha256=sha256(raw).hexdigest(), sample_start=1)
        orphan = self.ingest(encoded(bad))
        alternate = self.ingest(json.dumps(bad, separators=(", ", ":")).encode())
        self.assertEqual(alternate.reason_code, "idempotency_conflict")
        self.ingest(raw)
        # Model a producer index defect using permitted INSERTs, without
        # rewriting immutable data or disabling any constraint/trigger.
        with self.connect() as conn:
            conn.execute("INSERT INTO waveforms.chunk_slots(capture_pk,intake_gateway_id,stream_id,chunk_index,fact_id) SELECT capture_pk,intake_gateway_id,stream_id,chunk_index,fact_id FROM waveforms.chunks WHERE fact_id=%s", (orphan.fact_id,))
            conn.execute("INSERT INTO waveforms.assembly_jobs(capture_pk,intake_gateway_id,next_attempt_at) SELECT capture_pk,intake_gateway_id,%s FROM waveforms.captures", (self.now,))
        assembler = self.assembler()
        self.assertEqual(assembler.run(assembler.claim(uuid4())).reason_code, "layout_invalid")

    def test_digest_precedes_anchored_conflict(self):
        raw, piece = capture()
        body, chunk = json.loads(raw), json.loads(piece)
        body["streams"][0]["data_sha256"] = "f"*64
        raw = encoded(body)
        chunk["manifest_sha256"] = sha256(raw).hexdigest()
        self.ingest(raw)
        self.ingest(encoded(chunk))
        self.ingest(json.dumps(chunk, separators=(", ", ":")).encode())
        assembler = self.assembler()
        self.assertEqual(assembler.run(assembler.claim(uuid4())).reason_code, "digest_mismatch")

    def test_rollback_during_final_transaction_fences_all_without_sealing(self):
        self.ready()
        self.ready()
        assembler = self.assembler()
        lease, other = assembler.claim(uuid4()), assembler.claim(uuid4())
        read, receipt = assembler._read_fact, assembler._receipt
        def advance(*args):
            payload = read(*args)
            self.now = FIXED_TIME + timedelta(seconds=5)
            return payload
        def retreat(*args):
            receipt(*args)
            self.now = FIXED_TIME + timedelta(seconds=4)
        with patch.object(assembler, "_read_fact", side_effect=advance), patch.object(assembler, "_receipt", side_effect=retreat):
            self.assertIsNone(assembler.run(lease))
        self.assertEqual(self.count("completion"), 0)
        with self.connect("assembler") as conn:
            self.assertEqual(conn.execute("SELECT last_seen,error_code FROM waveforms.worker_health").fetchone(), (FIXED_TIME + timedelta(seconds=5), "clock_rollback"))
            self.assertEqual(conn.execute("SELECT count(*) FROM waveforms.assembly_jobs WHERE lease_token IS NOT NULL").fetchone()[0], 0)
        self.now += timedelta(seconds=2)
        self.assertIsNone(assembler.run(other))
        self.assertEqual(assembler.run(assembler.claim(uuid4())).status, "complete")

    def test_production_database_clock_and_real_stalled_view(self):
        with self.connect("assembler") as conn:
            now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        raw, piece = capture()
        old = now - timedelta(days=2)
        self.ingest(raw, received_at=old)
        bad = json.loads(piece)
        bad["sample_start"] = 1
        self.ingest(encoded(bad), received_at=now)
        self.ingest(raw, received_at=now)
        self.assertTrue(self.scalar("SELECT stalled FROM waveforms.capture_metadata"))
        self.assertEqual(self.scalar("SELECT last_valid_at FROM waveforms.captures", role="assembler"), old)
        self.ingest(piece, received_at=now)
        self.assertFalse(self.scalar("SELECT stalled FROM waveforms.capture_metadata"))
        assembler = self.assembler(clock=None)
        lease = assembler.claim(uuid4())
        self.assertGreaterEqual(lease.claimed_at, now)
        self.assertEqual((lease.expires_at-lease.claimed_at).total_seconds(), 30)
        self.assertEqual(assembler.run(lease).status, "complete")
        self.assertIsNone(self.scalar("SELECT last_clock_seen FROM waveforms.capacity WHERE bucket='GW'", role="assembler"))

    def test_shared_durability_guard_prevents_assembly_writes(self):
        from backend.waveforms import capacity
        self.ready()
        for settings in (("off", "on", "on"), ("on", "off", "on")):
            with patch.object(capacity, "_durability_settings", return_value=settings), self.assertRaises(WaveformError):
                self.assembler().claim(uuid4())
        self.assertEqual(self.count("worker_health"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.assembly_jobs WHERE lease_token IS NOT NULL", role="assembler"), 0)

    def test_missing_eligible_manifest_message_receipt_rolls_back_seal(self):
        self.ready(transport="file")
        # A deliberately incomplete producer dependency, using real permitted
        # observation INSERT rather than deleting immutable receipt evidence.
        with self.connect() as conn:
            conn.execute("INSERT INTO waveforms.observations(observation_id,fact_id,outcome_id,intake_gateway_id,transport,source_ref,topic,qos,retain,dup,first_seen,last_seen) SELECT %s,manifest_fact_id,manifest_outcome_id,intake_gateway_id,'mqtt',%s,'oil/v2/GW/PUMP-001/waveform',1,false,false,%s,%s FROM waveforms.captures", (uuid4(), uuid4(), self.now, self.now))
        assembler = self.assembler()
        with self.assertRaises(WaveformError) as caught:
            assembler.run(assembler.claim(uuid4()))
        self.assertEqual(caught.exception.code, "waveform.invariant")
        self.assertEqual(self.count("completion"), 0)
        self.assertEqual(self.count("receipts"), 0)
        self.assertGreater(self.scalar("SELECT reserved_raw_bytes FROM waveforms.capacity WHERE bucket='GW'", role="assembler"), 0)

    def test_final_clock_observation_is_persisted_as_high_water(self):
        self.ready()
        self.ready()
        assembler = self.assembler()
        lease = assembler.claim(uuid4())
        assembler.claim(uuid4())
        receipt = assembler._receipt
        def advance(*args):
            receipt(*args)
            self.now += timedelta(seconds=5)
        with patch.object(assembler, "_receipt", side_effect=advance):
            self.assertEqual(assembler.run(lease).status, "complete")
        self.assertEqual(self.scalar("SELECT last_seen FROM waveforms.worker_health", role="assembler"), self.now)
        self.now -= timedelta(seconds=1)
        self.assertIsNone(assembler.claim(uuid4()))
        self.assertEqual(self.scalar("SELECT error_code FROM waveforms.worker_health", role="assembler"), "clock_rollback")
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.assembly_jobs WHERE lease_token IS NOT NULL", role="assembler"), 0)
