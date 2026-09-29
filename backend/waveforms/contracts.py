"""Validated immutable identities, wire metadata and application receipts."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from uuid import UUID

from .errors import ContractError


SCHEMA = "OWP-WAVE/1.0"
ACK_SCHEMA = "OWP-WAVE-ACK/1.0"
MAX_MANIFEST_BYTES = 65_536
MAX_CHUNK_BYTES = 262_144
MAX_CHUNK_DATA_BYTES = 131_072
MAX_CAPTURE_DATA_BYTES = 16_777_216
MAX_CAPTURE_RAW_BYTES = 33_554_432
MAX_RECEIPT_BYTES = 4096
MAX_I64 = 9_223_372_036_854_775_807
MIN_I64 = -9_223_372_036_854_775_808
MAX_I32 = 2_147_483_647
VALIDATOR_VERSION = SCHEMA
PERMANENT_REASONS = frozenset({
    "payload_too_large", "topic_invalid", "qos_not_supported", "retained_publish",
    "utf8_invalid", "json_invalid", "json_duplicate_key", "contract_invalid",
    "layout_invalid", "digest_mismatch", "identity_mismatch", "reference_conflict",
    "idempotency_conflict",
})
INVALID_REASONS = frozenset({"reference_conflict", "idempotency_conflict", "layout_invalid", "digest_mismatch"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z")


def valid_name(value):
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def valid_prefix(value):
    return isinstance(value, str) and _PREFIX.fullmatch(value) is not None


def require_name(value):
    if not valid_name(value):
        raise ContractError("contract_invalid")
    return value


def require_uuid(value):
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ContractError("contract_invalid")
    try:
        return UUID(value)
    except ValueError as error:
        raise ContractError("contract_invalid") from error


def require_sha(value):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ContractError("contract_invalid")
    return value


def require_i64_string(value, *, nonnegative=False):
    if not isinstance(value, str) or not _INTEGER.fullmatch(value):
        raise ContractError("contract_invalid")
    if len(value.lstrip("-")) > 19:
        raise ContractError("contract_invalid")
    number = int(value)
    if number < (0 if nonnegative else MIN_I64) or number > MAX_I64:
        raise ContractError("contract_invalid")
    return number


def require_count(value, *, minimum=0, maximum=MAX_I32):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError("contract_invalid")
    return value


def require_utc(value):
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise ContractError("contract_invalid")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError("contract_invalid") from error
    return value


def require_text(value, *, maximum=128):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ContractError("contract_invalid")
    if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ContractError("contract_invalid")
    return value


def require_fields(body, expected):
    if not isinstance(body, dict) or set(body) != set(expected):
        raise ContractError("contract_invalid")


@dataclass(frozen=True)
class CaptureKey:
    gateway_id: str
    device_id: str
    capture_id: UUID

    def __post_init__(self):
        require_name(self.gateway_id)
        require_name(self.device_id)
        if type(self.capture_id) is not UUID:
            raise ContractError("contract_invalid")


@dataclass(frozen=True)
class Observation:
    transport: str
    topic: str | None
    payload: bytes
    received_at: datetime
    source_ref: UUID
    qos: int | None
    retain: bool | None
    dup: bool | None

    def __post_init__(self):
        if not isinstance(self.payload, bytes) or not isinstance(self.received_at, datetime) or self.received_at.tzinfo is None:
            raise ValueError("observation requires exact bytes and an aware receipt time")
        if type(self.source_ref) is not UUID:
            raise ValueError("source_ref must be UUID")


@dataclass(frozen=True)
class ChannelSpec:
    channel_id: str
    quantity: str
    unit: str
    encoding: str
    measurement_source: str
    signal_kind: str
    config_id: str
    calibration_id: UUID | None


@dataclass(frozen=True)
class StreamSpec:
    stream_id: str
    source: dict
    clock_domain_id: str
    rate_num: int
    rate_den: int
    first_sample_index: int
    offset_ns: int
    frame_count: int
    frame_bytes: int
    chunk_frames: int
    chunk_count: int
    decoded_bytes: int
    data_sha256: str
    channels: tuple[ChannelSpec, ...]


@dataclass(frozen=True)
class ManifestFrame:
    key: CaptureKey
    raw_sha256: str
    body: dict
    streams: tuple[StreamSpec, ...]


@dataclass(frozen=True)
class ChunkFrame:
    key: CaptureKey
    manifest_sha256: str
    stream_id: str
    chunk_index: int
    sample_start: int
    sample_count: int
    data_sha256: str
    data: bytes


@dataclass(frozen=True)
class Candidate:
    frame: ManifestFrame | ChunkFrame | None
    key: CaptureKey | None
    reason_code: str | None
    validator_version: str


@dataclass(frozen=True)
class ReceiptExpectation:
    prefix: str
    key: CaptureKey
    scope: str
    manifest_sha256: str | None
    payload_sha256: str | None
    stream_id: str | None
    chunk_index: int | None

    def __post_init__(self):
        if not valid_prefix(self.prefix) or type(self.key) is not CaptureKey or self.scope not in ("message", "capture"):
            raise ContractError("contract_invalid")
        if self.manifest_sha256 is not None:
            require_sha(self.manifest_sha256)
        if self.payload_sha256 is not None:
            require_sha(self.payload_sha256)
        if (self.stream_id is None) != (self.chunk_index is None):
            raise ContractError("contract_invalid")
        if self.stream_id is not None:
            require_name(self.stream_id)
            require_count(self.chunk_index)
        if self.scope == "capture" and (self.manifest_sha256 is None or self.payload_sha256 is not None or self.stream_id is not None):
            raise ContractError("contract_invalid")
        if self.scope == "message" and self.payload_sha256 is None:
            raise ContractError("contract_invalid")


@dataclass(frozen=True)
class WaveReceipt:
    schema: str
    receipt_id: UUID
    gateway_id: str
    device_id: str
    capture_id: UUID
    scope: str
    manifest_sha256: str | None
    payload_sha256: str | None
    stream_id: str | None
    chunk_index: int | None
    status: str
    reason_code: str
    recorded_at: str

    def __post_init__(self):
        if self.schema != ACK_SCHEMA or type(self.receipt_id) is not UUID or type(self.capture_id) is not UUID:
            raise ContractError("contract_invalid")
        require_name(self.gateway_id)
        require_name(self.device_id)
        if self.scope not in ("message", "capture"):
            raise ContractError("contract_invalid")
        if self.manifest_sha256 is not None:
            require_sha(self.manifest_sha256)
        if self.payload_sha256 is not None:
            require_sha(self.payload_sha256)
        if (self.stream_id is None) != (self.chunk_index is None):
            raise ContractError("contract_invalid")
        if self.stream_id is not None:
            require_name(self.stream_id)
            require_count(self.chunk_index)
        require_utc(self.recorded_at)
        if (self.scope, self.status) not in {("message", "stored"), ("message", "quarantined"),
                                              ("capture", "complete"), ("capture", "invalid")}:
            raise ContractError("contract_invalid")
        if self.scope == "message":
            if self.payload_sha256 is None or (self.status == "stored" and self.manifest_sha256 is None):
                raise ContractError("contract_invalid")
        elif self.manifest_sha256 is None or self.payload_sha256 is not None or self.stream_id is not None:
            raise ContractError("contract_invalid")
        if self.status in ("stored", "complete") and self.reason_code != "accepted":
            raise ContractError("contract_invalid")
        if self.status == "quarantined" and self.reason_code not in PERMANENT_REASONS:
            raise ContractError("contract_invalid")
        if self.status == "invalid" and self.reason_code not in INVALID_REASONS:
            raise ContractError("contract_invalid")

    @classmethod
    def from_bytes(cls, payload: bytes):
        if not isinstance(payload, bytes) or len(payload) > MAX_RECEIPT_BYTES:
            raise ContractError("payload_too_large")
        from .wire import strict_json
        body = strict_json(payload)
        require_fields(body, {"schema", "receipt_id", "gateway_id", "device_id", "capture_id", "scope",
                              "manifest_sha256", "payload_sha256", "stream_id", "chunk_index", "status",
                              "reason_code", "recorded_at"})
        return cls(schema=body["schema"], receipt_id=require_uuid(body["receipt_id"]),
                   gateway_id=body["gateway_id"], device_id=body["device_id"],
                   capture_id=require_uuid(body["capture_id"]), scope=body["scope"],
                   manifest_sha256=body["manifest_sha256"], payload_sha256=body["payload_sha256"],
                   stream_id=body["stream_id"], chunk_index=body["chunk_index"], status=body["status"],
                   reason_code=body["reason_code"], recorded_at=body["recorded_at"])

    def to_bytes(self):
        body = {"schema": self.schema, "receipt_id": str(self.receipt_id), "gateway_id": self.gateway_id,
                "device_id": self.device_id, "capture_id": str(self.capture_id), "scope": self.scope,
                "manifest_sha256": self.manifest_sha256, "payload_sha256": self.payload_sha256,
                "stream_id": self.stream_id, "chunk_index": self.chunk_index, "status": self.status,
                "reason_code": self.reason_code, "recorded_at": self.recorded_at}
        payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_RECEIPT_BYTES:
            raise ContractError("payload_too_large")
        return payload

    def matches(self, topic: str, *, expected: ReceiptExpectation):
        if type(expected) is not ReceiptExpectation:
            return False
        address = f"{expected.prefix}/v2/{expected.key.gateway_id}/{expected.key.device_id}/waveform-ack"
        return (topic == address and (self.gateway_id, self.device_id, self.capture_id)
                == (expected.key.gateway_id, expected.key.device_id, expected.key.capture_id)
                and self.scope == expected.scope
                and self.manifest_sha256 == expected.manifest_sha256
                and self.payload_sha256 == expected.payload_sha256
                and self.stream_id == expected.stream_id and self.chunk_index == expected.chunk_index)
