"""Public study input contracts and canonical JSON."""
import hashlib
import json
import math
from uuid import UUID

KINDS = frozenset(("condition_training", "condition_prediction", "recovery_fit", "energy_schedule",
                   "energy_effects", "motor_spectrum", "protection_replay"))
SELECTABLE = frozenset(("condition_training", "energy_schedule", "protection_replay"))
MAX_JSON_BYTES = 8 * 1024 * 1024


class StudyError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def canonical(value):
    def walk(item, depth=0):
        if depth > 32:
            raise StudyError("invalid_json", "JSON nesting is too deep")
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise StudyError("invalid_json", "non-finite number")
            return
        if type(item) is list:
            for child in item:
                walk(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                walk(child, depth + 1)
            return
        raise StudyError("invalid_json", "JSON-compatible data required")
    walk(value)
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, RecursionError):
        raise StudyError("invalid_json", "invalid JSON value") from None
    if len(result.encode("utf-8")) > MAX_JSON_BYTES:
        raise StudyError("too_large", "JSON exceeds 8 MiB", 413)
    return result


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def uuid(value, name="request_id"):
    if type(value) is not str:
        raise StudyError("invalid_field", f"{name} must be a UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise StudyError("invalid_field", f"{name} must be a UUID") from None
    if str(parsed) != value.lower():
        raise StudyError("invalid_field", f"{name} must be a UUID")
    return str(parsed)


def fields(value, required, allowed=None):
    if type(value) is not dict:
        raise StudyError("invalid_field", "JSON object required")
    required, allowed = set(required), set(allowed or required)
    if set(value) != required and (not required <= set(value) or not set(value) <= allowed):
        raise StudyError("invalid_field", "missing or unknown field")


def bounded_text(value, name, maximum=240, required=True):
    if type(value) is not str or len(value) > maximum or (required and not value.strip()):
        raise StudyError("invalid_field", f"invalid {name}")
    return value.strip()


def validate_envelope(body):
    fields(body, ("request_id", "kind", "title", "source", "payload"))
    uuid(body["request_id"])
    if type(body["kind"]) is not str or body["kind"] not in KINDS:
        raise StudyError("unsupported_kind", "unsupported study kind")
    bounded_text(body["title"], "title", 160)
    source = body["source"]
    fields(source, ("kind", "description", "references"))
    if source["kind"] not in ("software_fixture", "submitted_observation"):
        raise StudyError("invalid_source", "unsupported source kind")
    bounded_text(source["description"], "source.description", 1000, source["kind"] == "submitted_observation")
    refs = source["references"]
    if type(refs) is not list or len(refs) > 32:
        raise StudyError("invalid_source", "invalid source references")
    for ref in refs:
        bounded_text(ref, "source reference", 512)
    if source["kind"] == "submitted_observation" and not refs:
        raise StudyError("invalid_source", "observation references required")
    if type(body["payload"]) is not dict:
        raise StudyError("invalid_payload", "payload must be an object")
    canonical(body)
    return body


def validate_review(body):
    fields(body, ("request_id", "expected_version", "decision", "comment"))
    uuid(body["request_id"])
    if type(body["expected_version"]) is not int or body["expected_version"] < 1:
        raise StudyError("invalid_field", "expected_version must be positive")
    if body["decision"] not in ("approved", "rejected", "revoked"):
        raise StudyError("invalid_field", "invalid decision")
    bounded_text(body["comment"], "comment", 1000)
    canonical(body)
    return body


def validate_selection(body):
    fields(body, ("request_id", "expected_version", "record_id", "comment"))
    uuid(body["request_id"])
    if type(body["expected_version"]) is not int or body["expected_version"] < 0:
        raise StudyError("invalid_field", "invalid expected_version")
    if body["record_id"] is not None:
        uuid(body["record_id"], "record_id")
    bounded_text(body["comment"], "comment", 1000)
    canonical(body)
    return body
