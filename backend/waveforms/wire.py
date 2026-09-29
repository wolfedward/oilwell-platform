"""Strict, side-effect-free OWP-WAVE/1.0 wire parsing and classification."""

import base64
import binascii
from fractions import Fraction
from hashlib import sha256
import json
from math import gcd

from .contracts import (
    Candidate, CaptureKey, ChannelSpec, ChunkFrame, ManifestFrame, Observation, StreamSpec,
    MAX_CAPTURE_DATA_BYTES, MAX_CAPTURE_RAW_BYTES, MAX_CHUNK_BYTES, MAX_CHUNK_DATA_BYTES,
    MAX_I64, MIN_I64, MAX_MANIFEST_BYTES, SCHEMA, VALIDATOR_VERSION, require_count, require_fields,
    require_i64_string, require_name, require_sha, require_text, require_utc, require_uuid,
    valid_name, valid_prefix,
)
from .errors import ContractError, WaveformError
from .samples import validate_quality_bytes


class _DuplicateKey(ValueError):
    pass


class _NonFinite(ValueError):
    pass


def _unique_object(pairs):
    body = {}
    for key, value in pairs:
        if key in body:
            raise _DuplicateKey
        body[key] = value
    return body


def _reject_constant(_):
    raise _NonFinite


def _depth(value):
    if isinstance(value, dict):
        return 1 + max((_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(item) for item in value), default=0)
    return 0


def strict_json(payload: bytes):
    if not isinstance(payload, bytes):
        raise ContractError("contract_invalid")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("utf8_invalid") from error
    if "\r" in text or "\n" in text:
        raise ContractError("json_invalid")
    try:
        body = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except _DuplicateKey as error:
        raise ContractError("json_duplicate_key") from error
    except _NonFinite as error:
        raise ContractError("contract_invalid") from error
    except (ValueError, RecursionError) as error:
        raise ContractError("json_invalid") from error
    if _depth(body) > 16:
        raise ContractError("contract_invalid")
    return body


_COMMON = {"schema", "kind", "gateway_id", "device_id", "capture_id"}
_MANIFEST = _COMMON | {"boot_id", "capture_kind", "source_kind", "producer", "source_evidence",
                       "utc_start", "utc_uncertainty_ns", "clock_domain_id", "sync_evidence_id",
                       "window_ns", "cycle", "streams"}
_STREAM = {"stream_id", "source", "clock_domain_id", "rate_num", "rate_den",
           "first_sample_index", "offset_ns", "frame_count", "frame_bytes", "chunk_frames",
           "chunk_count", "decoded_bytes", "data_sha256", "channels"}
_CHANNEL = {"channel_id", "quantity", "unit", "encoding", "measurement_source", "signal_kind",
            "config_id", "calibration_id"}
_CHUNK = _COMMON | {"stream_id", "chunk_index", "sample_start", "sample_count",
                    "manifest_sha256", "data_sha256", "data_base64"}
_CYCLE = {"cycle_id", "start_ns", "turn_ns", "end_ns", "direction", "zero_reference",
          "reference_channel", "reference_events"}
_EVENT = {"stream_id", "sample_index", "event"}


