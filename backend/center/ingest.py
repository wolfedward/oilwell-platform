"""MQTT topic/envelope trust boundary for centrally received telemetry."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re

from backend.gateway.contracts import ContractValidationError, UplinkBatch


_SAFE_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
MAX_PAYLOAD_BYTES = 1_048_576


class IngestValidationError(ValueError):
    pass


@dataclass(frozen=True)
class IngestResult:
    status: str
    message_id: str
    gateway_id: str
    device_id: str
    sample_count: int


class CentralIngestService:
    def __init__(self, repository, *, topic_prefix: str = "oilfield", clock=None):
        if not isinstance(topic_prefix, str) or not _SAFE_PREFIX.fullmatch(topic_prefix):
            raise IngestValidationError("topic_prefix is invalid")
        self.repository = repository
        self.topic_prefix = topic_prefix
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def ingest(self, topic: str, payload: bytes) -> IngestResult:
        if not isinstance(topic, str):
            raise IngestValidationError("MQTT topic must be text")
        parts = topic.split("/")
        if len(parts) != 5 or parts[0] != self.topic_prefix or parts[1] != "v1" or parts[4] != "telemetry":
            raise IngestValidationError("MQTT topic does not match the telemetry namespace")
        if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) > MAX_PAYLOAD_BYTES:
            raise IngestValidationError("MQTT payload exceeds the telemetry limit")
        exact = bytes(payload)
        try:
            batch = UplinkBatch.from_json(exact)
        except ContractValidationError as error:
            raise IngestValidationError(str(error)) from error
        if parts[2] != batch.gateway_id or parts[3] != batch.device_id:
            raise IngestValidationError("topic identity does not match envelope identity")
        received_at = self.clock()
        if not isinstance(received_at, datetime) or received_at.tzinfo is None or received_at.utcoffset() is None:
            raise IngestValidationError("central clock must be timezone-aware")
        received_at = received_at.astimezone(timezone.utc)
        stored = self.repository.store(
            batch,
            topic=topic,
            payload_sha256=hashlib.sha256(exact).hexdigest(),
            raw_payload=exact,
            received_at=received_at,
        )
        return IngestResult(
            status="stored" if stored else "duplicate",
            message_id=batch.message_id,
            gateway_id=batch.gateway_id,
            device_id=batch.device_id,
            sample_count=len(batch.samples),
        )
