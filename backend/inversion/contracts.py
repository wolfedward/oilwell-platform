"""Shared immutable request and exact JSON contracts."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
import re
from types import MappingProxyType
from typing import TypeAlias
from uuid import UUID

from backend.waveforms.commands import Actor
from backend.waveforms.contracts import MAX_I64, require_name, require_sha, require_text, require_uuid

from .errors import InversionContractError


JsonValue: TypeAlias = "None | bool | int | str | list[JsonValue] | tuple[JsonValue, ...] | Mapping[str, JsonValue]"
JsonObject: TypeAlias = Mapping[str, JsonValue]
MAX_JSON_BYTES = 262_144
_DECIMAL = re.compile(r"(?:0|-?(?:0\.[0-9]*[1-9]|[1-9][0-9]*(?:\.[0-9]*[1-9])?))\Z")


def fail():
    raise InversionContractError()


def decimal(value, *, nonnegative=False, positive=False):
    if type(value) is not str or len(value) > 64 or not _DECIMAL.fullmatch(value):
        fail()
    result = Decimal(value)
    if not result.is_finite() or (nonnegative and result < 0) or (positive and result <= 0):
        fail()
    return result


def integer(value, *, minimum=0, maximum=MAX_I64):
    if type(value) is not int or not minimum <= value <= maximum:
        fail()
    return value


def integer_text(value, *, minimum=0, maximum=MAX_I64):
    if type(value) is not str or not re.fullmatch(r"(?:0|-?[1-9][0-9]*)", value) or len(value) > 20:
        fail()
    return integer(int(value), minimum=minimum, maximum=maximum)


def fields(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        fail()
    return value


def identity(value):
    try:
        return require_uuid(value)
    except ValueError as error:
        raise InversionContractError() from error


def name(value):
    try:
        return require_name(value)
    except ValueError as error:
        raise InversionContractError() from error


def digest(value):
    try:
        return require_sha(value)
    except ValueError as error:
        raise InversionContractError() from error


def text(value, *, maximum=2048):
    try:
        require_text(value, maximum=maximum)
    except ValueError as error:
        raise InversionContractError() from error
    if not value.strip():
        fail()
    return value


def utc(value):
    from backend.waveforms.contracts import require_utc
    try:
        require_utc(value)
    except ValueError as error:
        raise InversionContractError() from error
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _plain_json(value):
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) in (list, tuple):
        return [_plain_json(item) for item in value]
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value): fail()
        return {key: _plain_json(item) for key, item in value.items()}
    fail()


def canonical_bytes(value: JsonValue) -> bytes:
    try:
        return json.dumps(_plain_json(value), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise InversionContractError() from error


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail()
        result[key] = value
    return result


def strict_json(raw: bytes):
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_JSON_BYTES:
        fail()
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                          parse_float=lambda _: fail(), parse_constant=lambda _: fail())
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise InversionContractError() from error


def freeze(value):
    if type(value) is dict:
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class VersionPrecondition:
    kind: str
    version: int | None = None

    def __post_init__(self):
        if (self.kind == "absent" and self.version is None) or self.kind == "match" and type(self.version) is int and 1 <= self.version <= MAX_I64:
            return
        fail()


@dataclass(frozen=True)
class InversionCommand:
    actor: Actor
    key: UUID
    expected: VersionPrecondition
    reason: str

    def __post_init__(self):
        if type(self.actor) is not Actor or type(self.key) is not UUID or type(self.expected) is not VersionPrecondition:
            fail()
        text(self.reason, maximum=512)


@dataclass(frozen=True)
class AuthorityRef:
    domain: str
    user_id: int
    session_id: int

    def __post_init__(self):
        name(self.domain)
        integer(self.user_id, minimum=1)
        integer(self.session_id, minimum=1)


@dataclass(frozen=True)
class AuthoritySnapshot:
    ref: AuthorityRef
    permissions: frozenset[str]
    checked_at: datetime
    user_version: int

    def __post_init__(self):
        if type(self.ref) is not AuthorityRef or type(self.permissions) is not frozenset or not all(type(p) is str for p in self.permissions):
            fail()
        for permission in self.permissions:
            name(permission)
        if type(self.checked_at) is not datetime or self.checked_at.tzinfo is None:
            fail()
        integer(self.user_version, minimum=1)
