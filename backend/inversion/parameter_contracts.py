"""Immutable SI parameter packages with exact evidence and no inferred values.

Wire choices: ``evidence_refs`` are reviewed content pins in every physical
group; ``policy`` contains its fixed body and canonical digest. Provenance
records operating conditions and four role bindings as explicit lists.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import re
from uuid import UUID

from .contracts import (canonical_bytes, decimal, digest, fields, identity, integer,
                        integer_text, name, strict_json, text, utc, fail)
from .policy import AlgorithmPolicy, parse_policy
from .series import ChannelPin, EvidencePin


def _pins(raw, *, minimum=1):
    if type(raw) is not list or not minimum <= len(raw) <= 64: fail()
    pins = tuple(EvidencePin.from_wire(item) for item in raw)
    if len({pin.id for pin in pins}) != len(pins): fail()
    return pins


def _sign(value):
    if type(value) is not int or value not in (-1, 1): fail()
    return value


def _source_kind(value):
    if type(value) is not str or value not in {"synthetic", "bench_measurement", "field_measurement", "unknown"}:
        fail()
    return value


def _range(raw, *, integer_ns=False, allow_equal=False):
    fields(raw, {"min", "max"})
    lo = integer_text(raw["min"]) if integer_ns else decimal(raw["min"])
    hi = integer_text(raw["max"]) if integer_ns else decimal(raw["max"])
    if lo > hi or lo == hi and not allow_equal: fail()
    return Bound(lo, hi)


@dataclass(frozen=True)
class Bound:
    min: Decimal | int
    max: Decimal | int


@dataclass(frozen=True)
class Geometry:
    pivot_x_m: Decimal
    pivot_y_m: Decimal
    crank_radius_m: Decimal
    pitman_length_m: Decimal
    rear_arm_m: Decimal
    horsehead_radius_m: Decimal
    branch: int
    rotation_sign: int
    displacement_sign: int
    evidence_refs: tuple[EvidencePin, ...]


def _geometry(raw):
    fields(raw, Geometry.__dataclass_fields__)
    return Geometry(decimal(raw["pivot_x_m"]), decimal(raw["pivot_y_m"]),
                    *(decimal(raw[key], positive=True) for key in (
                        "crank_radius_m", "pitman_length_m", "rear_arm_m", "horsehead_radius_m")),
                    _sign(raw["branch"]), _sign(raw["rotation_sign"]),
                    _sign(raw["displacement_sign"]), _pins(raw["evidence_refs"]))


@dataclass(frozen=True)
class Phase:
    channel_pin: ChannelPin
    zero_reading_rad: Decimal
    encoder_direction: int
    unwrap_kind: str
    evidence_refs: tuple[EvidencePin, ...]


def _phase(raw):
    fields(raw, Phase.__dataclass_fields__)
    if type(raw["unwrap_kind"]) is not str or raw["unwrap_kind"] not in {"wrapped_2pi", "unwrapped"}: fail()
    return Phase(ChannelPin.from_wire(raw["channel_pin"]), decimal(raw["zero_reading_rad"]),
                 _sign(raw["encoder_direction"]), raw["unwrap_kind"], _pins(raw["evidence_refs"]))


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    owner: str


def _ledger(raw):
    if type(raw) is not list or not 1 <= len(raw) <= 256: fail()
    result = []
    for item in raw:
        fields(item, Component.__dataclass_fields__)
        if type(item["owner"]) is not str or item["owner"] not in {"upstream", "crank", "beam", "pitman"}: fail()
        result.append(Component(name(item["id"]), text(item["name"], maximum=128), item["owner"]))
    if len({component.id for component in result}) != len(result): fail()
    return tuple(result)


@dataclass(frozen=True)
class Bodies:
    crank_inertia_kg_m2: Decimal
    crank_first_moment_x_kg_m: Decimal
    crank_first_moment_y_kg_m: Decimal
    beam_inertia_kg_m2: Decimal
    beam_first_moment_x_kg_m: Decimal
    beam_first_moment_y_kg_m: Decimal
    pitman_mass_kg: Decimal
    pitman_com_fraction: Decimal
    pitman_inertia_kg_m2: Decimal
    gravity_m_s2: Decimal
    component_ledger: tuple[Component, ...]
    evidence_refs: tuple[EvidencePin, ...]


def _bodies(raw):
    fields(raw, Bodies.__dataclass_fields__)
    values = []
    for key in list(Bodies.__dataclass_fields__)[:10]:
        values.append(decimal(raw[key], nonnegative=key in {
            "crank_inertia_kg_m2", "beam_inertia_kg_m2", "pitman_mass_kg", "pitman_inertia_kg_m2"},
            positive=key == "gravity_m_s2"))
    if not 0 <= values[7] <= 1: fail()
    return Bodies(*values, _ledger(raw["component_ledger"]), _pins(raw["evidence_refs"]))


@dataclass(frozen=True)
class LossGrid:
    power_W: tuple[Decimal, ...]
    motor_speed_rad_s: tuple[Decimal, ...]
    loss_W: tuple[tuple[Decimal, ...], ...]
    error_W: tuple[tuple[Decimal, ...], ...]
    evidence_refs: tuple[EvidencePin, ...]


def _axis(raw):
    if type(raw) is not list or not 2 <= len(raw) <= 64: fail()
    values = tuple(decimal(item) for item in raw)
    if any(left >= right for left, right in zip(values, values[1:])): fail()
    return values


def _loss_grid(raw):
    fields(raw, LossGrid.__dataclass_fields__)
    power = _axis(raw["power_W"]); speed = _axis(raw["motor_speed_rad_s"])
    matrices = []
    for key in ("loss_W", "error_W"):
        matrix = raw[key]
        if type(matrix) is not list or len(matrix) != len(power): fail()
        rows = []
        for row in matrix:
            if type(row) is not list or len(row) != len(speed): fail()
            rows.append(tuple(decimal(item, nonnegative=True) for item in row))
        matrices.append(tuple(rows))
    return LossGrid(power, speed, *matrices, _pins(raw["evidence_refs"]))


@dataclass(frozen=True)
class Drive:
    supply_kind: str
    ratio: Decimal
    upstream_inertia_kg_m2: Decimal
    loss_grid: LossGrid
    crank_friction_Nm: Decimal
    viscous_friction_Nm_s: Decimal
    evidence_refs: tuple[EvidencePin, ...]


def _drive(raw):
    fields(raw, Drive.__dataclass_fields__)
    if raw["supply_kind"] != "direct_line_induction": fail()
    return Drive(raw["supply_kind"], decimal(raw["ratio"], positive=True),
                 decimal(raw["upstream_inertia_kg_m2"], nonnegative=True),
                 _loss_grid(raw["loss_grid"]), decimal(raw["crank_friction_Nm"], nonnegative=True),
                 decimal(raw["viscous_friction_Nm_s"], nonnegative=True), _pins(raw["evidence_refs"]))


@dataclass(frozen=True)
class Domain:
    power_W: Bound
    speed_rad_s: Bound
    acceleration_rad_s2: Bound
    temperature_K: Bound
    supply_voltage_V: Bound
    supply_frequency_Hz: Bound
    bandwidth_Hz: Bound
    window_ns: Bound
    evidence_refs: tuple[EvidencePin, ...]


def _domain(raw):
    fields(raw, Domain.__dataclass_fields__)
    return Domain(*(_range(raw[key], integer_ns=key == "window_ns") for key in list(Domain.__dataclass_fields__)[:-1]),
                  _pins(raw["evidence_refs"]))


@dataclass(frozen=True)
class Uncertainty:
    torque_bound_Nm: Decimal
    factor_bound_m: Decimal
    speed_bound_rad_s: Decimal
    numerical_budget_Nm: Decimal
    numerical_factor_budget_m: Decimal
    evidence_refs: tuple[EvidencePin, ...]


def _uncertainty(raw):
    fields(raw, Uncertainty.__dataclass_fields__)
    return Uncertainty(*(decimal(raw[key], nonnegative=True) for key in list(Uncertainty.__dataclass_fields__)[:-1]),
                       _pins(raw["evidence_refs"]))


@dataclass(frozen=True)
class CalibrationCycle:
    start_ns: int
    end_ns: int
    physical_cycle_id: str


@dataclass(frozen=True)
class OperatingCondition:
    id: str
    valid_from: datetime
    valid_to: datetime
    temperature_K: Bound
    supply_voltage_V: Bound
    supply_frequency_Hz: Bound
    source_description: str
    attachment_pins: tuple[EvidencePin, ...]


@dataclass(frozen=True)
class ModbusBinding:
    device_id: str
    map_version: int
    map_sha256: str
    point_id: str


@dataclass(frozen=True)
class ChannelBinding:
    role: str
    source: str
    channel_pin: ChannelPin
    installation_evidence_refs: tuple[EvidencePin, ...]
    modbus: ModbusBinding | None


@dataclass(frozen=True)
class Provenance:
    source_organization: str
    source_date: date
    attachment_pins: tuple[EvidencePin, ...]
    calibration_cycles: tuple[CalibrationCycle, ...]
    operating_conditions: tuple[OperatingCondition, ...]
    channel_bindings: tuple[ChannelBinding, ...]
    notes: str


def _bindings(bindings):
    if type(bindings) is not list or len(bindings) != 4: fail()
    parsed_bindings = []
    for item in bindings:
        fields(item, ChannelBinding.__dataclass_fields__)
        if type(item["role"]) is not str or type(item["source"]) is not str or item["role"] not in {"active_power", "crank_angle", "load", "displacement"} or item["source"] not in {"native_waveform", "modbus"}:
            fail()
        modbus = item["modbus"]
        if item["source"] == "modbus":
            fields(modbus, ModbusBinding.__dataclass_fields__)
            modbus = ModbusBinding(name(modbus["device_id"]), integer(modbus["map_version"], minimum=1),
                                   digest(modbus["map_sha256"]), name(modbus["point_id"]))
        elif modbus is not None: fail()
        parsed_bindings.append(ChannelBinding(item["role"], item["source"],
            ChannelPin.from_wire(item["channel_pin"]), _pins(item["installation_evidence_refs"]), modbus))
    if {item.role for item in parsed_bindings} != {"active_power", "crank_angle", "load", "displacement"}: fail()
    return tuple(parsed_bindings)


def _provenance(raw):
    fields(raw, Provenance.__dataclass_fields__)
    organization = text(raw["source_organization"], maximum=128)
    source_date = raw["source_date"]
    if type(source_date) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date): fail()
    try:
        parsed_date = date.fromisoformat(source_date)
    except ValueError as error:
        raise ValueError("contract_invalid") from error
    notes = text(raw["notes"], maximum=262144)
    cycles = raw["calibration_cycles"]
    if type(cycles) is not list or not 1 <= len(cycles) <= 64: fail()
    parsed_cycles = []
    for item in cycles:
        fields(item, CalibrationCycle.__dataclass_fields__)
        start = integer_text(item["start_ns"]); end = integer_text(item["end_ns"])
        if start >= end: fail()
        parsed_cycles.append(CalibrationCycle(start, end, name(item["physical_cycle_id"])))
    conditions = raw["operating_conditions"]
    if type(conditions) is not list or not 1 <= len(conditions) <= 64: fail()
    parsed_conditions = []
    for item in conditions:
        fields(item, OperatingCondition.__dataclass_fields__)
        start = utc(item["valid_from"]); end = utc(item["valid_to"])
        if start >= end: fail()
        parsed_conditions.append(OperatingCondition(name(item["id"]), start, end,
            _range(item["temperature_K"], allow_equal=True), _range(item["supply_voltage_V"], allow_equal=True),
            _range(item["supply_frequency_Hz"], allow_equal=True), text(item["source_description"]),
            _pins(item["attachment_pins"])))
    if len({item.id for item in parsed_conditions}) != len(parsed_conditions): fail()
    return Provenance(organization, parsed_date, _pins(raw["attachment_pins"]),
                      tuple(parsed_cycles), tuple(parsed_conditions), _bindings(raw["channel_bindings"]), notes)


@dataclass(frozen=True)
class ParameterSet:
    schema: str
    assembly_id: UUID | None
    assembly_version: int | None
    source_kind: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    parent_version: int | None
    provenance: Provenance | None
    geometry: Geometry | None
    phase: Phase | None
    bodies: Bodies | None
    drive: Drive | None
    domain: Domain | None
    uncertainty: Uncertainty | None
    policy: AlgorithmPolicy | None
    raw: bytes
    raw_sha256: str
    is_draft: bool
    missing: tuple[str, ...]


_GROUP_FIELDS = {
    "provenance": set(Provenance.__dataclass_fields__),
    "geometry": set(Geometry.__dataclass_fields__),
    "phase": set(Phase.__dataclass_fields__),
    "bodies": set(Bodies.__dataclass_fields__),
    "drive": set(Drive.__dataclass_fields__),
    "domain": set(Domain.__dataclass_fields__),
    "uncertainty": set(Uncertainty.__dataclass_fields__),
    "policy": {"body", "content_sha256"},
}
_TOP_FIELDS = set(ParameterSet.__dataclass_fields__) - {"raw", "raw_sha256", "is_draft", "missing"}
_PARSERS = {"provenance": _provenance, "geometry": _geometry, "phase": _phase,
            "bodies": _bodies, "drive": _drive, "domain": _domain, "uncertainty": _uncertainty}


def _missing(body):
    paths = []
    for key in sorted(_TOP_FIELDS):
        if key not in body or body[key] is None and key != "parent_version":
            paths.append(key)
        elif key in _GROUP_FIELDS and type(body[key]) is dict:
            paths.extend(f"{key}.{field}" for field in sorted(_GROUP_FIELDS[key])
                         if field not in body[key] or body[key][field] is None)
    return tuple(paths)


def missing_fields(parameter: ParameterSet) -> tuple[str, ...]:
    if type(parameter) is not ParameterSet: fail()
    return parameter.missing


def _partial_group(key, item):
    """Validate supplied draft values even when sibling fields are blank."""
    if key == "geometry":
        for field in set(item) & {"pivot_x_m", "pivot_y_m"}:
            if item[field] is not None: decimal(item[field])
        for field in set(item) & {"crank_radius_m", "pitman_length_m", "rear_arm_m", "horsehead_radius_m"}:
            if item[field] is not None: decimal(item[field], positive=True)
        for field in set(item) & {"branch", "rotation_sign", "displacement_sign"}:
            if item[field] is not None: _sign(item[field])
    elif key == "bodies":
        for field in set(item) & set(Bodies.__dataclass_fields__) - {"component_ledger", "evidence_refs"}:
            if item[field] is not None:
                value = decimal(item[field], nonnegative=field in {
                    "crank_inertia_kg_m2", "beam_inertia_kg_m2", "pitman_mass_kg", "pitman_inertia_kg_m2"},
                    positive=field == "gravity_m_s2")
                if field == "pitman_com_fraction" and not 0 <= value <= 1: fail()
        if "component_ledger" in item and item["component_ledger"] is not None:
            _ledger(item["component_ledger"])
    elif key == "uncertainty":
        for field in set(item) - {"evidence_refs"}:
            if item[field] is not None: decimal(item[field], nonnegative=True)
    elif key == "domain":
        for field in set(item) - {"evidence_refs"}:
            if item[field] is not None: _range(item[field], integer_ns=field == "window_ns")
    elif key == "drive":
        if "supply_kind" in item and item["supply_kind"] is not None and item["supply_kind"] != "direct_line_induction": fail()
        if "ratio" in item and item["ratio"] is not None: decimal(item["ratio"], positive=True)
        if "loss_grid" in item and item["loss_grid"] is not None: _loss_grid(item["loss_grid"])
        for field in ("upstream_inertia_kg_m2", "crank_friction_Nm", "viscous_friction_Nm_s"):
            if field in item and item[field] is not None: decimal(item[field], nonnegative=True)
    elif key == "phase":
        if "zero_reading_rad" in item and item["zero_reading_rad"] is not None: decimal(item["zero_reading_rad"])
        if "channel_pin" in item and item["channel_pin"] is not None: ChannelPin.from_wire(item["channel_pin"])
        if "encoder_direction" in item and item["encoder_direction"] is not None: _sign(item["encoder_direction"])
        if "unwrap_kind" in item and item["unwrap_kind"] is not None:
            if type(item["unwrap_kind"]) is not str or item["unwrap_kind"] not in {"wrapped_2pi", "unwrapped"}: fail()
    elif key == "provenance":
        if "source_organization" in item and item["source_organization"] is not None:
            text(item["source_organization"], maximum=128)
        if "source_date" in item and item["source_date"] is not None:
            value = item["source_date"]
            if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value): fail()
            try: date.fromisoformat(value)
            except ValueError: fail()
        if "notes" in item and item["notes"] is not None: text(item["notes"], maximum=262144)
        if "attachment_pins" in item and item["attachment_pins"] is not None: _pins(item["attachment_pins"])
        if "calibration_cycles" in item and item["calibration_cycles"] is not None:
            rows = item["calibration_cycles"]
            if type(rows) is not list or not 1 <= len(rows) <= 64: fail()
            for row in rows:
                fields(row, CalibrationCycle.__dataclass_fields__)
                start = integer_text(row["start_ns"]); end = integer_text(row["end_ns"])
                if start >= end: fail()
                name(row["physical_cycle_id"])
        if "operating_conditions" in item and item["operating_conditions"] is not None:
            rows = item["operating_conditions"]
            if type(rows) is not list or not 1 <= len(rows) <= 64: fail()
            for row in rows:
                fields(row, OperatingCondition.__dataclass_fields__)
                name(row["id"]); start = utc(row["valid_from"]); end = utc(row["valid_to"])
                if start >= end: fail()
                for bound in ("temperature_K", "supply_voltage_V", "supply_frequency_Hz"):
                    _range(row[bound], allow_equal=True)
                text(row["source_description"]); _pins(row["attachment_pins"])
        if "channel_bindings" in item and item["channel_bindings"] is not None:
            _bindings(item["channel_bindings"])
    elif key == "policy":
        if "content_sha256" in item and item["content_sha256"] is not None:
            digest(item["content_sha256"])
        if "body" in item and item["body"] is not None:
            parse_policy(canonical_bytes(item["body"]))
    if "evidence_refs" in item and item["evidence_refs"] is not None:
        _pins(item["evidence_refs"])


def parse_parameter_set(raw: bytes, *, draft: bool = False) -> ParameterSet:
    if type(draft) is not bool: fail()
    body = strict_json(raw)
    if type(body) is not dict or not set(body) <= _TOP_FIELDS: fail()
    missing = _missing(body)
    if missing and not draft: fail()
    if body.get("schema") != "inversion.parameter-set/1": fail()
    assembly = identity(body["assembly_id"]) if body.get("assembly_id") is not None else None
    version = integer(body["assembly_version"], minimum=1) if body.get("assembly_version") is not None else None
    source_kind = _source_kind(body["source_kind"]) if body.get("source_kind") is not None else None
    valid_from = utc(body["valid_from"]) if body.get("valid_from") is not None else None
    valid_to = utc(body["valid_to"]) if body.get("valid_to") is not None else None
    if valid_from is not None and valid_to is not None and valid_from >= valid_to: fail()
    parent = body.get("parent_version")
    if parent is not None: integer(parent, minimum=1, maximum=(version-1 if version is not None else 9_223_372_036_854_775_807))
    elif version is not None and version != 1: fail()
    groups = {}
    for key in _GROUP_FIELDS:
        item = body.get(key)
        if item is None:
            groups[key] = None; continue
        if type(item) is not dict or not set(item) <= _GROUP_FIELDS[key]: fail()
        if any(field not in item or item[field] is None for field in _GROUP_FIELDS[key]):
            _partial_group(key, item)
            groups[key] = None; continue
        if key == "policy":
            fields(item, {"body", "content_sha256"})
            digest(item["content_sha256"])
            encoded = canonical_bytes(item["body"])
            if sha256(encoded).hexdigest() != item["content_sha256"]: fail()
            groups[key] = parse_policy(encoded)
        else:
            groups[key] = _PARSERS[key](item)
    if groups["phase"] is not None and groups["provenance"] is not None:
        angle = next(item for item in groups["provenance"].channel_bindings if item.role == "crank_angle")
        if groups["phase"].channel_pin != angle.channel_pin: fail()
    return ParameterSet(body["schema"], assembly, version, source_kind, valid_from, valid_to,
                        parent, *(groups[key] for key in ("provenance", "geometry", "phase", "bodies",
                            "drive", "domain", "uncertainty", "policy")), raw, sha256(raw).hexdigest(),
                        draft, missing)
