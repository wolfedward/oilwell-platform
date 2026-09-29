"""Explicit immutable capacity budgets. Importing this module performs no I/O."""
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Mapping
from .contracts import MAX_I64, valid_name


@dataclass(frozen=True)
class Quota:
    raw_bytes: int
    facts: int
    attachment_bytes: int
    incomplete_captures: int
    orphan_bytes: int
    quarantine_bytes: int
    metadata_rows: int
    orphan_bytes_per_capture: int

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 1 <= value <= MAX_I64:
                raise ValueError("quota values must be explicit positive signed 64-bit integers")
        if self.quarantine_bytes > self.raw_bytes or self.orphan_bytes > self.raw_bytes:
            raise ValueError("quarantine and orphan limits must fit raw capacity")
        if self.orphan_bytes_per_capture > self.orphan_bytes:
            raise ValueError("per-capture orphan capacity exceeds its bucket")


@dataclass(frozen=True)
class CapacityLimits:
    global_quota: Quota
    gateways: Mapping[str, Quota]

    def __post_init__(self):
        if type(self.global_quota) is not Quota or not isinstance(self.gateways, Mapping) or not self.gateways:
            raise ValueError("explicit global and gateway capacities are required")
        gateway_copy = dict(self.gateways)
        if any(not valid_name(key) or type(value) is not Quota for key, value in gateway_copy.items()):
            raise ValueError("invalid configured gateway capacity")
        for field in fields(Quota):
            if field.name == "orphan_bytes_per_capture":
                if any(value.orphan_bytes_per_capture > self.global_quota.orphan_bytes_per_capture for value in gateway_copy.values()):
                    raise ValueError("per-capture orphan budget exceeds global maximum")
            elif sum(getattr(value, field.name) for value in gateway_copy.values()) > getattr(self.global_quota, field.name):
                raise ValueError("sum of gateway budgets exceeds global capacity")
        object.__setattr__(self, "gateways", MappingProxyType(gateway_copy))
