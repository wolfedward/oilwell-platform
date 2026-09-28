"""Shared types for center and gateway reliability boundaries."""

from dataclasses import asdict, dataclass, fields
from datetime import datetime
import hashlib
import json
import re
from uuid import UUID

from backend.gateway.contracts import UplinkBatch


REJECTION_REASONS = frozenset({
    "topic_invalid", "payload_too_large", "utf8_invalid", "json_invalid",
    "json_duplicate_key", "contract_invalid", "identity_mismatch",
    "idempotency_conflict", "qos_not_supported", "retained_publish",
})
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SAFE_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RECEIPT_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}(?:Z|\+00:00)\Z")
MAX_RECEIPT_BYTES = 4096


def valid_name(value) -> bool:
    return isinstance(value, str) and SAFE_NAME.fullmatch(value) is not None


def valid_prefix(value) -> bool:
    return isinstance(value, str) and SAFE_PREFIX.fullmatch(value) is not None


@dataclass(frozen=True)
class Observation:
    topic: str
    payload: bytes
    qos: int
    retain: bool
    dup: bool
    received_at: datetime


@dataclass(frozen=True)
class Correlation:
    gateway_id: str
    device_id: str
    message_id: str


@dataclass(frozen=True)
class Candidate:
    batch: UplinkBatch | None
    correlation: Correlation | None
    reason_code: str | None
    validator_version: str


@dataclass(frozen=True)
class CapacityLimits:
    total_bytes: int
    quarantine_bytes: int

    def __post_init__(self):
        if (type(self.total_bytes) is not int or type(self.quarantine_bytes) is not int
                or not 1 <= self.quarantine_bytes <= self.total_bytes <= 9_223_372_036_854_775_807):
            raise ValueError("capacity limits must be positive BIGINT values; quarantine <= total")


@dataclass(frozen=True)
class CommittedResult:
    fact_id: UUID
    status: str
    receipt_id: UUID | None
    duplicate: bool


@dataclass(frozen=True)
class Receipt:
    schema: str
    receipt_id: str
    gateway_id: str
    device_id: str
    message_id: str
    payload_sha256: str
    status: str
    reason_code: str
    recorded_at: str

    def __post_init__(self):
        if any(not isinstance(getattr(self, field.name), str) for field in fields(self)):
            raise ValueError("receipt fields must be strings")
        if self.schema != "OWP-INGEST-ACK/1.0":
            raise ValueError("unsupported receipt schema")
        if not _UUID.fullmatch(self.receipt_id):
            raise ValueError("invalid receipt UUID")
        UUID(self.receipt_id)
        if not all(valid_name(value) for value in (self.gateway_id, self.device_id, self.message_id)):
            raise ValueError("invalid receipt identity")
        if not _SHA256.fullmatch(self.payload_sha256):
            raise ValueError("invalid receipt SHA-256")
        if not ((self.status == "stored" and self.reason_code == "accepted") or
                (self.status == "quarantined" and self.reason_code in REJECTION_REASONS)):
            raise ValueError("invalid receipt outcome")
        if not _RECEIPT_UTC.fullmatch(self.recorded_at):
            raise ValueError("receipt time must be UTC with six fractional digits")
        datetime.fromisoformat(self.recorded_at.replace("Z", "+00:00"))

    @classmethod
    def from_bytes(cls, payload: bytes):
        if not isinstance(payload, bytes) or len(payload) > MAX_RECEIPT_BYTES:
            raise ValueError("receipt must be at most 4096 UTF-8 bytes")

        def unique_object(pairs):
            body = {}
            for key, value in pairs:
                if key in body:
                    raise ValueError("duplicate receipt field")
                body[key] = value
            return body

        try:
            body = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object)
        except (ValueError, RecursionError) as error:
            raise ValueError("receipt must be strict UTF-8 JSON") from error
        if not isinstance(body, dict) or set(body) != {field.name for field in fields(cls)}:
            raise ValueError("receipt fields do not match schema")
        return cls(**body)

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def topic(self, prefix: str) -> str:
        if not valid_prefix(prefix):
            raise ValueError("invalid receipt topic prefix")
        return f"{prefix}/v2/{self.gateway_id}/{self.device_id}/ingest-ack"

    def matches(self, topic: str, observation: Observation) -> bool:
        # Import here avoids a cycle: classification produces the shared types.
        from .wire import classify
        if not isinstance(topic, str):
            return False
        prefix = topic.split("/")[0]
        if not valid_prefix(prefix) or topic != self.topic(prefix):
            return False
        identity = classify(observation, prefix=prefix).correlation
        return (identity is not None
                and (self.gateway_id, self.device_id, self.message_id)
                == (identity.gateway_id, identity.device_id, identity.message_id)
                and self.payload_sha256 == hashlib.sha256(observation.payload).hexdigest())
