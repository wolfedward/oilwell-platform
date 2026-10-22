"""Bounded, immutable finite-input contracts; syntax is not proof of truth.

The server's installed case checker must independently verify every claimed
bound. Only power/angle may enter PredictionEvidence, never reference arrays.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction as Q
from hashlib import sha256
import re

from .contracts import canonical_bytes, digest, fields, freeze, identity, integer, integer_text, name, strict_json, fail
from .interval_runtime import check_fraction
from .parameter_contracts import AttachmentPin
from .parameter_contracts import _collect_missing, _CHANNEL_SHAPE, _ATTACHMENT_SHAPE
from .series import ChannelPin
from .transactions import InversionError


def exact_number(value):
    if (type(value) is not str or len(value) > 128 or not re.fullmatch(
            r'(?:0|-?(?:0\.[0-9]*[1-9]|[1-9][0-9]*(?:\.[0-9]*[1-9])?))', value)):
        fail()
    return check_fraction(Q(value))


def exact_fraction(value):
    """Canonical rational wire text, with a pre-allocation size guard."""
    if type(value) is not str:
        fail()
    if len(value) > 2469:
        raise InversionError('proof_resource_limit')
    if not re.fullmatch(r'(?:0|-?[1-9][0-9]*)(?:/[1-9][0-9]*)?', value):
        fail()
    result = check_fraction(Q(value))
    if str(result) != value:
        fail()
    return result


def _nonnegative(value):
    if exact_number(value) < 0:
        fail()


def _positive(value):
    if exact_number(value) <= 0:
        fail()


def _constant(expected):
    def validate(value):
        if type(value) is not type(expected) or value != expected:
            fail()
    return validate


_ENCODING = {'unit': name, 'scale': _positive, 'offset': exact_number,
             'rounding': _constant('nearest_even'), 'sample_format': _constant('i32'),
             'pre_encoding_error_bound': _nonnegative}
_INPUT = {'pin': ChannelPin.from_wire, 'original_file_sha256': digest, 'bound_file_sha256': digest,
          'channel_sha256': digest, 'raw_sha256': digest, 'time_sha256': digest, 'quality_sha256': digest,
          'count': lambda v: integer(v, minimum=1, maximum=32768), 'encoding': _ENCODING}
COVERAGE_SHAPE = {
    'schema': _constant('inversion.coverage-contract/1'),
    'scope': _constant('finite_synthetic_input/1'), 'source_kind': _constant('synthetic'),
    'purpose': _constant('software_pair_test'), 'field_accuracy_certified': _constant(False),
    'validation_status': _constant('not_field_validated'),
    'pack_id': name, 'case_id': name, 'config_sha256': digest, 'generator_sha256': digest,
    'inputs': {'active_power': _INPUT, 'crank_angle': _INPUT},
    'cycle': {'start_ns': integer_text, 'end_ns': integer_text, 'physical_cycle_id': name},
    'power_support': {'kind': _constant('centered_mean'), 'window_ns': lambda v: integer_text(v, minimum=1, maximum=10000000)},
    'measurement': {**{key: _nonnegative for key in ('angle_quantization_rad', 'angle_calibration_error_rad',
        'angle_noise_error_rad', 'power_quantization_W', 'power_noise_error_W', 'power_average_mismatch_W', 'loss_error_W')},
        'parameter_semantics': _constant('exact_registered_constants'),
        'unmodeled_terms': _constant('none_in_registered_family'), 'basis': _constant('installed_case/1')},
    'time': {'common_offset_bound_ns': _nonnegative, 'differential_bound_ns': _nonnegative,
             'basis': _constant('installed_case/1')},
    'motion': {'family': _constant('linear_sinusoid/1'), 'theta0_rad': exact_number,
        'rate_rad_s': _positive, 'epsilon_rad': exact_number, 'omega_max_rad_s': _positive,
        'acceleration_max_rad_s2': _nonnegative, 'third_derivative_max_rad_s3': _nonnegative,
        'fourth_derivative_max_rad_s4': _nonnegative, 'support_start_ns': integer_text,
        'support_end_ns': integer_text, 'basis': _constant('installed_case/1'),
        'mechanical_family': _constant('rigid_four_bar_crank_only/1')},
    'binding': {'parameter_core_sha256': digest, 'policy_sha256': digest,
        'checker_version': _constant('finite-input/1'), 'checker_sha256': digest,
        'budget_attachment_pin': AttachmentPin.from_wire},
}


def collect_coverage_missing(value, shape=COVERAGE_SHAPE, path='coverage_contract', missing=None):
    """Validate supplied leaves even for partial drafts, never fill unknowns."""
    missing = [] if missing is None else missing
    if isinstance(shape, dict):
        if type(value) is not dict or not set(value) <= set(shape):
            fail()
        for key, validator in shape.items():
            child = path+'.'+key
            if key not in value or value[key] is None:
                missing.append(child)
            else:
                collect_coverage_missing(value[key], validator, child, missing)
    elif shape == ChannelPin.from_wire or shape == AttachmentPin.from_wire:
        pin_shape = _CHANNEL_SHAPE if shape == ChannelPin.from_wire else _ATTACHMENT_SHAPE
        complete = _collect_missing(value, pin_shape, path, missing)
        if complete: shape(value)
    else:
        shape(value)
    return tuple(missing)


@dataclass(frozen=True)
class CoverageContract:
    body: Mapping
    payload_sha256: str
    raw_sha256: str


def coverage_payload_bytes(body: Mapping) -> bytes:
    # Explicit whitelist; only the human PDF pin is outside its own payload.
    plain = strict_json(canonical_bytes(body))
    fields(plain, COVERAGE_SHAPE)
    fields(plain['binding'], COVERAGE_SHAPE['binding'])
    binding = {key: plain['binding'][key] for key in (
        'parameter_core_sha256', 'policy_sha256', 'checker_version', 'checker_sha256')}
    return canonical_bytes({key: binding if key == 'binding' else plain[key] for key in COVERAGE_SHAPE})


CORE_FIELDS = ('schema', 'assembly_id', 'assembly_version', 'source_kind', 'valid_from', 'valid_to',
               'parent_version', 'provenance', 'geometry', 'phase', 'bodies', 'drive', 'domain', 'uncertainty', 'policy')
PROVENANCE_FIELDS = ('source_organization', 'source_date', 'attachment_pins', 'calibration_cycles',
                     'operating_conditions', 'channel_bindings', 'notes')


def parameter_core_bytes(body: Mapping) -> bytes:
    plain = strict_json(canonical_bytes(body))
    if not set(plain) <= set(CORE_FIELDS) | {'coverage_contract'}:
        fail()
    projected = {key: plain.get(key) for key in CORE_FIELDS}
    provenance = plain.get('provenance')
    if provenance is not None:
        if type(provenance) is not dict or not set(provenance) <= set(PROVENANCE_FIELDS):
            fail()
        projected['provenance'] = {key: provenance.get(key) for key in PROVENANCE_FIELDS}
    coverage = plain.get('coverage_contract') or {}
    budget = (coverage.get('binding') or {}).get('budget_attachment_pin')
    if budget is not None:
        _collect_missing(budget, _ATTACHMENT_SHAPE, 'coverage_contract.binding.budget_attachment_pin', [])
        budget_id = identity(budget['id']) if budget.get('id') is not None else None
        budget_sha = digest(budget['content_sha256']) if budget.get('content_sha256') is not None else None
        # The report cannot also be an excluded dependency of physical evidence.
        def check_pins(node):
            if type(node) is dict:
                if {'id', 'content_sha256', 'purpose', 'source_description'} <= set(node):
                    pin = AttachmentPin.from_wire(node)
                    if pin.id == budget_id or pin.content_sha256 == budget_sha:
                        fail()
                for child in node.values(): check_pins(child)
            elif type(node) is list:
                for child in node: check_pins(child)
        check_pins(projected)
    return canonical_bytes({'projection': 'parameter_core/1', 'body': projected})


def parse_coverage(raw: bytes) -> CoverageContract:
    body = strict_json(raw)
    if collect_coverage_missing(body):
        fail()
    cycle = body['cycle']; motion = body['motion']
    if not (integer_text(motion['support_start_ns']) <= integer_text(cycle['start_ns']) <
            integer_text(cycle['end_ns']) <= integer_text(motion['support_end_ns'])):
        fail()
    for role, unit in (('active_power', 'W'), ('crank_angle', 'rad')):
        item = body['inputs'][role]; pin = ChannelPin.from_wire(item['pin'])
        if item['count'] != pin.last_index - pin.first_index + 1 or item['encoding']['unit'] != unit:
            fail()
    angle = body['inputs']['crank_angle']['encoding']
    if angle['scale'] != '0.00000001' or angle['offset'] != '0':
        fail()
    if body['binding']['budget_attachment_pin']['purpose'] != 'uncertainty':
        fail()
    return CoverageContract(freeze(body), sha256(coverage_payload_bytes(body)).hexdigest(), sha256(raw).hexdigest())


def pin_wire(pin):
    def evidence(value):
        return {'id': str(value.id), 'content_sha256': value.content_sha256, 'review_version': value.review_version}
    return {'gateway_id': pin.gateway_id, 'device_id': pin.device_id, 'capture_id': str(pin.capture_id),
            'stream_id': pin.stream_id, 'channel_id': pin.channel_id, 'manifest_sha256': pin.manifest_sha256,
            'calibration_pin': evidence(pin.calibration_pin), 'sampling_pin': evidence(pin.sampling_pin),
            'first_index': pin.first_index, 'last_index': pin.last_index,
            'sync_pin': evidence(pin.sync_pin) if pin.sync_pin else None,
            'producer_endpoint': {'gateway_id': pin.producer_endpoint.gateway_id, 'device_id': pin.producer_endpoint.device_id}
                if pin.producer_endpoint else None}


@dataclass(frozen=True)
class ChannelEvidence:
    role: str
    raw: tuple[int, ...]
    time_ns: tuple[Q, ...]
    quality: tuple[int, ...]
    scale: Q
    offset: Q
    pin: ChannelPin
    original_file_sha256: str
    bound_file_sha256: str
    channel_sha256: str

    def __post_init__(self):
        if self.role not in ('active_power', 'crank_angle') or type(self.pin) is not ChannelPin:
            fail()
        if any(type(value) is not tuple for value in (self.raw, self.time_ns, self.quality)):
            fail()
        count = len(self.raw)
        if not 1 <= count <= 32768 or len(self.time_ns) != count or len(self.quality) != count:
            fail()
        if count != self.pin.last_index - self.pin.first_index + 1:
            fail()
        for code in self.raw: integer(code, minimum=-(2**31), maximum=2**31-1)
        for quality in self.quality: integer(quality, maximum=4)
        for time in self.time_ns: check_fraction(time)
        if any(left >= right for left, right in zip(self.time_ns, self.time_ns[1:])):
            fail()
        if check_fraction(self.scale) <= 0:
            fail()
        check_fraction(self.offset)
        for value in (self.original_file_sha256, self.bound_file_sha256, self.channel_sha256): digest(value)
        if sha256(channel_evidence_bytes(self)).hexdigest() != self.channel_sha256:
            fail()


def channel_evidence_bytes(value):
    return canonical_bytes({'role': value.role, 'raw': value.raw,
        'time_ns': [str(t) for t in value.time_ns], 'quality': value.quality,
        'scale': str(value.scale), 'offset': str(value.offset), 'pin': pin_wire(value.pin),
        'original_file_sha256': value.original_file_sha256, 'bound_file_sha256': value.bound_file_sha256})


@dataclass(frozen=True)
class PredictionEvidence:
    power: ChannelEvidence
    angle: ChannelEvidence
    pack_id: str
    case_id: str
    config_sha256: str
    generator_sha256: str

    def __post_init__(self):
        if (type(self.power) is not ChannelEvidence or self.power.role != 'active_power' or
                type(self.angle) is not ChannelEvidence or self.angle.role != 'crank_angle'):
            fail()
        name(self.pack_id); name(self.case_id)
        digest(self.config_sha256); digest(self.generator_sha256)
