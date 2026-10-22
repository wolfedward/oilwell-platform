"""Frozen v2 numerical policy, without changing the approved v1 parser."""
from decimal import Decimal
from hashlib import sha256
from types import MappingProxyType

from .contracts import fields, strict_json, fail
from .policy import AlgorithmPolicy

POLICY_BODY = MappingProxyType({
    'schema': 'inversion.algorithm-policy/2', 'algorithm': 'beam_energy_balance/2',
    'model_version': 'beam_energy_balance/2', 'numerical_margin': '0.001',
    'motion_max_time_span_ns': '250000000', 'motion_max_angle_span_rad': '0.5',
    'motion_max_residual_rad': '0.0001', 'motion_max_condition': '10000',
    'max_load_error_N': '1000', 'min_coverage_fraction': '0.8',
    'reference_max_gap_ns': '20000000', 'fill_max_time_span_ns': '20000000',
    'fill_max_angle_span_rad': '0.2', 'relative_error_min_denominator_N': '1',
})


def parse_policy_v2(raw: bytes) -> AlgorithmPolicy:
    body = fields(strict_json(raw), POLICY_BODY)
    if body != POLICY_BODY:
        fail()
    values = [int(body[key]) if key.endswith('_ns') else Decimal(body[key])
              for key in list(AlgorithmPolicy.__dataclass_fields__)[3:-2]]
    return AlgorithmPolicy(body['schema'], body['algorithm'], body['model_version'],
                           *values, raw, sha256(raw).hexdigest())
