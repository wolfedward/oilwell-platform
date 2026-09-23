"""Published OWP-EDGE rules must also hold at the actual ingestion boundary."""

from datetime import datetime, timezone
import json
from pathlib import Path
import runpy
import unittest

from backend.center.ingest import CentralIngestService, IngestValidationError
from backend.gateway.contracts import ContractValidationError, PointDefinition, TelemetrySample, UplinkBatch


KIT = Path(__file__).resolve().parents[1] / "assets" / "open-interface"
TOPIC = "oilfield/v1/GW-EXAMPLE/D-EXAMPLE/telemetry"
NOW = datetime(2026, 9, 23, 8, tzinfo=timezone.utc)


class RecordingRepository:
    def __init__(self):
        self.calls = []

    def store(self, batch, **kwargs):
        self.calls.append((batch, kwargs))
        return True


class WireValidationTests(unittest.TestCase):
    def setUp(self):
        self.payload = (KIT / "telemetry-example.json").read_bytes()
        self.repo = RecordingRepository()
        self.service = CentralIngestService(self.repo, clock=lambda: NOW)
        self.preflight = runpy.run_path(str(KIT / "validate_telemetry.py"))["validate"]

    def changed(self, change):
        body = json.loads(self.payload)
        change(body)
        return json.dumps(body).encode("utf-8")

    def reject_at_both_boundaries(self, payload):
        self.repo = RecordingRepository()
        self.service = CentralIngestService(self.repo, clock=lambda: NOW)
        with self.assertRaises(ValueError):
            self.preflight(TOPIC, payload)
        with self.assertRaises(IngestValidationError):
            self.service.ingest(TOPIC, payload)
        self.assertEqual(self.repo.calls, [], "invalid input must not reach persistence")

    def test_duplicate_keys_cannot_hide_an_envelope_or_sample_value(self):
        for original, replacement in ((b'"schema":', b'"schema":"wrong","schema":'),
                                      (b'"value":', b'"value":999,"value":'),
                                      (b'"first":', b'"first":99,"first":')):
            with self.subTest(field=original):
                self.reject_at_both_boundaries(self.payload.replace(original, replacement, 1))

    def test_sequence_summary_requires_integer_tokens_not_boolean_or_float(self):
        for field in ("first", "last"):
            for value in (True, 1.0):
                with self.subTest(field=field, value=value):
                    self.reject_at_both_boundaries(self.changed(lambda b: b["sequence"].update({field: value})))

    def test_sequence_overflow_is_rejected_before_postgres_bigint_write(self):
        def overflow(body):
            body["sequence"] = {"first": 9223372036854775808, "last": 9223372036854775808}
            for row in body["samples"]:
                row["sequence"] = 9223372036854775808
        self.reject_at_both_boundaries(self.changed(overflow))

    def test_unit_type_length_and_jsonb_unicode_are_checked_before_write(self):
        for value in ({"unit": "kW"}, 1, "", "x" * 33, "\x00", "\ud800", "kW\udfff"):
            with self.subTest(value=repr(value)):
                self.reject_at_both_boundaries(self.changed(lambda b: b["samples"][0].update(unit=value)))

    def test_numbers_are_checked_before_underflow_discards_the_original_token(self):
        for token in (b'1e-100000', b'0e-100000', b'0e1000000', b'1e309', b'9' * 400):
            with self.subTest(token=token[:30]):
                self.reject_at_both_boundaries(self.payload.replace(b'5.8', token))

    def test_wire_timestamps_require_rfc3339_seconds_and_utc(self):
        for value in ("2026-09-23 08:00:00Z", "2026-09-23T08:00Z", "2026-09-23T08:00:00.1234567Z",
                      "2026-W39-3T08:00:00Z", "2026-09-23T08:00:00+08:00", "2026-09-23T08:00:00Z\n"):
            for field in ("created_at", "measured_at"):
                with self.subTest(value=value, field=field):
                    self.reject_at_both_boundaries(self.changed(
                        lambda b: (b if field == "created_at" else b["samples"][0]).update({field: value})))

    def test_invalid_encoding_and_deeply_nested_json_are_contract_errors(self):
        for payload in (b'\xff', b'[' * 2000 + b'0' + b']' * 2000):
            with self.subTest(length=len(payload)):
                self.reject_at_both_boundaries(payload)

    def test_valid_boundary_values_preserve_exact_payload_and_integer_sequence(self):
        for value in ("立方米/日", "\U0001f4a7", None):
            def valid(body):
                body["sequence"] = {"first": 9223372036854775807, "last": 9223372036854775807}
                for row in body["samples"]:
                    row["sequence"] = 9223372036854775807
                    row["unit"] = value
            payload = self.changed(valid)
            self.assertTrue(self.preflight(TOPIC, payload)["valid"])
            result = self.service.ingest(TOPIC, payload)
            self.assertEqual(result.sample_count, 3)
            batch, metadata = self.repo.calls[-1]
            self.assertEqual(batch.samples[0].sequence, 9223372036854775807)
            self.assertEqual(metadata["raw_payload"], payload)


class OutgoingContractValidationTests(unittest.TestCase):
    def test_invalid_units_cannot_be_emitted_from_local_samples_or_point_maps(self):
        for unit in ("\x00", "\ud800", "", "x" * 33, {}):
            with self.subTest(unit=repr(unit)):
                with self.assertRaises(ContractValidationError):
                    TelemetrySample("GW-1", "D-1", "p", NOW, 1, 1.0, unit=unit)
                with self.assertRaises(ContractValidationError):
                    PointDefinition("p", 1, 4, 0, "float32", unit=unit)

    def test_outbound_sequence_has_same_signed_bigint_limit(self):
        with self.assertRaises(ContractValidationError):
            TelemetrySample("GW-1", "D-1", "p", NOW, 9223372036854775808, 1.0)

    def test_json_text_and_typed_memoryview_limits_use_utf8_byte_size(self):
        # 600k non-ASCII chars or a 300k-word view are over 1 MiB despite len().
        from array import array
        for payload in (" " * 1_048_577, "中" * 600_000, memoryview(array("I", [0] * 300_000))):
            with self.subTest(type=type(payload).__name__), self.assertRaisesRegex(ContractValidationError, "exceeds"):
                UplinkBatch.from_json(payload)

    def test_nonfinite_or_overflowing_python_values_have_stable_contract_errors(self):
        for value in (10 ** 400, float("inf"), True):
            with self.subTest(value=repr(value)[:30]), self.assertRaises(ContractValidationError):
                TelemetrySample("GW-1", "D-1", "p", NOW, 1, value)


if __name__ == "__main__":
    unittest.main()
