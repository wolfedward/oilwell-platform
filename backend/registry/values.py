"""Small strict value validators shared by commands and point definitions."""

from decimal import Decimal
import math
import re
from uuid import UUID

from .errors import RegistryError


MAX_VERSION = 9_223_372_036_854_775_807
TRANSPORTS = ("modbus_tcp", "modbus_rtu", "mqtt_direct")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def invalid(label: str, message: str = "格式或取值不正确") -> RegistryError:
    return RegistryError("registry.invalid_field", f"{label}：{message}")


def text(value: object, label: str, *, minimum: int = 0, maximum: int = 120) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise invalid(label, f"须为 {minimum}—{maximum} 个字符的文字")
    if minimum and len(value.strip()) < minimum:
        raise invalid(label, "有效文字不足，不能只填空白")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise invalid(label, "不能包含控制字符或不完整的 Unicode 字符")
    return value


def identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise invalid(label, "须为 1—128 位 ASCII 字母、数字及 . _ : -，且以字母或数字开头")
    return value


def choice(value: object, label: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise invalid(label, "不支持的选项")
    return value


def version(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise RegistryError("registry.invalid_version", "版本必须为正整数字符串")
    result = int(value)
    if result > MAX_VERSION:
        raise RegistryError("registry.invalid_version", "版本超出范围")
    return result


def uuid_value(value: object, label: str) -> UUID:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise invalid(label, "须为带连字符的 UUID")
    return UUID(value)


def finite(value: object, label: str) -> float:
    if type(value) not in (int, float, Decimal):
        raise invalid(label, "须为有限数值，不能使用布尔值或数字字符串")
    try:
        result = float(value)
        if not math.isfinite(result) or (result == 0 and value != 0):
            raise invalid(label, "超出可表示范围，不能静默转成零或无穷")
    except (OverflowError, ValueError) as error:
        if isinstance(error, RegistryError):
            raise
        raise invalid(label, "超出可表示范围") from error
    return result


def fields(value: object, allowed: set[str], required: set[str], label: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise invalid(label, "须为 JSON 对象")
    if set(value) - allowed:
        raise invalid(label, "包含不允许的字段")
    if required - set(value):
        raise invalid(label, "缺少必填字段")
    return value
