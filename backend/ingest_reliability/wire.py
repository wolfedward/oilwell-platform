"""No-I/O classification of received telemetry bytes."""

from datetime import datetime
import json

from backend.gateway.contracts import ContractValidationError, UplinkBatch, _wire_float
from .contracts import Candidate, Correlation, valid_name, valid_prefix
from .errors import ReliabilityError


MAX_QUARANTINE_BYTES = 2_097_152
MAX_TELEMETRY_BYTES = 1_048_576
VALIDATOR_VERSION = "OWP-INGEST/1.0"


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON token")


def _parse(payload):
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None, "utf8_invalid"
    try:
        body = json.loads(text, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant, parse_float=_wire_float)
    except _DuplicateKey:
        return None, "json_duplicate_key"
    except ContractValidationError:
        return None, "contract_invalid"
    except (ValueError, RecursionError):
        return None, "json_invalid"
    return body, None


def classify(observation, *, prefix: str) -> Candidate:
    if not isinstance(observation.payload, bytes):
        raise ReliabilityError("ingest.observation_invalid", "接收原文必须是精确字节")
    if len(observation.payload) > MAX_QUARANTINE_BYTES:
        raise ReliabilityError("ingest.payload_capacity", "原文超过隔离硬上限，未确认接收")
    instant = observation.received_at
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ReliabilityError("ingest.clock_invalid", "中心时钟必须包含时区")
    if not valid_prefix(prefix):
        raise ReliabilityError("ingest.prefix_invalid", "接收主题配置无效")

    parts = observation.topic.split("/") if isinstance(observation.topic, str) else []
    topic_ok = (len(parts) == 5 and parts[0] == prefix and parts[1] == "v1"
                and parts[4] == "telemetry" and valid_name(parts[2]) and valid_name(parts[3]))
    reason = None
    if len(observation.payload) > MAX_TELEMETRY_BYTES:
        reason = "payload_too_large"
    elif not topic_ok:
        reason = "topic_invalid"
    elif type(observation.qos) is not int or observation.qos != 1:
        reason = "qos_not_supported"
    elif observation.retain:
        reason = "retained_publish"

    body, parse_error = _parse(observation.payload)
    if parse_error:
        return Candidate(None, None, reason or parse_error, VALIDATOR_VERSION)

    correlation = None
    if (topic_ok and isinstance(body, dict) and body.get("schema") == "oilwell.telemetry.v1"
            and all(valid_name(body.get(key)) for key in ("gateway_id", "device_id", "message_id"))
            and body["gateway_id"] == parts[2] and body["device_id"] == parts[3]):
        correlation = Correlation(body["gateway_id"], body["device_id"], body["message_id"])

    # Keep a valid batch even if transport flags were rejected. The repository
    # may have a previously committed acceptance that must never be reversed.
    try:
        batch = UplinkBatch.from_json(observation.payload)
    except ContractValidationError:
        return Candidate(None, correlation, reason or "contract_invalid", VALIDATOR_VERSION)
    if topic_ok and (batch.gateway_id != parts[2] or batch.device_id != parts[3]):
        reason = reason or "identity_mismatch"
    return Candidate(batch, correlation, reason, VALIDATOR_VERSION)
