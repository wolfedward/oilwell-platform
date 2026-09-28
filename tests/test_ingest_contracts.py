"""A receipt must bind one exact local observation, not equivalent JSON."""

from dataclasses import asdict, replace
import json
import unittest

from backend.ingest_reliability.contracts import CapacityLimits, Receipt
from tests.ingest_vectors import observation, receipt_for


class ReceiptTests(unittest.TestCase):
    def test_receipt_binds_exact_bytes(self):
        obs = observation()
        receipt = receipt_for(obs)
        self.assertTrue(receipt.matches("oilfield/v2/GW/D/ingest-ack", obs))
        self.assertFalse(receipt.matches("oilfield/v2/GW/D/ingest-ack",
                                         replace(obs, payload=obs.payload + b" ")))

    def test_round_trip_preserves_identity_and_persistent_outcome(self):
        receipt = receipt_for(observation())
        parsed = Receipt.from_bytes(receipt.to_bytes())
        self.assertEqual(parsed.message_id, "M-1")
        self.assertEqual(parsed.status, "stored")
        self.assertEqual(parsed.reason_code, "accepted")
        self.assertEqual(parsed.recorded_at, "2026-09-24T00:00:00.000000+00:00")
        self.assertEqual(parsed.topic("oilfield"), "oilfield/v2/GW/D/ingest-ack")

    def test_mismatched_namespace_identity_or_message_does_not_finish_local_record(self):
        obs = observation()
        receipt = receipt_for(obs)
        for topic in ("oilfield/v2/OTHER/D/ingest-ack", "other/v2/GW/D/ingest-ack",
                      "oilfield/v1/GW/D/ingest-ack", "oilfield/v2/GW/D/telemetry",
                      "oilfield/v2/GW/D/ingest-ack/extra", "", None):
            with self.subTest(topic=topic):
                self.assertFalse(receipt.matches(topic, obs))
        for fields in ({"gateway_id": "OTHER"}, {"device_id": "OTHER"}, {"message_id": "M-2"}):
            with self.subTest(fields=fields):
                other = replace(receipt, **fields)
                self.assertFalse(other.matches(other.topic("oilfield"), obs))

    def test_rejects_unknown_missing_duplicate_and_non_string_fields(self):
        body = asdict(receipt_for(observation()))
        invalid = [dict(body, extra="ignored"), {k: v for k, v in body.items() if k != "status"}]
        for value in (True, 1, None, [], {"value": "stored"}):
            invalid.append(dict(body, status=value))
        for value in invalid:
            with self.subTest(body=value), self.assertRaises(ValueError):
                Receipt.from_bytes(json.dumps(value).encode())
        duplicated = json.dumps(body).encode().replace(b'"status":', b'"status":"quarantined","status":')
        with self.assertRaises(ValueError):
            Receipt.from_bytes(duplicated)

    def test_constructor_and_wire_reject_invalid_values_identically(self):
        base = receipt_for(observation())
        cases = (
            {"schema": "OWP-INGEST-ACK/2.0"}, {"receipt_id": "not-a-uuid"},
            {"receipt_id": "00000000000040008000000000000001"},
            {"gateway_id": "GW/OTHER"}, {"device_id": "+"}, {"message_id": ""},
            {"message_id": "M" * 129}, {"message_id": "M\n"},
            {"payload_sha256": "a" * 63}, {"payload_sha256": "A" * 64},
            {"payload_sha256": "g" * 64}, {"status": "duplicate"},
            {"reason_code": "json_invalid"}, {"status": "quarantined"},
            {"status": "quarantined", "reason_code": "arbitrary_exception"},
            {"recorded_at": "2026-09-24T00:00:00+00:00"},
            {"recorded_at": "2026-09-24T00:00:00.000000+08:00"},
            {"recorded_at": "2026-02-30T00:00:00.000000Z"},
            {"recorded_at": "2026-09-24 00:00:00.000000Z"},
            {"recorded_at": "2026-09-24T00:00:00.0000000Z"},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    replace(base, **fields)
                with self.assertRaises(ValueError):
                    Receipt.from_bytes(json.dumps(dict(asdict(base), **fields)).encode())

    def test_only_documented_quarantine_reasons_round_trip(self):
        for reason in ("topic_invalid", "payload_too_large", "utf8_invalid", "json_invalid",
                       "json_duplicate_key", "contract_invalid", "identity_mismatch",
                       "idempotency_conflict", "qos_not_supported", "retained_publish"):
            with self.subTest(reason=reason):
                receipt = receipt_for(observation(), status="quarantined", reason_code=reason)
                self.assertEqual(Receipt.from_bytes(receipt.to_bytes()).reason_code, reason)

    def test_wire_byte_limit_and_strict_encoding(self):
        raw = receipt_for(observation()).to_bytes()
        self.assertEqual(Receipt.from_bytes(raw + b" " * (4096 - len(raw))).status, "stored")
        for payload in (raw + b" " * (4097 - len(raw)), b"\xff", b"[]", b"null",
                        b"\xef\xbb\xbf" + raw, b"[" * 2000 + b"0" + b"]" * 2000):
            with self.subTest(length=len(payload)), self.assertRaises(ValueError):
                Receipt.from_bytes(payload)

    def test_invalid_topic_prefix_is_never_used_as_an_arbitrary_reply_address(self):
        receipt = receipt_for(observation())
        for prefix in ("oilfield/other", "+", "", "x" * 65, None):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                receipt.topic(prefix)

    def test_contract_rejection_can_match_a_quarantined_receipt(self):
        obs = observation()
        body = json.loads(obs.payload)
        body["samples"] = []
        rejected = replace(obs, payload=json.dumps(body).encode())
        receipt = receipt_for(rejected, status="quarantined", reason_code="contract_invalid")
        self.assertTrue(receipt.matches("oilfield/v2/GW/D/ingest-ack", rejected))
        ambiguous = replace(rejected, payload=rejected.payload[:-1])
        self.assertFalse(receipt.matches("oilfield/v2/GW/D/ingest-ack", ambiguous))


class CapacityLimitsTests(unittest.TestCase):
    def test_invalid_or_unrepresentable_budgets_fail_before_database_use(self):
        for total, quarantine in ((0, 1), (-1, 1), (10, 0), (10, -1), (10, 11),
                                  (True, 1), (10, False), (10.0, 1), ("10", 1),
                                  (9223372036854775808, 1)):
            with self.subTest(total=total, quarantine=quarantine), self.assertRaises(ValueError):
                CapacityLimits(total, quarantine)
        limits = CapacityLimits(9223372036854775807, 9223372036854775807)
        self.assertEqual(limits.quarantine_bytes, 9223372036854775807)


if __name__ == "__main__":
    unittest.main()
