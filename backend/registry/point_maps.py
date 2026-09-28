"""Read-only point definitions and the registry's versioned canonical bytes."""

from dataclasses import asdict
import hashlib
import json

from backend.gateway.contracts import ContractValidationError, PointDefinition
from .values import TRANSPORTS, choice, fields, finite, identifier, invalid, text


CANONICAL_RULE = "registry.point-map.canonical.v1"
_COMMON = {"point_code", "unit", "engineering_min", "engineering_max"}
_MODBUS = {"unit_id", "function_code", "address", "data_type", "byte_order", "word_order",
           "scale", "offset", "writable", "poll_group"}
_MODBUS_REQUIRED = {"point_code", "unit_id", "function_code", "address", "data_type"}


def normalize_points(points: list[dict], *, modbus: bool | None = None) -> list[dict]:
    """Normalize homogeneous point shape; device transport is checked at storage."""
    if not isinstance(points, list) or not 1 <= len(points) <= 256:
        raise invalid("points", "每版须包含 1—256 个测点")
    for point in points:
        fields(point, _COMMON | _MODBUS, {"point_code"}, "point")
    if modbus is None:
        modbus = any(set(point) & _MODBUS for point in points)
    result = []
    names = set()
    for point in points:
        fields(point, _COMMON | _MODBUS if modbus else _COMMON,
               _MODBUS_REQUIRED if modbus else {"point_code"}, "point")
        code = identifier(point["point_code"], "point_code")
        if code in names:
            raise invalid("point_code", "同版点表内测点代码不能重复")
        names.add(code)
        unit = point.get("unit")
        if unit is not None:
            unit = text(unit, "unit", minimum=1, maximum=32)
        item = {"point_code": code, "unit": unit}
        for label in ("engineering_min", "engineering_max"):
            value = point.get(label)
            item[label] = finite(value, label) if value is not None else None
        if item["engineering_min"] is not None and item["engineering_max"] is not None:
            if item["engineering_min"] > item["engineering_max"]:
                raise invalid("engineering_min", "工程量下界不能大于上界")
        if modbus:
            for label in ("unit_id", "function_code", "address"):
                if type(point[label]) is not int:
                    raise invalid(label, "须为整数，不能使用布尔值或小数")
                item[label] = point[label]
            item["data_type"] = choice(point["data_type"], "data_type",
                                        ("uint16", "int16", "uint32", "int32", "float32", "float64"))
            for label in ("byte_order", "word_order"):
                item[label] = choice(point.get(label, "big"), label, ("big", "little"))
            item["scale"] = finite(point.get("scale", 1.0), "scale")
            item["offset"] = finite(point.get("offset", 0.0), "offset")
            if point.get("writable", False) is not False:
                raise invalid("writable", "采集点表只能只读")
            item["writable"] = False
            item["poll_group"] = identifier(point.get("poll_group", "normal"), "poll_group")
            try:
                item = asdict(PointDefinition(**item))
            except ContractValidationError as error:
                raise invalid("point", "Modbus 参数超出只读寄存器范围") from error
        result.append(item)
    return sorted(result, key=lambda point: point["point_code"])


def canonical_point_map(transport: str, points: list[dict]) -> tuple[dict, bytes, str]:
    choice(transport, "transport", TRANSPORTS)
    document = {"rule": CANONICAL_RULE, "transport": transport,
                "points": normalize_points(points, modbus=transport != "mqtt_direct")}
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return document, raw, hashlib.sha256(raw).hexdigest()
