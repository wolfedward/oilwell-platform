"""Central MQTT envelope validation and idempotent ingestion tests."""

from datetime import datetime, timezone
import json
import unittest

from backend.center.ingest import CentralIngestService, IngestValidationError
from backend.gateway.contracts import Quality, TelemetrySample, UplinkBatch


NOW = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)


class FakeRepository:
    def __init__(self, stored=True):
        self.stored = stored
        self.calls = []

    def store(self, batch, *, topic, payload_sha256, raw_payload, received_at):
        self.calls.append((batch, topic, payload_sha256, raw_payload, received_at))
        return self.stored


def payload():
    batch = UplinkBatch.create(
        "GW-001", "D-001", "boot-a",
        (TelemetrySample(
            "GW-001", "D-001", "motor.current", NOW, 10, 12.5,
            Quality.GOOD, "A", "modbus-tcp",
        ),),
        message_id="message-001", created_at=NOW,
    )
    return batch.to_json().encode("utf-8")


class CentralIngestTests(unittest.TestCase):
    def test_valid_topic_and_payload_are_stored_with_integrity_digest(self):
        repository = FakeRepository()
        service = CentralIngestService(repository, topic_prefix="oilfield", clock=lambda: NOW)
        result = service.ingest("oilfield/v1/GW-001/D-001/telemetry", payload())
        self.assertEqual((result.status, result.message_id, result.sample_count), ("stored", "message-001", 1))
        call = repository.calls[0]
        self.assertEqual(len(call[2]), 64)
        self.assertEqual(call[3], payload())
        self.assertEqual(call[4], NOW)

    def test_duplicate_message_is_successful_idempotent_processing(self):
        service = CentralIngestService(FakeRepository(stored=False), topic_prefix="oilfield", clock=lambda: NOW)
        result = service.ingest("oilfield/v1/GW-001/D-001/telemetry", payload())
        self.assertEqual(result.status, "duplicate")

    def test_topic_identity_must_exactly_match_signed_envelope_identity(self):
        repository = FakeRepository()
        service = CentralIngestService(repository, topic_prefix="oilfield", clock=lambda: NOW)
        invalid_topics = (
            "oilfield/v1/GW-002/D-001/telemetry",
            "oilfield/v1/GW-001/D-002/telemetry",
            "oilfield/v1/GW-001/D-001/telemetry/extra",
            "other/v1/GW-001/D-001/telemetry",
        )
        for topic in invalid_topics:
            with self.subTest(topic=topic), self.assertRaises(IngestValidationError):
                service.ingest(topic, payload())
        self.assertEqual(repository.calls, [])

    def test_unknown_schema_fields_bad_sequence_and_oversized_payload_fail_closed(self):
        service = CentralIngestService(FakeRepository(), topic_prefix="oilfield", clock=lambda: NOW)
        body = json.loads(payload())
        body["unexpected"] = True
        with self.assertRaises(IngestValidationError):
            service.ingest("oilfield/v1/GW-001/D-001/telemetry", json.dumps(body).encode())
        body = json.loads(payload())
        body["sequence"]["last"] = 999
        with self.assertRaises(IngestValidationError):
            service.ingest("oilfield/v1/GW-001/D-001/telemetry", json.dumps(body).encode())
        with self.assertRaises(IngestValidationError):
            service.ingest("oilfield/v1/GW-001/D-001/telemetry", b"x" * (1_048_576 + 1))


if __name__ == "__main__":
    unittest.main()