def _enum(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ContractError("contract_invalid")
    return value


def _optional_uuid(value):
    return None if value is None else require_uuid(value)


def _channel(body):
    require_fields(body, _CHANNEL)
    require_name(body["channel_id"])
    require_text(body["quantity"])
    require_text(body["unit"], maximum=32)
    _enum(body["encoding"], {"i32le", "f32le"})
    _enum(body["measurement_source"], {"sensor", "derived", "synthetic", "unknown"})
    _enum(body["signal_kind"], {"instantaneous", "rms", "window_mean", "derived"})
    require_name(body["config_id"])
    return ChannelSpec(body["channel_id"], body["quantity"], body["unit"], body["encoding"],
                       body["measurement_source"], body["signal_kind"], body["config_id"],
                       _optional_uuid(body["calibration_id"]))


def _stream(body):
    require_fields(body, _STREAM)
    require_name(body["stream_id"])
    require_fields(body["source"], {"gateway_id", "device_id", "boot_id"})
    for name in ("gateway_id", "device_id"):
        require_name(body["source"][name])
    require_uuid(body["source"]["boot_id"])
    require_name(body["clock_domain_id"])
    numerator = require_count(body["rate_num"], minimum=1)
    denominator = require_count(body["rate_den"], minimum=1, maximum=1_000_000)
    first = require_i64_string(body["first_sample_index"], nonnegative=True)
    offset = require_i64_string(body["offset_ns"])
    frames = require_count(body["frame_count"], minimum=1)
    frame_bytes = require_count(body["frame_bytes"], minimum=1)
    chunk_frames = require_count(body["chunk_frames"], minimum=1)
    chunk_count = require_count(body["chunk_count"], minimum=1)
    decoded_bytes = require_count(body["decoded_bytes"], minimum=1)
    require_sha(body["data_sha256"])
    channel_rows = body["channels"]
    if not isinstance(channel_rows, list):
        raise ContractError("contract_invalid")
    channels = tuple(_channel(row) for row in channel_rows)
    if not channels or len(channels) > 32 or len({channel.channel_id for channel in channels}) != len(channels):
        raise ContractError("layout_invalid")
    if (gcd(numerator, denominator) != 1 or numerator < denominator or numerator > 128_000 * denominator
            or frame_bytes != 5 * len(channels) or decoded_bytes != frames * frame_bytes
            or chunk_count != (frames + chunk_frames - 1) // chunk_frames
            or chunk_frames * frame_bytes > MAX_CHUNK_DATA_BYTES
            or first + frames - 1 > MAX_I64):
        raise ContractError("layout_invalid")
    last_ns = Fraction(offset) + Fraction((frames - 1) * 1_000_000_000 * denominator, numerator)
    if not MIN_I64 <= last_ns <= MAX_I64:
        raise ContractError("layout_invalid")
    return StreamSpec(body["stream_id"], body["source"], body["clock_domain_id"], numerator,
                      denominator, first, offset, frames, frame_bytes, chunk_frames, chunk_count,
                      decoded_bytes, body["data_sha256"], channels)


def _cycle(body, *, capture_kind, window_ns, streams):
    if body is None:
        if capture_kind == "cycle":
            raise ContractError("contract_invalid")
        return
    require_fields(body, _CYCLE)
    require_uuid(body["cycle_id"])
    start = require_i64_string(body["start_ns"])
    turn = require_i64_string(body["turn_ns"])
    end = require_i64_string(body["end_ns"])
    _enum(body["direction"], {"clockwise", "counterclockwise", "unknown"})
    require_text(body["zero_reference"])
    require_name(body["reference_channel"])
    events = body["reference_events"]
    if not isinstance(events, list) or len(events) != 3:
        raise ContractError("contract_invalid")
    for event, label in zip(events, ("start", "turn", "end")):
        require_fields(event, _EVENT)
        _enum(event["event"], {label})
        require_name(event["stream_id"])
        if event["sample_index"] is not None:
            require_i64_string(event["sample_index"], nonnegative=True)
    if not start < turn < end:
        raise ContractError("layout_invalid")
    references = {stream.stream_id: stream for stream in streams}
    if not any(channel.channel_id == body["reference_channel"] for stream in streams for channel in stream.channels):
        raise ContractError("layout_invalid")
    for event in events:
        stream = references.get(event["stream_id"])
        if stream is None or not any(channel.channel_id == body["reference_channel"] for channel in stream.channels):
            raise ContractError("layout_invalid")
        if event["sample_index"] is not None:
            index = int(event["sample_index"])
            if not stream.first_sample_index <= index < stream.first_sample_index + stream.frame_count:
                raise ContractError("layout_invalid")


def _manifest(body, key, raw):
    require_fields(body, _MANIFEST)
    require_uuid(body["boot_id"])
    capture_kind = _enum(body["capture_kind"], {"cycle", "event"})
    _enum(body["source_kind"], {"synthetic", "bench_measurement", "field_measurement", "unknown"})
    require_fields(body["producer"], {"manufacturer", "recorder_version"})
    require_text(body["producer"]["manufacturer"])
    require_text(body["producer"]["recorder_version"])
    _optional_uuid(body["source_evidence"])
    if body["utc_start"] is not None:
        require_utc(body["utc_start"])
    if body["utc_uncertainty_ns"] is not None:
        require_i64_string(body["utc_uncertainty_ns"], nonnegative=True)
    require_name(body["clock_domain_id"])
    _optional_uuid(body["sync_evidence_id"])
    window_ns = require_i64_string(body["window_ns"], nonnegative=True)
    if window_ns == 0:
        raise ContractError("contract_invalid")
    rows = body["streams"]
    if not isinstance(rows, list):
        raise ContractError("contract_invalid")
    if not 1 <= len(rows) <= 8:
        raise ContractError("layout_invalid")
    streams = tuple(_stream(row) for row in rows)
    _cycle(body["cycle"], capture_kind=capture_kind, window_ns=window_ns, streams=streams)
    time_points = [Fraction(0)]
    for stream in streams:
        time_points.extend((Fraction(stream.offset_ns),
                            Fraction(stream.offset_ns) + Fraction(
                                (stream.frame_count - 1) * 1_000_000_000 * stream.rate_den,
                                stream.rate_num)))
    if body["cycle"] is not None:
        time_points.extend(Fraction(int(body["cycle"][name])) for name in ("start_ns", "turn_ns", "end_ns"))
    if (window_ns > 60_000_000_000 or len({s.stream_id for s in streams}) != len(streams)
            or max(time_points) - min(time_points) > window_ns
            or sum(len(s.channels) for s in streams) > 32
            or sum(s.decoded_bytes for s in streams) > MAX_CAPTURE_DATA_BYTES
            or sum(s.chunk_count for s in streams) > 512):
        raise ContractError("layout_invalid")
    # The complete message budget includes base64/JSON overhead. Reject manifest
    # declarations that cannot possibly fit the capture's raw-byte ceiling.
    minimum_raw = len(raw) + sum((s.decoded_bytes + 2) // 3 * 4 for s in streams)
    if minimum_raw > MAX_CAPTURE_RAW_BYTES:
        raise ContractError("layout_invalid")
    return ManifestFrame(key, sha256(raw).hexdigest(), body, streams)


def _chunk(body, key):
    require_fields(body, _CHUNK)
    require_name(body["stream_id"])
    index = require_count(body["chunk_index"])
    start = require_count(body["sample_start"])
    count = require_count(body["sample_count"], minimum=1)
    require_sha(body["manifest_sha256"])
    require_sha(body["data_sha256"])
    encoded = body["data_base64"]
    if not isinstance(encoded, str) or not encoded.isascii():
        raise ContractError("contract_invalid")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ContractError("contract_invalid") from error
    if base64.b64encode(data).decode("ascii") != encoded:
        raise ContractError("contract_invalid")
    if len(data) > MAX_CHUNK_DATA_BYTES:
        raise ContractError("payload_too_large")
    if not data or len(data) % (count * 5) or start + count > 2_147_483_647:
        raise ContractError("layout_invalid")
    validate_quality_bytes(data)
    if sha256(data).hexdigest() != body["data_sha256"]:
        raise ContractError("digest_mismatch")
    return ChunkFrame(key, body["manifest_sha256"], body["stream_id"], index, start, count,
                      body["data_sha256"], data)


def parse_frame(payload: bytes):
    if not isinstance(payload, bytes):
        raise ContractError("contract_invalid")
    if len(payload) > MAX_CHUNK_BYTES:
        raise ContractError("payload_too_large")
    body = strict_json(payload)
    if not isinstance(body, dict) or body.get("schema") != SCHEMA or body.get("kind") not in ("manifest", "chunk"):
        raise ContractError("contract_invalid")
    if body["kind"] == "manifest" and len(payload) > MAX_MANIFEST_BYTES:
        raise ContractError("payload_too_large")
    key = CaptureKey(require_name(body.get("gateway_id")), require_name(body.get("device_id")),
                     require_uuid(body.get("capture_id")))
    return _manifest(body, key, payload) if body["kind"] == "manifest" else _chunk(body, key)


def classify(observation: Observation, *, prefix: str):
    if type(observation) is not Observation or not valid_prefix(prefix):
        raise WaveformError("observation_invalid", "Invalid observation or configured topic prefix")
    if len(observation.payload) > MAX_CHUNK_BYTES:
        raise WaveformError("payload_capacity", "Message exceeds the quarantine hard limit")
    parts = observation.topic.split("/") if isinstance(observation.topic, str) else []
    topic_ok = (len(parts) == 5 and parts[0] == prefix and parts[1] == "v2"
                and parts[4] == "waveform" and valid_name(parts[2]) and valid_name(parts[3]))
    reason = None
    if not topic_ok or observation.transport != "mqtt":
        reason = "topic_invalid"
    elif type(observation.qos) is not int or observation.qos != 1:
        reason = "qos_not_supported"
    elif observation.retain is not False:
        reason = "retained_publish"
    try:
        frame = parse_frame(observation.payload)
    except ContractError as error:
        return Candidate(None, None, reason or error.code, VALIDATOR_VERSION)
    if topic_ok and (frame.key.gateway_id != parts[2] or frame.key.device_id != parts[3]):
        return Candidate(frame, None, reason or "identity_mismatch", VALIDATOR_VERSION)
    return Candidate(frame, frame.key if topic_ok else None, reason, VALIDATOR_VERSION)
