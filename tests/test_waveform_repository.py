"""Real receiver-role transactions; all payloads are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import base64
import json
from uuid import uuid4

from backend.waveforms.errors import WaveformError
from tests.waveform_support import WaveformDatabaseCase, FIXED_TIME, LIMITS
from tests.waveform_vectors import golden_capture, encoded
from backend.waveforms.contracts import Observation


def capture(gateway="GW", capture_id=None):
    raw, pieces = golden_capture()
    body = json.loads(raw)
    body.update(gateway_id=gateway, capture_id=str(capture_id or uuid4()), capture_kind="event", cycle=None)
    stream = body["streams"][1]
    stream.update(frame_count=1, chunk_frames=1, chunk_count=1, decoded_bytes=10)
    stream["source"]["gateway_id"] = gateway
    piece = json.loads(pieces[-1])
    stream["data_sha256"] = piece["data_sha256"]
    body["streams"] = [stream]
    raw = encoded(body)
    piece.update(gateway_id=gateway, capture_id=body["capture_id"], chunk_index=0,
                 sample_start=0, sample_count=1, manifest_sha256=sha256(raw).hexdigest())
    return raw, encoded(piece)


def observation(payload, *, gateway="GW", transport="mqtt", **kwargs):
    values = dict(transport=transport, topic=f"oil/v2/{gateway}/PUMP-001/waveform", payload=payload,
                  received_at=FIXED_TIME, source_ref=uuid4(), qos=1, retain=False, dup=False)
    if transport == "file":
        values.update(topic=None, qos=None, retain=None, dup=None)
    values.update(kwargs)
    return Observation(**values)


def capture_512():
    """512 one-frame chunks, with a genuine digest of the repeated samples."""
    raw, piece = capture()
    manifest, chunk = json.loads(raw), json.loads(piece)
    data = base64.b64decode(chunk["data_base64"])
    manifest["streams"][0].update(frame_count=512, chunk_count=512,
                                   decoded_bytes=5120, data_sha256=sha256(data * 512).hexdigest())
    raw = encoded(manifest)
    chunk["manifest_sha256"] = sha256(raw).hexdigest()
    return raw, tuple(encoded(chunk | dict(chunk_index=index, sample_start=index)) for index in range(512))


class WaveformRepositoryTests(WaveformDatabaseCase):
    def test_online_unknown_config_is_preserved_as_null(self):
        body=json.loads(capture()[0])
        body['streams'][0]['channels'][0]['config_id']=None
        raw=encoded(body)
        stored=self.repo().ingest(observation(raw))
        self.assertEqual(stored.status,'stored')
        self.assertEqual(self.scalar('SELECT raw_payload FROM waveforms.facts WHERE fact_id=%s',(stored.fact_id,),role='receiver'),raw)
        self.assertIsNone(self.scalar("SELECT specification->'channels'->0->'config_id' FROM waveforms.streams",role='receiver'))

    def repo(self, **kwargs):
        from backend.waveforms.repository import WaveformRepository
        return WaveformRepository(kwargs.pop("connect", self.connect), limits=kwargs.pop("limits", LIMITS),
                                  prefix="oil", ingress_gateway_id=kwargs.pop("gateway", "GW"), **kwargs)

    def count(self, table):
        return self.scalar("SELECT count(*) FROM waveforms." + table, role="receiver")

    def test_duplicate_exact_bytes(self):
        original = observation(capture()[0])
        first = self.repo().ingest(original)
        self.assertFalse(first.duplicate)
        second = self.repo().ingest(original)
        self.assertTrue(second.duplicate)
        self.assertEqual((first.fact_id, first.outcome_id, first.receipt_id),
                         (second.fact_id, second.outcome_id, second.receipt_id))
        self.assertEqual(self.count_facts_for(original), 1)
        self.assertEqual(self.count("observations"), 1)
        self.assertEqual(self.scalar("SELECT duplicate_count FROM waveforms.observations", role="receiver"), 1)

    def test_same_key_reordered_json_conflicts(self):
        raw, _ = capture()
        first = self.repo().ingest(observation(raw))
        reordered = json.dumps(json.loads(raw), sort_keys=False, separators=(",", ": ")).encode()
        conflict = self.repo().ingest(observation(reordered))
        self.assertEqual((conflict.status, conflict.reason_code), ("quarantined", "idempotency_conflict"))
        self.assertEqual(self.scalar("SELECT manifest_fact_id FROM waveforms.captures", role="receiver"), first.fact_id)
        self.assertEqual(self.count("capture_conflicts"), 1)

    def test_orphan_other_manifest_does_not_claim_slot(self):
        raw, chunk = capture()
        wrong = json.loads(chunk)
        wrong["manifest_sha256"] = "f" * 64
        self.repo().ingest(observation(encoded(wrong)))
        self.repo().ingest(observation(raw))
        self.assertEqual(self.count("chunk_slots"), 0)
        valid = self.repo().ingest(observation(chunk))
        self.assertEqual(valid.status, "stored")
        self.assertEqual(self.scalar("SELECT fact_id FROM waveforms.chunk_slots", role="receiver"), valid.fact_id)
        self.assertEqual(self.count("assembly_jobs"), 1)

    def test_wrong_manifest_orphan_does_not_consume_512_valid_slots(self):
        raw, chunks = capture_512()
        wrong = json.loads(chunks[0])
        wrong["manifest_sha256"] = "f" * 64
        orphan = self.repo().ingest(observation(encoded(wrong), transport="file"))
        self.repo().ingest(observation(raw, transport="file"))
        repo = self.repo()
        with self.connect() as conn:
            pending = [repo.store_in_transaction(conn.cursor(), observation(chunk, transport="file"))
                       for chunk in chunks]
            results = repo.commit_pending(conn, pending)
        self.assertEqual([index for index, result in enumerate(results) if result.status != "stored"], [])
        self.assertEqual(self.count("chunks"), 513)
        self.assertEqual(self.count("chunk_slots"), 512)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM waveforms.chunk_slots WHERE fact_id=%s", (orphan.fact_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT requested_generation,finished_generation FROM waveforms.assembly_jobs").fetchone(), (1, 0))
            self.assertEqual(conn.execute("SELECT used_orphan_bytes,used_quarantine_bytes,used_raw_bytes+reserved_raw_bytes FROM waveforms.capacity WHERE bucket='GW'").fetchone(), (1024, 1024, 33554432))
            self.assertEqual(conn.execute("SELECT count(*) FROM waveforms.capture_conflicts WHERE fact_id=%s", (orphan.fact_id,)).fetchone()[0], 1)

    def test_premanifest_orphans_and_rejected_repeat_remain_bounded(self):
        _, chunks = capture_512()
        repo = self.repo()
        with self.connect() as conn:
            pending = [repo.store_in_transaction(conn.cursor(), observation(chunk, transport="file"))
                       for chunk in chunks]
            results = repo.commit_pending(conn, pending)
        self.assertTrue(all(result.status == "stored" for result in results))
        extra = json.loads(chunks[0])
        extra["manifest_sha256"] = "f" * 64
        rejected_observation = observation(encoded(extra), transport="file")
        rejected = repo.ingest(rejected_observation)
        self.assertEqual((rejected.status, rejected.reason_code), ("quarantined", "layout_invalid"))
        for _ in range(3):
            self.assertTrue(repo.ingest(rejected_observation).duplicate)
        self.assertEqual(self.count("chunks"), 512)
        self.assertEqual(self.count("chunk_slots"), 0)
        self.assertEqual(self.count("assembly_jobs"), 0)
        self.assertEqual(self.count("facts"), 513)
        self.assertEqual(self.count("message_outcomes"), 513)
        self.assertEqual(self.count("observations"), 513)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT used_orphan_bytes,used_quarantine_bytes FROM waveforms.capacity WHERE bucket='GW'").fetchone(), (524288, 1024))
            self.assertEqual(conn.execute("SELECT duplicate_count FROM waveforms.observations WHERE outcome_id=%s", (rejected.outcome_id,)).fetchone()[0], 3)

    def test_invalid_same_key_orphan_cannot_block_first_valid_slot(self):
        import struct
        from backend.waveforms.contracts import WaveReceipt
        for invalidity in ("layout", "quality"):
            with self.subTest(invalidity=invalidity):
                raw, chunk = capture()
                manifest, valid = json.loads(raw), json.loads(chunk)
                if invalidity == "quality":
                    manifest["streams"][0]["channels"][0]["encoding"] = "f32le"
                    data = struct.pack("<fB", 1.0, 0) + struct.pack("<iB", 0, 0)
                    manifest["streams"][0]["data_sha256"] = sha256(data).hexdigest()
                    valid.update(data_base64=base64.b64encode(data).decode(), data_sha256=sha256(data).hexdigest())
                raw = encoded(manifest)
                valid["manifest_sha256"] = sha256(raw).hexdigest()
                invalid = valid.copy()
                if invalidity == "layout":
                    invalid["sample_start"] = 1
                else:
                    bad_data = struct.pack("<fB", float("inf"), 0) + struct.pack("<iB", 0, 0)
                    invalid.update(data_base64=base64.b64encode(bad_data).decode(), data_sha256=sha256(bad_data).hexdigest())
                repo = self.repo()
                bad_observation = observation(encoded(invalid))
                good_observation = observation(encoded(valid))
                orphan = repo.ingest(bad_observation)
                self.assertEqual(orphan.status, "stored")
                premature = repo.ingest(good_observation)
                self.assertEqual(premature.reason_code, "idempotency_conflict")
                repo.ingest(observation(raw, transport="file"))
                with self.connect() as conn:
                    pk = conn.execute("SELECT capture_pk FROM waveforms.chunks WHERE fact_id=%s", (orphan.fact_id,)).fetchone()[0]
                    self.assertEqual(conn.execute("SELECT count(*) FROM waveforms.chunk_slots WHERE capture_pk=%s", (pk,)).fetchone()[0], 0)
                    before = conn.execute("SELECT used_orphan_bytes,used_quarantine_bytes,used_facts,used_raw_bytes+reserved_raw_bytes FROM waveforms.capacity WHERE bucket='GW'").fetchone()
                accepted = repo.ingest(good_observation)
                self.assertEqual(accepted.status, "stored")
                self.assertEqual(accepted.fact_id, premature.fact_id)
                self.assertNotEqual(accepted.outcome_id, premature.outcome_id)
                with self.connect() as conn:
                    self.assertEqual(conn.execute("SELECT fact_id FROM waveforms.chunk_slots WHERE capture_pk=%s", (pk,)).fetchone()[0], accepted.fact_id)
                    self.assertEqual(conn.execute("SELECT count(*) FROM waveforms.chunks WHERE capture_pk=%s", (pk,)).fetchone()[0], 2)
                    self.assertEqual(conn.execute("SELECT orphan_bytes,used_raw_bytes+reserved_raw_bytes FROM waveforms.captures WHERE capture_pk=%s", (pk,)).fetchone(), (1024, 33554432))
                    after = conn.execute("SELECT used_orphan_bytes,used_quarantine_bytes,used_facts,used_raw_bytes+reserved_raw_bytes FROM waveforms.capacity WHERE bucket='GW'").fetchone()
                    self.assertEqual(tuple(new-old for new, old in zip(after, before)), (0, 0, 0, -1024))
                    self.assertEqual(conn.execute("SELECT requested_generation,finished_generation FROM waveforms.assembly_jobs WHERE capture_pk=%s", (pk,)).fetchone(), (1, 0))
                    original_receipt = bytes(conn.execute("SELECT payload FROM waveforms.receipts WHERE receipt_id=%s", (orphan.receipt_id,)).fetchone()[0])
                    self.assertEqual(WaveReceipt.from_bytes(original_receipt).status, "stored")
                self.assertEqual(repo.ingest(bad_observation).reason_code, "layout_invalid")
                self.assertTrue(repo.ingest(good_observation).duplicate)
                with self.connect() as conn:
                    self.assertEqual(conn.execute("SELECT used_orphan_bytes,used_quarantine_bytes,used_facts,used_raw_bytes+reserved_raw_bytes FROM waveforms.capacity WHERE bucket='GW'").fetchone(), after)
                    self.assertEqual(conn.execute("SELECT count(*) FROM waveforms.capture_conflicts WHERE capture_pk=%s AND fact_id=%s", (pk, orphan.fact_id)).fetchone()[0], 1)
                reordered = json.dumps(valid, separators=(", ", ":")).encode()
                self.assertEqual(repo.ingest(observation(reordered)).reason_code, "idempotency_conflict")
                with self.connect() as conn:
                    self.assertEqual(conn.execute("SELECT fact_id FROM waveforms.chunk_slots WHERE capture_pk=%s", (pk,)).fetchone()[0], accepted.fact_id)

    def test_rollback_no_receipt(self):
        from backend.waveforms.repository import PendingStoredMessage, StoredMessage
        with self.connect() as conn:
            pending = self.repo().store_in_transaction(conn.cursor(), observation(capture()[0]))
            self.assertIsInstance(pending, PendingStoredMessage)
            self.assertNotIsInstance(pending, StoredMessage)
            conn.rollback()
        self.assertEqual(self.count("facts"), 0)
        self.assertEqual(self.count("receipts"), 0)

    def test_independent_outcomes_both_orders(self):
        for retained_first in (True, False):
            with self.subTest(retained_first=retained_first):
                raw, _ = capture()
                a = self.repo().ingest(observation(raw, retain=retained_first))
                b = self.repo().ingest(observation(raw, retain=not retained_first))
                self.assertEqual(a.fact_id, b.fact_id)
                self.assertNotEqual(a.outcome_id, b.outcome_id)
                self.assertNotEqual(a.receipt_id, b.receipt_id)
                self.assertEqual({a.status, b.status}, {"stored", "quarantined"})

    def test_online_offline_reuse_without_offline_delivery(self):
        for file_first in (True, False):
            with self.subTest(file_first=file_first):
                raw, _ = capture()
                first = self.repo().ingest(observation(raw, transport="file" if file_first else "mqtt"))
                before = self.scalar("SELECT coalesce(sum(requested_generation),0) FROM waveforms.deliveries", role="receiver")
                second = self.repo().ingest(observation(raw, transport="mqtt" if file_first else "file"))
                self.assertEqual((first.fact_id, first.outcome_id), (second.fact_id, second.outcome_id))
                self.assertIsNone((first if file_first else second).receipt_id)
                if not file_first:
                    self.assertEqual(self.scalar("SELECT sum(requested_generation) FROM waveforms.deliveries", role="receiver"), before)

    def test_bad_online_transport_then_legal_file(self):
        raw, _ = capture()
        bad = self.repo().ingest(observation(raw, qos=0))
        good = self.repo().ingest(observation(raw, transport="file"))
        self.assertEqual(good.status, "stored")
        self.assertEqual(bad.fact_id, good.fact_id)
        self.assertNotEqual(bad.outcome_id, good.outcome_id)
        self.assertIsNone(good.receipt_id)

    def test_bad_topic_and_spoof_do_not_invent_receipt_or_capture(self):
        for value in (observation(capture()[0], topic="bad"), observation(capture("OTHER")[0], gateway="OTHER")):
            result = self.repo().ingest(value)
            self.assertEqual(result.intake_gateway_id, "GW")
            self.assertEqual(result.status, "quarantined")
            self.assertIsNone(result.receipt_id)
        self.assertEqual(self.count("captures"), 0)

    def test_outcome_parsed_identity_is_independent_of_transport_arrival_order(self):
        raw, _ = capture()
        mismatched = observation(raw, qos=0, topic="oil/v2/GW/OTHER/waveform")
        result = self.repo().ingest(mismatched)
        self.assertIsNone(result.receipt_id)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT gateway_id,device_id FROM waveforms.message_outcomes WHERE outcome_id=%s", (result.outcome_id,)).fetchone(), ("GW", "PUMP-001"))
        retry = self.repo().ingest(observation(raw, qos=0))
        self.assertEqual(result.outcome_id, retry.outcome_id)
        self.assertIsNotNone(retry.receipt_id)
        self.assertEqual(self.count("captures"), 0)

    def test_hash_match_still_requires_exact_bytes(self):
        from unittest.mock import patch
        raw, _ = capture()
        self.repo().ingest(observation(raw))
        other, _ = capture()
        with patch("backend.waveforms.repository.sha256", return_value=sha256(raw)):
            with self.assertRaises(WaveformError):
                self.repo().ingest(observation(other))
        self.assertEqual(self.count("facts"), 1)

    def test_concurrent_duplicates_and_conflicts(self):
        raw, _ = capture()
        altered = json.dumps(json.loads(raw), separators=(", ", ":")).encode()
        values = [observation(raw)] * 3 + [observation(altered)] * 3
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self.repo().ingest, values))
        self.assertEqual(sum(not r.duplicate for r in results), 2)
        self.assertEqual(self.count("facts"), 2)
        self.assertEqual(self.count("captures"), 1)
        self.assertEqual(self.count("capture_conflicts"), 1)

    def test_input_api_failures_do_not_quarantine(self):
        raw, _ = capture()
        for value in (observation(raw, transport="http"), observation(raw, transport="file", qos=1)):
            with self.assertRaises(WaveformError):
                self.repo().ingest(value)
        self.assertEqual(self.count("facts"), 0)

    def test_offline_sealed_manifest_online_retry_requests_ack_work(self):
        raw, chunk = capture()
        self.repo().ingest(observation(raw, transport="file"))
        self.repo().ingest(observation(chunk, transport="file"))
        with self.connect("assembler") as conn:
            conn.execute("UPDATE waveforms.captures SET state='complete',reserved_raw_bytes=0")
            conn.execute("UPDATE waveforms.capacity SET reserved_raw_bytes=0,used_incomplete_captures=0 WHERE bucket='GW'")
            conn.execute("UPDATE waveforms.assembly_jobs SET finished_generation=requested_generation")
        self.repo().ingest(observation(raw))
        self.assertEqual(self.scalar("SELECT requested_generation-finished_generation FROM waveforms.assembly_jobs", role="receiver"), 1)
        self.assertEqual(self.scalar("SELECT state FROM waveforms.captures", role="receiver"), "complete")
        self.assertEqual(self.scalar("SELECT used_incomplete_captures FROM waveforms.capacity WHERE bucket='GW'", role="receiver"), 0)

    def test_post_seal_conflict_preserves_completion(self):
        raw, chunk = capture()
        self.repo().ingest(observation(raw))
        self.repo().ingest(observation(chunk))
        with self.connect("assembler") as conn:
            conn.execute("INSERT INTO waveforms.completion(capture_pk,intake_gateway_id,manifest_fact_id,status,reason_code,result,completed_at) SELECT capture_pk,intake_gateway_id,manifest_fact_id,'complete','accepted','{}',last_valid_at FROM waveforms.captures")
            conn.execute("UPDATE waveforms.captures SET state='complete',reserved_raw_bytes=0")
            conn.execute("UPDATE waveforms.capacity SET reserved_raw_bytes=0,used_incomplete_captures=0 WHERE bucket='GW'")
        altered = json.dumps(json.loads(chunk), separators=(", ", ":")).encode()
        conflict = self.repo().ingest(observation(altered))
        self.assertEqual(conflict.reason_code, "idempotency_conflict")
        self.assertEqual(self.scalar("SELECT status FROM waveforms.completion", role="assembler"), "complete")
        self.assertEqual(self.count("capture_conflicts"), 1)

    def test_layout_invalid_orphan_retained_and_bounded_revalidation(self):
        raw, chunk = capture()
        invalid = json.loads(chunk)
        invalid["sample_start"] = 1
        orphan = observation(encoded(invalid))
        accepted = self.repo().ingest(orphan)
        self.assertEqual(accepted.status, "stored")
        self.repo().ingest(observation(raw, transport="file"))
        self.assertEqual(self.count("receipts"), 1)
        rejected = self.repo().ingest(orphan)
        self.assertEqual(rejected.reason_code, "layout_invalid")
        for _ in range(3):
            self.repo().ingest(orphan)
        self.assertEqual(self.count("capture_conflicts"), 1)
        self.assertEqual(self.count("message_outcomes"), 3)
        self.assertEqual(self.count("observations"), 3)
        self.assertEqual(self.count("chunk_slots"), 0)
        self.assertEqual(self.scalar("SELECT used_orphan_bytes FROM waveforms.capacity WHERE bucket='GW'", role="receiver"), 1024)
        self.assertEqual(self.scalar("SELECT used_quarantine_bytes FROM waveforms.capacity WHERE bucket='GW'", role="receiver"), 1024)

    def test_quality_layout_is_checked_before_slot(self):
        import base64
        import struct
        raw, chunk = capture()
        manifest = json.loads(raw)
        manifest["streams"][0]["channels"][0]["encoding"] = "f32le"
        data = struct.pack("<fB", float("inf"), 0) + struct.pack("<iB", 0, 0)
        manifest["streams"][0]["data_sha256"] = sha256(data).hexdigest()
        raw = encoded(manifest)
        body = json.loads(chunk)
        body.update(manifest_sha256=sha256(raw).hexdigest(), data_sha256=sha256(data).hexdigest(), data_base64=base64.b64encode(data).decode())
        self.repo().ingest(observation(raw))
        result = self.repo().ingest(observation(encoded(body)))
        self.assertEqual(result.reason_code, "layout_invalid")
        self.assertEqual(self.count("chunk_slots"), 0)

    def test_counters_saturate_without_unbounded_repeat_history(self):
        value = observation(b"invalid")
        self.repo().ingest(value)
        with self.connect() as conn:
            conn.execute("UPDATE waveforms.observations SET duplicate_count=2147483646,anomaly_count=2147483646")
        self.repo().ingest(replace(value, dup=True))
        head = self.scalar("SELECT version FROM waveforms.state_head", role="receiver")
        self.repo().ingest(replace(value, dup=True))
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT duplicate_count,anomaly_count,counters_saturated FROM waveforms.observations").fetchone(), (2147483647, 2147483647, True))
            self.assertEqual(conn.execute("SELECT version FROM waveforms.state_head").fetchone()[0], head)
        self.assertEqual(self.count("observations"), 1)

    def test_existing_capture_receipt_requeues_only_on_accepted_mqtt_manifest(self):
        from backend.waveforms.contracts import WaveReceipt, ACK_SCHEMA
        raw, chunk = capture()
        self.repo().ingest(observation(raw, transport="file"))
        self.repo().ingest(observation(chunk, transport="file"))
        body = json.loads(raw)
        from uuid import UUID
        receipt_id = uuid4()
        payload = WaveReceipt(ACK_SCHEMA, receipt_id, "GW", "PUMP-001", UUID(body["capture_id"]), "capture",
                              sha256(raw).hexdigest(), None, None, None, "complete", "accepted", "2026-09-29T00:00:00.000000Z").to_bytes()
        with self.connect("assembler") as conn:
            conn.execute("UPDATE waveforms.captures SET state='complete',reserved_raw_bytes=0")
            conn.execute("UPDATE waveforms.capacity SET reserved_raw_bytes=0,used_incomplete_captures=0 WHERE bucket='GW'")
            conn.execute("INSERT INTO waveforms.receipts(receipt_id,intake_gateway_id,capture_pk,scope,topic,payload,recorded_at) SELECT %s,'GW',capture_pk,'capture','oil/v2/GW/PUMP-001/waveform-ack',%s,last_valid_at FROM waveforms.captures", (receipt_id,payload))
            conn.execute("INSERT INTO waveforms.deliveries(receipt_id,intake_gateway_id) VALUES(%s,'GW')", (receipt_id,))
        self.repo().ingest(observation(raw, transport="file"))
        self.repo().ingest(observation(chunk))
        self.repo().ingest(observation(raw, retain=True))
        self.assertEqual(self.scalar("SELECT requested_generation FROM waveforms.deliveries WHERE receipt_id=%s", (receipt_id,), role="receiver"), 1)
        self.repo().ingest(observation(raw))
        self.assertEqual(self.scalar("SELECT requested_generation FROM waveforms.deliveries WHERE receipt_id=%s", (receipt_id,), role="receiver"), 2)

    def test_exhausted_delivery_generation_cannot_claim_rescheduled_success(self):
        value = observation(capture()[0])
        self.repo().ingest(value)
        with self.connect() as conn:
            conn.execute("UPDATE waveforms.deliveries SET requested_generation=9223372036854775807")
        with self.connect("receipt_publisher") as conn:
            conn.execute("UPDATE waveforms.deliveries SET sent_generation=9223372036854775807")
        with self.assertRaises(WaveformError):
            self.repo().ingest(value)
        self.assertEqual(self.scalar("SELECT duplicate_count FROM waveforms.observations", role="receiver"), 0)

    def test_exhausted_finished_job_cannot_claim_capture_ack_work(self):
        raw, chunk = capture()
        self.repo().ingest(observation(raw, transport="file"))
        self.repo().ingest(observation(chunk, transport="file"))
        with self.connect() as conn:
            conn.execute("UPDATE waveforms.assembly_jobs SET requested_generation=9223372036854775807")
        with self.connect("assembler") as conn:
            conn.execute("UPDATE waveforms.assembly_jobs SET finished_generation=9223372036854775807")
            conn.execute("UPDATE waveforms.captures SET state='complete',reserved_raw_bytes=0")
            conn.execute("UPDATE waveforms.capacity SET reserved_raw_bytes=0,used_incomplete_captures=0 WHERE bucket='GW'")
        with self.assertRaises(WaveformError):
            self.repo().ingest(observation(raw))
        self.assertEqual(self.count("receipts"), 0)
