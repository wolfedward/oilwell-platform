"""Bounded binary framing for the virtual DSP+MCU terminal interface."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import struct


MAGIC = b"ODSP"
PROTOCOL_VERSION = 1
MAX_DEVICE_ID_BYTES = 128
MAX_TIMESTAMP_BYTES = 40
MAX_PAYLOAD_BYTES = 65_535
CHECKSUM_BYTES = 32
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
_HEADER = struct.Struct("!4sBBQBH")
_DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class FrameError(ValueError):
    """A byte stream is not a valid device frame."""


class FrameFormatError(FrameError):
    """The frame layout or a decoded field violates the protocol contract."""


class FrameChecksumError(FrameError):
    """The frame integrity digest does not match its bytes."""


@dataclass(frozen=True)
class DeviceFrame:
    magic: bytes
    protocol_version: int
    device_id: str
    sequence: int
    timestamp: datetime
    payload_length: int
    payload: dict
    checksum: bytes


def _timestamp(value: datetime | None) -> datetime:
    value = datetime.now(SHANGHAI_TIMEZONE) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(hours=8):
        raise FrameFormatError("timestamp must be timezone-aware +08:00")
    return value


def _device_id(value: str) -> bytes:
    if not isinstance(value, str) or not _DEVICE_ID.fullmatch(value):
        raise FrameFormatError("invalid device id")
    encoded = value.encode("ascii")
    if len(encoded) > MAX_DEVICE_ID_BYTES:
        raise FrameFormatError("device id is too long")
    return encoded


def encode_frame(device_id: str, sequence: int, payload: dict, *, timestamp: datetime | None = None) -> bytes:
    """Encode canonical UTF-8 object JSON with a SHA-256 digest over all body bytes."""
    device = _device_id(device_id)
    if not isinstance(sequence, int) or isinstance(sequence, bool) or not 0 <= sequence <= 2**64 - 1:
        raise FrameFormatError("sequence must be an unsigned 64-bit integer")
    if not isinstance(payload, dict):
        raise FrameFormatError("payload must be an object")
    try:
        payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise FrameFormatError("payload must be canonical JSON") from None
    if len(payload_bytes) > MAX_PAYLOAD_BYTES:
        raise FrameFormatError("payload is too large")
    stamp = _timestamp(timestamp).isoformat(timespec="microseconds").encode("ascii")
    if len(stamp) > MAX_TIMESTAMP_BYTES:
        raise FrameFormatError("timestamp is too long")
    body = _HEADER.pack(MAGIC, PROTOCOL_VERSION, len(device), sequence, len(stamp), len(payload_bytes)) + device + stamp + payload_bytes
    return body + hashlib.sha256(body).digest()


def decode_frame(frame: bytes) -> DeviceFrame:
    """Decode one complete bounded frame; never allocate based on untrusted size alone."""
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise FrameFormatError("frame must be bytes")
    data = bytes(frame)
    if len(data) < _HEADER.size + CHECKSUM_BYTES:
        raise FrameFormatError("frame is truncated")
    magic, version, device_length, sequence, stamp_length, payload_length = _HEADER.unpack_from(data)
    if magic != MAGIC:
        raise FrameFormatError("bad frame magic")
    if version != PROTOCOL_VERSION:
        raise FrameFormatError("unsupported protocol version")
    if not 1 <= device_length <= MAX_DEVICE_ID_BYTES or not 1 <= stamp_length <= MAX_TIMESTAMP_BYTES or payload_length > MAX_PAYLOAD_BYTES:
        raise FrameFormatError("invalid field length")
    body_length = _HEADER.size + device_length + stamp_length + payload_length
    if len(data) != body_length + CHECKSUM_BYTES:
        raise FrameFormatError("truncated or trailing frame bytes")
    body, checksum = data[:body_length], data[body_length:]
    if hashlib.sha256(body).digest() != checksum:
        raise FrameChecksumError("frame checksum mismatch")
    offset = _HEADER.size
    try:
        device_id = body[offset:offset + device_length].decode("ascii")
        timestamp = datetime.fromisoformat(body[offset + device_length:offset + device_length + stamp_length].decode("ascii"))
        payload = json.loads(body[offset + device_length + stamp_length:].decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise FrameFormatError("invalid encoded field") from None
    _device_id(device_id)
    _timestamp(timestamp)
    if not isinstance(payload, dict):
        raise FrameFormatError("payload must be a JSON object")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if canonical != body[offset + device_length + stamp_length:]:
        raise FrameFormatError("payload JSON is not canonical")
    return DeviceFrame(magic, version, device_id, sequence, timestamp, payload_length, payload, checksum)
