"""Fixed algorithm policy; every numerical tolerance is supplied by evidence."""

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import re

from .contracts import decimal, fields, integer_text, strict_json, fail


@dataclass(frozen=True)
class AlgorithmPolicy:
    schema: str
    algorithm: str
    model_version: str
    numerical_margin: Decimal
    motion_max_time_span_ns: int
    motion_max_angle_span_rad: Decimal
    motion_max_residual_rad: Decimal
    motion_max_condition: Decimal
    max_load_error_N: Decimal
    min_coverage_fraction: Decimal
    reference_max_gap_ns: int
    fill_max_time_span_ns: int
    fill_max_angle_span_rad: Decimal
    relative_error_min_denominator_N: Decimal
    raw: bytes
    raw_sha256: str


_FIELDS = set(AlgorithmPolicy.__dataclass_fields__) - {"raw", "raw_sha256"}


def parse_policy(raw: bytes) -> AlgorithmPolicy:
    body = fields(strict_json(raw), _FIELDS)
    if body["schema"] != "inversion.algorithm-policy/1" or body["algorithm"] != "beam_energy_balance/1":
        fail()
    if type(body["model_version"]) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", body["model_version"]):
        fail()
    values = {key: decimal(body[key], nonnegative=True) for key in (
        "numerical_margin", "motion_max_angle_span_rad", "motion_max_residual_rad",
        "motion_max_condition", "max_load_error_N", "min_coverage_fraction",
        "fill_max_angle_span_rad", "relative_error_min_denominator_N")}
    if values["numerical_margin"] >= 1 or values["min_coverage_fraction"] > 1:
        fail()
    for key in ("motion_max_condition", "max_load_error_N", "relative_error_min_denominator_N"):
        if values[key] <= 0: fail()
    for key in ("motion_max_time_span_ns", "reference_max_gap_ns", "fill_max_time_span_ns"):
        values[key] = integer_text(body[key], minimum=1)
    if values["fill_max_time_span_ns"] > values["reference_max_gap_ns"]:
        fail()
    return AlgorithmPolicy(body["schema"], body["algorithm"], body["model_version"],
                           *(values[key] for key in list(AlgorithmPolicy.__dataclass_fields__)[3:-2]),
                           raw, sha256(raw).hexdigest())
