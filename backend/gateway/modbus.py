"""Deterministic Modbus read planning and engineering-value decoding."""

from dataclasses import dataclass
import math
import struct

from .contracts import PointDefinition, Quality


MAX_READ_REGISTERS = 125
_STRUCT_FORMATS = {
    "uint16": "H",
    "int16": "h",
    "uint32": "I",
    "int32": "i",
    "float32": "f",
    "float64": "d",
}


class DecodeError(ValueError):
    pass


@dataclass(frozen=True)
class ReadBlock:
    unit_id: int
    function_code: int
    start_address: int
    quantity: int
    points: tuple[PointDefinition, ...]


@dataclass(frozen=True)
class DecodedValue:
    point_code: str
    value: float
    raw_value: int | float
    quality: Quality
    unit: str | None


def build_read_blocks(points, *, max_gap: int = 0) -> list[ReadBlock]:
    """Combine compatible point reads without crossing the 125-register limit."""

    if not isinstance(max_gap, int) or isinstance(max_gap, bool) or not 0 <= max_gap <= 124:
        raise ValueError("max_gap must be between 0 and 124")
    ordered = sorted(tuple(points), key=lambda item: (item.unit_id, item.function_code, item.address))
    blocks = []
    current = []
    start = end = None
    key = None

    def flush():
        nonlocal current, start, end, key
        if current:
            blocks.append(ReadBlock(key[0], key[1], start, end - start, tuple(current)))
        current, start, end, key = [], None, None, None

    for point in ordered:
        point_key = (point.unit_id, point.function_code)
        point_end = point.address + point.register_width
        if not current:
            current, start, end, key = [point], point.address, point_end, point_key
            continue
        overlaps = point.address < end
        gap = point.address - end
        combined_end = max(end, point_end)
        compatible = point_key == key and (overlaps or gap <= max_gap) and combined_end - start <= MAX_READ_REGISTERS
        if compatible:
            current.append(point)
            end = combined_end
        else:
            flush()
            current, start, end, key = [point], point.address, point_end, point_key
    flush()
    return blocks


def decode_point(point: PointDefinition, registers) -> DecodedValue:
    """Decode exactly one point from its register slice using the declared byte layout."""

    values = tuple(registers)
    if len(values) != point.register_width:
        raise DecodeError(
            f"{point.point_code} requires {point.register_width} registers, received {len(values)}"
        )
    words = []
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFF:
            raise DecodeError("register values must be unsigned 16-bit integers")
        word = value.to_bytes(2, "big")
        words.append(word if point.byte_order == "big" else word[::-1])
    if point.word_order == "little":
        words.reverse()
    try:
        raw = struct.unpack(">" + _STRUCT_FORMATS[point.data_type], b"".join(words))[0]
    except (KeyError, struct.error) as error:
        raise DecodeError(f"cannot decode {point.point_code}") from error
    engineering = float(raw) * float(point.scale) + float(point.offset)
    if not math.isfinite(engineering):
        raise DecodeError(f"{point.point_code} decoded to a non-finite value")
    quality = Quality.GOOD
    if (
        (point.engineering_min is not None and engineering < point.engineering_min)
        or (point.engineering_max is not None and engineering > point.engineering_max)
    ):
        quality = Quality.UNCERTAIN
    return DecodedValue(point.point_code, engineering, raw, quality, point.unit)
