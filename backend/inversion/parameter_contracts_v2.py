"""Explicit v2 parse; v1 source, provenance coverage and hashes stay untouched."""
from dataclasses import dataclass
from datetime import date
from hashlib import sha256

from . import parameter_contracts as v1
from .contracts import canonical_bytes, strict_json, fields, identity, integer, integer_text, digest, text, utc, fail
from .coverage_contracts import (CoverageContract, CORE_FIELDS, PROVENANCE_FIELDS,
    collect_coverage_missing, parse_coverage, parameter_core_bytes, _constant)
from .policy_v2 import POLICY_BODY, parse_policy_v2
from .series import ChannelPin


@dataclass(frozen=True)
class ParameterSetV2(v1.ParameterSet):
    coverage_contract: CoverageContract | None
    parameter_core_sha256: str


def _provenance_v2(raw):
    fields(raw, PROVENANCE_FIELDS)
    v1._date(raw['source_date'])
    cycles = []
    for item in raw['calibration_cycles']:
        fields(item, v1.CalibrationCycle.__dataclass_fields__)
        start = integer_text(item['start_ns']); end = integer_text(item['end_ns'])
        if start >= end: fail()
        cycles.append(v1.CalibrationCycle(start, end, v1.name(item['physical_cycle_id'])))
    conditions = []
    for item in raw['operating_conditions']:
        fields(item, v1.OperatingCondition.__dataclass_fields__)
        start = utc(item['valid_from']); end = utc(item['valid_to'])
        if start >= end: fail()
        conditions.append(v1.OperatingCondition(v1.name(item['id']), start, end,
            *(v1._range(item[key], allow_equal=True) for key in ('temperature_K', 'supply_voltage_V', 'supply_frequency_Hz')),
            text(item['source_description']), v1._pins(item['attachment_pins'])))
    return v1.Provenance(text(raw['source_organization'], maximum=128), date.fromisoformat(raw['source_date']),
        v1._pins(raw['attachment_pins']), tuple(cycles), tuple(conditions),
        v1._bindings(raw['channel_bindings']), text(raw['notes'], maximum=262144), None)


_GROUPS = dict(v1._GROUP_SHAPES)
_GROUPS['provenance'] = {key: v1._GROUP_SHAPES['provenance'][key] for key in PROVENANCE_FIELDS}
_GROUPS['policy'] = {'body': {key: _constant(value) for key, value in POLICY_BODY.items()}, 'content_sha256': digest}
_TOP = set(CORE_FIELDS) | {'coverage_contract'}
_UNCERTAINTY = {'torque_bound_Nm': '100', 'factor_bound_m': '0.0001', 'speed_bound_rad_s': '0.00001',
                'numerical_budget_Nm': '0.01', 'numerical_factor_budget_m': '0.0000001'}
_DOMAIN = {'power_W': ('-250000', '250000'), 'speed_rad_s': ('0.1', '2'),
           'acceleration_rad_s2': ('-2', '2'), 'temperature_K': ('250', '350'),
           'supply_voltage_V': ('300', '500'), 'supply_frequency_Hz': ('45', '55'),
           'bandwidth_Hz': ('1', '1000'), 'window_ns': ('1', '10000000'), 'load_N': ('0', '100000')}


def parse_parameter_set_v2(raw: bytes, *, draft: bool = False) -> ParameterSetV2:
    if type(draft) is not bool: fail()
    body = strict_json(raw)
    if type(body) is not dict or not set(body) <= _TOP or body.get('schema') != 'inversion.parameter-set/2':
        fail()
    missing = []
    for key in sorted(_TOP):
        if key not in body or body[key] is None and key != 'parent_version':
            missing.append(key)
        elif key in _GROUPS:
            # Stable structural walker only; never the v1 missing_fields, policy
            # parser, _provenance or _coverage entry points.
            v1._collect_missing(body[key], _GROUPS[key], key, missing)
        elif key == 'coverage_contract':
            collect_coverage_missing(body[key], missing=missing)
    if missing and not draft: fail()
    def optional(key, parser):
        return parser(body[key]) if body.get(key) is not None else None
    assembly = optional('assembly_id', identity)
    version = optional('assembly_version', lambda v: integer(v, minimum=1))
    optional('source_kind', _constant('synthetic'))
    source = 'synthetic' if body.get('source_kind') is not None else None
    start = optional('valid_from', utc); end = optional('valid_to', utc)
    if start is not None and end is not None and start >= end: fail()
    parent = optional('parent_version', lambda v: integer(v, minimum=1))
    groups = {}
    for key in _GROUPS:
        item = body.get(key)
        if item is None or any(path == key or path.startswith(key+'.') or path.startswith(key+'[') for path in missing):
            groups[key] = None
            continue
        if key == 'provenance':
            groups[key] = _provenance_v2(item)
        elif key == 'policy':
            encoded = canonical_bytes(item['body'])
            if sha256(encoded).hexdigest() != item['content_sha256']: fail()
            groups[key] = parse_policy_v2(encoded)
        else:
            groups[key] = v1._PARSERS[key](item)
    uncertainty = body.get('uncertainty') or {}
    for key, expected in _UNCERTAINTY.items():
        if uncertainty.get(key) is not None and uncertainty[key] != expected: fail()
    domain = body.get('domain') or {}
    for key, limits in _DOMAIN.items():
        bound = domain.get(key) or {}
        for side, expected in zip(('min', 'max'), limits):
            if bound.get(side) is not None and bound[side] != expected: fail()
    if groups['phase'] is not None and groups['provenance'] is not None:
        angle = next(item for item in groups['provenance'].channel_bindings if item.role == 'crank_angle')
        if groups['phase'].channel_pin != angle.channel_pin: fail()
    coverage = None
    if body.get('coverage_contract') is not None and not any(path.startswith('coverage_contract') for path in missing):
        coverage = parse_coverage(canonical_bytes(body['coverage_contract']))
    core = sha256(parameter_core_bytes(body)).hexdigest()
    if not missing:
        binding = coverage.body['binding']
        if binding['parameter_core_sha256'] != core or binding['policy_sha256'] != groups['policy'].raw_sha256:
            fail()
        for role in ('active_power', 'crank_angle'):
            pin = next(item.channel_pin for item in groups['provenance'].channel_bindings if item.role == role)
            wire = strict_json(canonical_bytes(coverage.body['inputs'][role]['pin']))
            if pin != ChannelPin.from_wire(wire): fail()
    return ParameterSetV2(body['schema'], assembly, version, source, start, end, parent,
        *(groups[key] for key in ('provenance', 'geometry', 'phase', 'bodies', 'drive', 'domain', 'uncertainty', 'policy')),
        raw, sha256(raw).hexdigest(), draft, tuple(missing), coverage, core)
