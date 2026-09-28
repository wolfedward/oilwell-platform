"""Strict JSON and write-command contracts; no authentication or database I/O."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from uuid import UUID

from .errors import RegistryError
from .point_maps import normalize_points
from .values import TRANSPORTS, choice, fields, finite, identifier, invalid, text, uuid_value, version


MAX_JSON_BYTES = 65536
MAX_JSON_DEPTH = 64


@dataclass(frozen=True)
class Actor:
    domain: str
    user_id: str
    username: str
    display_name: str
    role_code: str


@dataclass(frozen=True)
class Command:
    action: str
    target: UUID | None
    actor: Actor
    key: UUID
    request_hash: str
    body: dict[str, object]


@dataclass(frozen=True)
class Change:
    object_type: str
    object_id: UUID
    before: dict[str, object]
    after: dict[str, object]
    entity: dict[str, object]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError("registry.duplicate_key", "JSON 含重复字段")
        result[key] = value
    return result


def _decimal(token):
    value = Decimal(token)
    finite(value, "number")
    return value


def _reject_constant(token):
    raise invalid("number", "不能使用非有限数值")


def _check_depth(source: str) -> None:
    # The C decoder's depth ceiling is not Python's recursion limit. Bound
    # untrusted nesting before parsing, ignoring braces inside JSON strings.
    depth = 0
    quoted = False
    escaped = False
    for char in source:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise RegistryError("registry.json_too_deep", "JSON 嵌套不能超过 64 层")
        elif char in "]}":
            depth -= 1


def parse_json(data: bytes) -> dict:
    if not isinstance(data, bytes):
        raise RegistryError("registry.invalid_json", "请求必须为 UTF-8 JSON 字节")
    if len(data) > MAX_JSON_BYTES:
        raise RegistryError("registry.body_too_large", "请求不能超过 64 KiB")
    try:
        source = data.decode("utf-8", errors="strict")
        _check_depth(source)
        result = json.loads(source, object_pairs_hook=_unique_object,
                            parse_float=_decimal, parse_constant=_reject_constant)
    except RegistryError:
        raise
    except (ValueError, UnicodeError, RecursionError, OverflowError, InvalidOperation) as error:
        raise RegistryError("registry.invalid_json", "请求不是有效的 UTF-8 JSON 对象") from error
    if not isinstance(result, dict):
        raise RegistryError("registry.invalid_json", "请求顶层必须为 JSON 对象")
    return result


def command_digest(method: str, route: str, body: dict) -> str:
    raw = json.dumps({"method": method, "route": route, "body": body}, ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


_ACTION_FIELDS = {
    "well.create": ({"area_code", "well_code", "name"}, {"area_code", "well_code", "name"}),
    "well.change": ({"name", "status"}, set()),
    "device.create": ({"gateway_id", "device_id", "name", "manufacturer", "model", "serial_number", "transport", "usage"},
                      {"gateway_id", "device_id", "name", "transport", "usage"}),
    "device.change": ({"name", "manufacturer", "model", "serial_number", "check_note", "usage"}, set()),
    "map.create": ({"points", "parent_version"}, {"points", "parent_version"}),
    "map.review": ({"map_version", "sha256", "decision"}, {"map_version", "sha256", "decision"}),
    "device.bind": ({"well_id"}, {"well_id"}),
    "device.unbind": (set(), set()),
    "map.assign": ({"map_version"}, {"map_version"}),
    "device.state": ({"status", "check_note"}, {"status"}),
}
_CREATES = {"well.create", "device.create"}


def validate_command(action: str, target: UUID | None, actor: Actor, body: dict, *, method: str, route: str) -> Command:
    if not isinstance(action, str) or action not in _ACTION_FIELDS:
        raise RegistryError("registry.invalid_action", "不支持的管理操作")
    if action in _CREATES:
        if target is not None:
            raise invalid("target", "创建时不能指定已有实体")
    elif not isinstance(target, UUID):
        raise invalid("target", "变更须指定实体 UUID")
    allowed, required = _ACTION_FIELDS[action]
    common = {"reason", "idempotency_key"}
    if action not in _CREATES:
        common = common | {"expected_version"}
    fields(body, allowed | common, required | common, "body")
    if action in {"well.change", "device.change"} and not (set(body) & allowed):
        raise invalid("body", "至少指定一个可变字段")
    result = {"reason": text(body["reason"], "reason", minimum=2, maximum=500),
              "idempotency_key": str(uuid_value(body["idempotency_key"], "idempotency_key"))}
    if "expected_version" in common:
        version(body["expected_version"])
        result["expected_version"] = body["expected_version"]
    for key in ("area_code", "well_code", "gateway_id", "device_id"):
        if key in body:
            result[key] = identifier(body[key], key)
    for key in ("name", "manufacturer", "model", "serial_number", "check_note"):
        if key in body:
            result[key] = text(body[key], key, minimum=1 if key == "name" else 0,
                               maximum={"serial_number": 128, "check_note": 1000}.get(key, 120))
        elif action == "device.create" and key in {"manufacturer", "model", "serial_number"}:
            result[key] = ""
    if "transport" in body:
        result["transport"] = choice(body["transport"], "transport", TRANSPORTS)
    if "usage" in body:
        result["usage"] = choice(body["usage"], "usage", ("commissioning", "production"))
    if "status" in body:
        result["status"] = choice(body["status"], "status", ("archived",) if action == "well.change"
                                   else ("active", "suspended", "retired"))
    if action == "device.state" and result["status"] == "active":
        result["check_note"] = text(body.get("check_note"), "check_note", minimum=2, maximum=1000)
    for key in ("map_version", "parent_version"):
        if key in body:
            if key != "parent_version" or body[key] is not None:
                version(body[key])
            result[key] = body[key]
    if "well_id" in body:
        result["well_id"] = str(uuid_value(body["well_id"], "well_id"))
    if "decision" in body:
        result["decision"] = choice(body["decision"], "decision", ("approved", "rejected"))
        digest = body["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise invalid("sha256", "须为 64 位小写十六进制摘要")
        result["sha256"] = digest
    if "points" in body:
        result["points"] = normalize_points(body["points"])
    return Command(action, target, actor, UUID(result["idempotency_key"]),
                   command_digest(method, route, result), result)
