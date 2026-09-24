"""Stateless input errors must never hide a storage or server-clock failure."""

from dataclasses import replace
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from backend.ingest_reliability.errors import ReliabilityError
from backend.ingest_reliability.wire import classify
from tests.ingest_vectors import observation


class ClassificationTests(unittest.TestCase):
    def test_valid_unregistered_bad_quality_data_stays_eligible_for_raw_storage(self):
        result = classify(observation(gateway="UNREGISTERED", quality="bad"), prefix="oilfield")
        self.assertIsNone(result.reason_code)
        self.assertEqual(result.batch.samples[0].value, 41.0)
        self.assertEqual(result.batch.samples[0].quality.value, "bad")
        self.assertEqual(result.correlation.gateway_id, "UNREGISTERED")

    def test_input_reasons_have_stable_precedence(self):
        obs = observation()
        cases = (
            (replace(obs, payload=b"x" * 1048577, topic="bad"), "payload_too_large"),
            (replace(obs, topic="bad", qos=0), "topic_invalid"),
            (replace(obs, qos=0, retain=True), "qos_not_supported"),
            (replace(obs, retain=True, payload=b"\xff"), "retained_publish"),
            (replace(obs, payload=b"\xff"), "utf8_invalid"),
            (replace(obs, payload=b'{"message_id":"M-1",'), "json_invalid"),
            (replace(obs, payload=b'{"x":1,"x":2}'), "json_duplicate_key"),
            (replace(obs, payload=b"[]"), "contract_invalid"),
            (replace(obs, topic="oilfield/v1/OTHER/D/telemetry"), "identity_mismatch"),
        )
        for received, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(classify(received, prefix="oilfield").reason_code, reason)

    def test_unsafe_json_never_yields_guessed_reply_identity(self):
        obs = observation()
        for payload in (obs.payload[:-1], obs.payload.replace(b'"value":', b'"value":0,"value":'),
                        b"\xff", b"[" * 2000 + b"0" + b"]" * 2000,
                        obs.payload + b" {}"):
            with self.subTest(payload=payload[:40]):
                result = classify(replace(obs, payload=payload), prefix="oilfield")
                self.assertIsNone(result.correlation)
                self.assertIsNone(result.batch)

    def test_safe_contract_rejection_can_be_correlated_without_becoming_a_batch(self):
        obs = observation()
        body = json.loads(obs.payload)
        body["samples"] = []
        result = classify(replace(obs, payload=json.dumps(body).encode()), prefix="oilfield")
        self.assertEqual(result.reason_code, "contract_invalid")
        self.assertEqual((result.correlation.gateway_id, result.correlation.device_id,
                          result.correlation.message_id), ("GW", "D", "M-1"))
        self.assertIsNone(result.batch)

    def test_wrong_schema_or_identifiers_cannot_generate_a_reply(self):
        obs = observation()
        for changes in ({"schema": "wrong"}, {"message_id": True}, {"gateway_id": "GW/#"},
                        {"device_id": "OTHER"}, {"message_id": "M\n"}):
            body = dict(json.loads(obs.payload), **changes)
            with self.subTest(changes=changes):
                self.assertIsNone(classify(replace(obs, payload=json.dumps(body).encode()),
                                           prefix="oilfield").correlation)

    def test_transport_rejection_retains_parseable_batch_for_legacy_fact_lookup(self):
        obs = observation()
        for changes in ({"qos": 0}, {"retain": True}):
            with self.subTest(changes=changes):
                result = classify(replace(obs, **changes), prefix="oilfield")
                self.assertEqual(result.batch.message_id, "M-1")
                self.assertEqual(result.correlation.message_id, "M-1")

    def test_hard_capacity_and_bad_server_time_are_system_errors(self):
        obs = observation()
        for changes in ({"payload": b"x" * 2097153}, {"received_at": datetime(2026, 9, 24)},
                        {"received_at": None}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(ReliabilityError):
                classify(replace(obs, **changes), prefix="oilfield")
        self.assertEqual(classify(replace(obs, payload=b"x" * 2097152),
                                  prefix="oilfield").reason_code, "payload_too_large")

    def test_v1_numeric_and_unicode_validation_is_not_relaxed(self):
        obs = observation()
        for token in (b"1e-100000", b"0e-100000", b"0e1000000", b"1e309", b"9" * 400,
                      b"NaN", b"Infinity", b"true"):
            with self.subTest(token=token[:20]):
                result = classify(replace(obs, payload=obs.payload.replace(b"41.0", token)),
                                  prefix="oilfield")
                self.assertIsNotNone(result.reason_code)
                self.assertIsNone(result.batch)

    def test_programming_error_is_not_quarantined(self):
        obs = observation()
        with patch("backend.ingest_reliability.wire.UplinkBatch.from_json",
                   side_effect=RuntimeError("injected internal parser failure")):
            with self.assertRaisesRegex(RuntimeError, "internal parser failure"):
                classify(obs, prefix="oilfield")

    def test_server_clock_failure_precedes_permanent_input_rejection(self):
        with self.assertRaises(ReliabilityError) as caught:
            classify(replace(observation(), payload=b"bad", received_at=None), prefix="oilfield")
        self.assertEqual(caught.exception.code, "ingest.clock_invalid")

    def test_invalid_config_prefix_is_not_a_device_topic_error(self):
        with self.assertRaises(ReliabilityError):
            classify(observation(), prefix="oilfield/#")

    def test_oversize_but_parseable_payload_only_correlates_with_complete_json(self):
        obs = observation()
        padded = replace(obs, payload=obs.payload + b" " * 1048576)
        result = classify(padded, prefix="oilfield")
        self.assertEqual(result.reason_code, "payload_too_large")
        self.assertEqual(result.correlation.message_id, "M-1")
        self.assertIsNone(result.batch)
        invalid = classify(replace(padded, payload=padded.payload + b"X"), prefix="oilfield")
        self.assertIsNone(invalid.correlation)

    def test_valid_v1_size_boundary_and_empty_payload(self):
        obs = observation()
        padded = replace(obs, payload=obs.payload + b" " * (1048576 - len(obs.payload)))
        self.assertIsNone(classify(padded, prefix="oilfield").reason_code)
        self.assertEqual(classify(replace(obs, payload=b""), prefix="oilfield").reason_code,
                         "json_invalid")


if __name__ == "__main__":
    unittest.main()
