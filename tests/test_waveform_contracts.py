import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
import struct
import unittest
from uuid import UUID

from backend.waveforms.contracts import Observation, ReceiptExpectation, WaveReceipt
from backend.waveforms.errors import ContractError
from backend.waveforms.samples import iter_samples
from backend.waveforms.wire import classify, parse_frame
from tests.waveform_vectors import encoded, golden_capture


class WaveformContractTests(unittest.TestCase):
    def test_exact_manifest_digest(self):
        raw, chunks = golden_capture()
        self.assertEqual(parse_frame(raw).raw_sha256, sha256(raw).hexdigest())
        self.assertEqual(parse_frame(chunks[0]).sample_start, 0)
        self.assertNotEqual(parse_frame(raw).raw_sha256, parse_frame(json.dumps(json.loads(raw), indent=2).encode()).raw_sha256)
        with self.assertRaises(ContractError) as caught:
            parse_frame(b'{"schema":true,"schema":false}')
        self.assertEqual(caught.exception.code, "json_duplicate_key")

    def test_integer_and_layout_bounds(self):
        raw, chunks = golden_capture()
        body = json.loads(raw)
        for path, value, code in (
            (("window_ns",), "60000000001", "layout_invalid"),
            (("utc_uncertainty_ns",), "-1", "contract_invalid"),
            (("streams", 0, "first_sample_index"), "9223372036854775807", "layout_invalid"),
            (("streams", 0, "rate_den"), 1000001, "contract_invalid"),
            (("streams", 0, "frame_bytes"), 14, "layout_invalid"),
            (("streams", 0, "rate_num"), True, "contract_invalid"),
            (("streams", 0, "channels"), [], "layout_invalid"),
        ):
            with self.subTest(path=path, value=value):
                changed = json.loads(raw)
                target = changed
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                with self.assertRaises(ContractError) as caught:
                    parse_frame(encoded(changed))
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(len(parse_frame(raw).streams), 2)
        self.assertEqual(parse_frame(chunks[-1]).sample_count, 1)

    def test_missing_not_zero(self):
        _, chunks = golden_capture()
        chunk = json.loads(chunks[-1])
        data = bytearray(base64.b64decode(chunk["data_base64"]))
        data[:5] = b"\x00\x00\x00\x00\x01"
        chunk["data_base64"] = base64.b64encode(data).decode()
        chunk["data_sha256"] = sha256(data).hexdigest()
        parsed = parse_frame(encoded(chunk))
        stream = parse_frame(golden_capture()[0]).streams[1]
        sample = next(iter_samples(stream, parsed.data, sample_start=parsed.sample_start))
        self.assertIsNone(sample.raw_value)
        self.assertEqual(sample.quality, 1)
        data[0] = 1
        chunk["data_base64"] = base64.b64encode(data).decode()
        chunk["data_sha256"] = sha256(data).hexdigest()
        with self.assertRaises(ContractError) as caught:
            parse_frame(encoded(chunk))
        self.assertEqual(caught.exception.code, "layout_invalid")

    def test_receipt_scopes(self):
        raw, chunks = golden_capture()
        manifest = parse_frame(raw)
        piece = parse_frame(chunks[0])
        receipt = WaveReceipt(schema="OWP-WAVE-ACK/1.0", receipt_id=UUID("55555555-5555-4555-8555-555555555555"),
                              gateway_id=manifest.key.gateway_id, device_id=manifest.key.device_id,
                              capture_id=manifest.key.capture_id, scope="message", manifest_sha256=manifest.raw_sha256,
                              payload_sha256=sha256(chunks[0]).hexdigest(), stream_id="electrical", chunk_index=0,
                              status="stored", reason_code="accepted", recorded_at="2026-09-29T00:00:01.000000Z")
        decoded = WaveReceipt.from_bytes(receipt.to_bytes())
        expected = ReceiptExpectation(prefix="oilfield", key=manifest.key, scope="message", manifest_sha256=manifest.raw_sha256,
                                      payload_sha256=sha256(chunks[0]).hexdigest(), stream_id="electrical", chunk_index=0)
        self.assertTrue(decoded.matches("oilfield/v2/GW-001/PUMP-001/waveform-ack", expected=expected))
        self.assertFalse(decoded.matches("oilfield/v2/GW-001/PUMP-001/waveform-ack",
                                         expected=ReceiptExpectation(prefix="oilfield", key=manifest.key, scope="message",
                                                                     manifest_sha256=manifest.raw_sha256,
                                                                     payload_sha256=sha256(chunks[1]).hexdigest(),
                                                                     stream_id="electrical", chunk_index=0)))
        self.assertEqual(piece.manifest_sha256, manifest.raw_sha256)
        for status, scope in (("complete", "message"), ("stored", "capture")):
            with self.assertRaises(ContractError):
                WaveReceipt(schema=receipt.schema, receipt_id=receipt.receipt_id,
                            gateway_id=receipt.gateway_id, device_id=receipt.device_id,
                            capture_id=receipt.capture_id, scope=scope,
                            manifest_sha256=receipt.manifest_sha256, payload_sha256=receipt.payload_sha256,
                            stream_id=receipt.stream_id, chunk_index=receipt.chunk_index, status=status,
                            reason_code="accepted", recorded_at=receipt.recorded_at)

    def test_classification_priority_and_identity(self):
        raw, _ = golden_capture()
        observation = Observation("mqtt", "oilfield/v2/GW-001/PUMP-001/waveform", raw,
                                  datetime(2026, 9, 29, tzinfo=timezone.utc), UUID("66666666-6666-4666-8666-666666666666"),
                                  1, False, False)
        self.assertIsNone(classify(observation, prefix="oilfield").reason_code)
        self.assertEqual(classify(Observation("mqtt", "bad", raw, observation.received_at,
                                               observation.source_ref, 0, True, False), prefix="oilfield").reason_code,
                         "topic_invalid")
        changed = json.loads(raw)
        changed["device_id"] = "OTHER"
        mismatched = Observation("mqtt", observation.topic, encoded(changed), observation.received_at,
                                 observation.source_ref, 1, False, False)
        self.assertEqual(classify(mismatched, prefix="oilfield").reason_code, "identity_mismatch")

    def test_depth_and_nonfinite_float_samples(self):
        raw, chunks = golden_capture()
        with self.assertRaises(ContractError) as caught:
            parse_frame(b"[" * 17 + b"0" + b"]" * 17)
        self.assertEqual(caught.exception.code, "contract_invalid")
        stream_body = json.loads(raw)
        stream_body["streams"][1]["channels"][0]["encoding"] = "f32le"
        stream = parse_frame(encoded(stream_body)).streams[1]
        original = parse_frame(chunks[-1]).data
        for bits in (0x7f800000, 0xff800000, 0x7f800001, 0x7fc00000,
                     0x7fffffff, 0xff800001, 0xffc00000, 0xffffffff):
            with self.subTest(bits=bits):
                sample = struct.pack("<I", bits) + b"\x00" + original[5:]
                with self.assertRaises(ContractError) as caught:
                    list(iter_samples(stream, sample, sample_start=1000))
                self.assertEqual(caught.exception.code, "layout_invalid")

    def test_chunk_digest_and_canonical_base64(self):
        _, chunks = golden_capture()
        body = json.loads(chunks[-1])
        body["data_sha256"] = "0" * 64
        with self.assertRaises(ContractError) as caught:
            parse_frame(encoded(body))
        self.assertEqual(caught.exception.code, "digest_mismatch")
        body = json.loads(chunks[-1])
        body["data_base64"] += "\n"
        with self.assertRaises(ContractError) as caught:
            parse_frame(encoded(body))
        self.assertEqual(caught.exception.code, "contract_invalid")

    def test_receipt_rejects_unbound_or_imprecise_response(self):
        raw, chunks = golden_capture()
        manifest = parse_frame(raw)
        base = {"schema": "OWP-WAVE-ACK/1.0", "receipt_id": "55555555-5555-4555-8555-555555555555",
                "gateway_id": manifest.key.gateway_id, "device_id": manifest.key.device_id,
                "capture_id": str(manifest.key.capture_id), "scope": "capture",
                "manifest_sha256": manifest.raw_sha256, "payload_sha256": None,
                "stream_id": None, "chunk_index": None, "status": "complete",
                "reason_code": "accepted", "recorded_at": "2026-09-29T00:00:01.000000Z"}
        self.assertEqual(WaveReceipt.from_bytes(encoded(base)).status, "complete")
        for field, value in (("recorded_at", "2026-09-29T00:00:01Z"),
                             ("manifest_sha256", None), ("payload_sha256", sha256(chunks[0]).hexdigest()),
                             ("reason_code", "contract_invalid")):
            with self.subTest(field=field):
                changed = dict(base, **{field: value})
                with self.assertRaises(ContractError):
                    WaveReceipt.from_bytes(encoded(changed))

    def test_overlong_decimal_is_a_contract_rejection(self):
        body = json.loads(golden_capture()[0])
        body["window_ns"] = "9" * 5000
        with self.assertRaises(ContractError) as caught:
            parse_frame(encoded(body))
        self.assertEqual(caught.exception.code, "contract_invalid")

    def test_cycle_event_stream_must_carry_reference_channel(self):
        body = json.loads(golden_capture()[0])
        body["cycle"]["reference_events"][0]["stream_id"] = "electrical"
        with self.assertRaises(ContractError) as caught:
            parse_frame(encoded(body))
        self.assertEqual(caught.exception.code, "layout_invalid")

    def test_exact_wire_size_limits(self):
        raw, chunks = golden_capture()
        exact_manifest = raw + b" " * (65_536 - len(raw))
        self.assertEqual(parse_frame(exact_manifest).raw_sha256, sha256(exact_manifest).hexdigest())
        with self.assertRaises(ContractError) as caught:
            parse_frame(exact_manifest + b" ")
        self.assertEqual(caught.exception.code, "payload_too_large")
        exact_chunk = chunks[0] + b" " * (262_144 - len(chunks[0]))
        self.assertEqual(parse_frame(exact_chunk).sample_start, 0)
        with self.assertRaises(ContractError) as caught:
            parse_frame(exact_chunk + b" ")
        self.assertEqual(caught.exception.code, "payload_too_large")

    def test_manifest_capacity_boundaries(self):
        body = json.loads(golden_capture()[0])
        body["window_ns"] = "60000000000"
        self.assertEqual(len(parse_frame(encoded(body)).streams), 2)
        body["window_ns"] = "60000000001"
        with self.assertRaises(ContractError) as caught:
            parse_frame(encoded(body))
        self.assertEqual(caught.exception.code, "layout_invalid")
        event = json.loads(golden_capture()[0])
        event["capture_kind"] = "event"
        event["cycle"] = None
        stream = event["streams"][0]
        stream["channels"] = stream["channels"][:1]
        stream["frame_bytes"] = 5
        stream["rate_num"] = 128000
        stream["frame_count"] = 3_355_443
        stream["chunk_frames"] = 26_214
        stream["chunk_count"] = (stream["frame_count"] + 26_213) // 26_214
        stream["decoded_bytes"] = stream["frame_count"] * 5
        event["streams"] = [stream]
        event["window_ns"] = "30000000000"
        self.assertEqual(parse_frame(encoded(event)).streams[0].decoded_bytes, 16_777_215)
        stream["frame_count"] += 1
        stream["decoded_bytes"] += 5
        with self.assertRaises(ContractError) as caught:
            parse_frame(encoded(event))
        self.assertEqual(caught.exception.code, "layout_invalid")

    def test_signed_pretrigger_window_and_overflow(self):
        body = json.loads(golden_capture()[0])
        for stream in body["streams"]:
            stream["offset_ns"] = "-1000000000"
        body["cycle"]["start_ns"] = "-1000000000"
        body["cycle"]["turn_ns"] = "4000000000"
        body["cycle"]["end_ns"] = "9000000000"
        self.assertEqual(parse_frame(encoded(body)).streams[0].offset_ns, -1_000_000_000)
        body["streams"][0]["offset_ns"] = "-1000000001"
        with self.assertRaises(ContractError) as caught:
            parse_frame(encoded(body))
        self.assertEqual(caught.exception.code, "layout_invalid")
        body = json.loads(golden_capture()[0])
        body["streams"][0]["offset_ns"] = "9223372036854775807"
        with self.assertRaises(ContractError) as caught:
            parse_frame(encoded(body))
        self.assertEqual(caught.exception.code, "layout_invalid")
        body = json.loads(golden_capture()[0])
        body["window_ns"] = "0"
        with self.assertRaises(ContractError) as caught:
            parse_frame(encoded(body))
        self.assertEqual(caught.exception.code, "contract_invalid")


if __name__ == "__main__":
    unittest.main()
