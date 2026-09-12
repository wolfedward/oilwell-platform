"""Versioned, transport-neutral contracts for field telemetry."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import math
import re
import uuid


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TYPE_WIDTHS = {
    "uint16": 1,
    "int16": 1,
    "uint32": 2,
    "int32": 2,
    "float32": 2,
    "float64": 4,
}


class ContractValidationError(ValueError):
    """Raised before an invalid map or sample can cross a trust boundary."""


class Quality(str, Enum):
    GOOD = "good"
    UNCERTAIN = "uncertain"
    BAD = "bad"


def _safe_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ContractValidationError(f"{label} contains unsupported characters")
    return value


def _utc_time(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractValidationError(f"{label} must be timezone-aware UTC")
    if value.utcoffset().total_seconds() != 0:
        raise ContractValidationError(f"{label} must use UTC")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class PointDefinition:
    """One read-only engineering point backed by Modbus input/holding registers."""

    point_code: str
    unit_id: int
    function_code: int
    address: int
    data_type: str
    byte_order: str = "big"
    word_order: str = "big"
    scale: float = 1.0
    offset: float = 0.0
    unit: str | None = None
    engineering_min: float | None = None
    engineering_max: float | None = None
    writable: bool = False
    poll_group: str = "normal"

    def __post_init__(self):
        _safe_name(self.point_code, "point_code")
        _safe_name(self.poll_group, "poll_group")
        if not isinstance(self.unit_id, int) or isinstance(self.unit_id, bool) or not 1 <= self.unit_id <= 247:
            raise ContractValidationError("unit_id must be between 1 and 247 for a read request")
        if self.function_code not in (3, 4):
            raise ContractValidationError("telemetry points support Modbus functions 03 and 04 only")
        if self.data_type not in _TYPE_WIDTHS:
            raise ContractValidationError(f"unsupported data_type: {self.data_type}")
        if not isinstance(self.address, int) or isinstance(self.address, bool) or self.address < 0:
            raise ContractValidationError("address must be a zero-based non-negative integer")
        if self.address + self.register_width > 65536:
            raise ContractValidationError("point extends beyond the Modbus address space")
        if self.byte_order not in ("big", "little") or self.word_order not in ("big", "little"):
            raise ContractValidationError("byte_order and word_order must be big or little")
        for label, value in (
            ("scale", self.scale), ("offset", self.offset),
            ("engineering_min", self.engineering_min),
            ("engineering_max", self.engineering_max),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ContractValidationError(f"{label} must be finite")
        if (
            self.engineering_min is not None
            and self.engineering_max is not None
            and self.engineering_min > self.engineering_max
        ):
            raise ContractValidationError("engineering_min cannot exceed engineering_max")
        if self.writable:
            raise ContractValidationError("telemetry maps are read-only; use a separately approved control contract")

    @property
    def register_width(self) -> int:
        return _TYPE_WIDTHS[self.data_type]


@dataclass(frozen=True)
class TelemetrySample:
    gateway_id: str
    device_id: str
    point_code: str
    measured_at: datetime
    sequence: int
    value: float
    quality: Quality = Quality.GOOD
    unit: str | None = None
    source: str = "modbus"

    def __post_init__(self):
        _safe_name(self.gateway_id, "gateway_id")
        _safe_name(self.device_id, "device_id")
        _safe_name(self.point_code, "point_code")
        _safe_name(self.source, "source")
        object.__setattr__(self, "measured_at", _utc_time(self.measured_at, "measured_at"))
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 0:
            raise ContractValidationError("sequence must be a non-negative integer")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(self.value):
            raise ContractValidationError("value must be finite")
        object.__setattr__(self, "value", float(self.value))
        try:
            object.__setattr__(self, "quality", Quality(self.quality))
        except ValueError as error:
            raise ContractValidationError("quality is invalid") from error

    def as_dict(self) -> dict:
        return {
            "point": self.point_code,
            "measured_at": self.measured_at.isoformat().replace("+00:00", "Z"),
            "sequence": self.sequence,
            "value": self.value,
            "quality": self.quality.value,
            "unit": self.unit,
            "source": self.source,
        }


@dataclass(frozen=True)
class UplinkBatch:
    gateway_id: str
    device_id: str
    boot_id: str
    samples: tuple[TelemetrySample, ...]
    message_id: str
    created_at: datetime
    schema: str = "oilwell.telemetry.v1"

    def __post_init__(self):
        for label, value in (
            ("gateway_id", self.gateway_id), ("device_id", self.device_id),
            ("boot_id", self.boot_id), ("message_id", self.message_id),
        ):
            _safe_name(value, label)
        object.__setattr__(self, "created_at", _utc_time(self.created_at, "created_at"))
        if not self.samples:
            raise ContractValidationError("an uplink batch requires at least one sample")
        previous = -1
        for sample in self.samples:
            if sample.gateway_id != self.gateway_id or sample.device_id != self.device_id:
                raise ContractValidationError("all samples must belong to the batch gateway and device")
            if sample.sequence < previous:
                raise ContractValidationError("sample sequences must be monotonic")
            previous = sample.sequence

    @classmethod
    def create(
        cls,
        gateway_id: str,
        device_id: str,
        boot_id: str,
        samples,
        *,
        message_id: str | None = None,
        created_at: datetime | None = None,
    ):
        return cls(
            gateway_id=gateway_id,
            device_id=device_id,
            boot_id=boot_id,
            samples=tuple(samples),
            message_id=message_id or str(uuid.uuid4()),
            created_at=created_at or datetime.now(timezone.utc),
        )

    def topic(self, prefix: str) -> str:
        _safe_name(prefix, "topic prefix")
        return f"{prefix}/v1/{self.gateway_id}/{self.device_id}/telemetry"

    def as_dict(self) -> dict:
        return {
            "schema": self.schema,
            "message_id": self.message_id,
            "gateway_id": self.gateway_id,
            "device_id": self.device_id,
            "boot_id": self.boot_id,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "sequence": {
                "first": self.samples[0].sequence,
                "last": self.samples[-1].sequence,
            },
            "samples": [sample.as_dict() for sample in self.samples],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
