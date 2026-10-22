"""Independent rational error propagation for the *selected* seven samples.

Samples here are already mapped to unwrapped physical phase radians using the
same phase mapping as nominal QR (never raw encoder readings or analytical
motion truth). The installed source checker must supply a derivative-support
interval covering the entire window expanded by both timing uncertainties.
"""
from dataclasses import dataclass
from fractions import Fraction as Q
from functools import lru_cache
import math

from .contracts import integer
from .interval_runtime import check_fraction as checked
from .motion import MotionPoint
from .series import Sample
from .transactions import InversionError


@dataclass(frozen=True)
class MotionBounds:
    power_index: int
    time_ns: Q
    angle_indices: tuple[int, ...]
    status: str
    reason_codes: tuple[str, ...]
    exact_center: tuple[Q, Q, Q] | None
    base: tuple[Q, Q, Q] | None
    physical: tuple[Q, Q, Q] | None
    numerical: tuple[Q, Q, Q] | None


def _sum(values):
    result = Q(0)
    for value in values:
        result = checked(result + value)
    return result


def _product(a, b):
    return checked(a * b)


def _power(a, n):
    result = Q(1)
    for _ in range(n): result = _product(result, a)
    return result


@lru_cache(maxsize=128)
def _weights(offsets):
    v = tuple(tuple(_power(x, k) for k in range(4)) for x in offsets)
    # Four equations, seven RHS columns; exact Gauss-Jordan, not the nominal fit.
    matrix = [[_sum(_product(row[i], row[j]) for row in v) for j in range(4)]
              + [row[i] for row in v] for i in range(4)]
    for col in range(4):
        pivot = next((i for i in range(col, 4) if matrix[i][col]), None)
        if pivot is None: raise InversionError('proof_singular_window')
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        factor = matrix[col][col]
        matrix[col] = [checked(x / factor) for x in matrix[col]]
        for row in range(4):
            if row == col: continue
            factor = matrix[row][col]
            matrix[row] = [checked(a - _product(factor, b)) for a, b in zip(matrix[row], matrix[col])]
    result = tuple(tuple(_product(Q(math.factorial(order)), x) for x in matrix[order][4:])
                   for order in range(3))
    for order in range(3):
        for degree in range(4):
            actual = _sum(_product(w, _power(x, degree)) for w, x in zip(result[order], offsets))
            if actual != (math.factorial(order) if order == degree else 0):
                raise InversionError('proof_reproduction_failed')
    return result


def exact_weights(offsets_s: tuple[Q, ...]):
    if type(offsets_s) is not tuple or len(offsets_s) != 7:
        raise ValueError('Seven exact offsets required')
    for value in offsets_s: checked(value)
    if any(left >= right for left, right in zip(offsets_s, offsets_s[1:])):
        raise ValueError('Strictly increasing exact offsets required')
    return _weights(offsets_s)


def weights_cache_info():
    return _weights.cache_info()


def _nonnegative_tuple(values, count):
    if type(values) is not tuple or len(values) != count:
        raise ValueError('Explicit complete error bounds required')
    for value in values:
        if checked(value) < 0: raise ValueError('Negative absolute bound')


def bound_window(*, power_index: int, center_ns: Q, samples: tuple[Sample, ...],
                 nominal: MotionPoint, angle_error_rad: tuple[Q, ...],
                 differential_time_s: tuple[Q, ...], common_time_s: Q,
                 derivative_bounds: tuple[Q, Q, Q, Q],
                 derivative_support_ns: tuple[Q, Q] | None = None) -> MotionBounds:
    integer(power_index); checked(center_ns)
    if type(samples) is not tuple or any(type(s) is not Sample for s in samples):
        raise ValueError('Exact mapped native samples required')
    if type(nominal) is not MotionPoint:
        raise ValueError('Nominal motion point required')
    indices = tuple(s.index for s in samples)
    def refused(reason, exact=None, base=None):
        return MotionBounds(power_index, center_ns, indices, 'refused', (reason,), exact, base, None, None)
    if nominal.power_index != power_index or nominal.time_ns != center_ns or nominal.angle_indices != indices:
        return refused('motion_window_mismatch')
    if nominal.status != 'valid': return refused(nominal.status)
    if len(samples) != 7 or any(s.quality != 0 or s.value_si is None for s in samples):
        return refused('insufficient_angle_support')
    if any(a.index+1 != b.index or a.time_ns >= b.time_ns for a,b in zip(samples,samples[1:])):
        return refused('angle_support_gap')
    if not samples[0].time_ns <= center_ns <= samples[-1].time_ns:
        return refused('insufficient_angle_support')
    if samples[-1].time_ns - samples[0].time_ns > 250000000:
        return refused('motion_time_span_exceeded')
    if samples[-1].value_si - samples[0].value_si > Q(1,2):
        return refused('motion_angle_span_exceeded')
    if any(a.value_si > b.value_si for a,b in zip(samples,samples[1:])):
        return refused('invalid_angle_motion')
    numbers = (nominal.theta_rad, nominal.omega_rad_s, nominal.alpha_rad_s2, nominal.residual_rad, nominal.condition)
    if any(type(n) is not float or not math.isfinite(n) for n in numbers):
        return refused('numerical_failure')
    if nominal.condition < 0 or nominal.condition > 10000:
        return refused('motion_condition_exceeded')
    residual = checked(Q.from_float(nominal.residual_rad))
    if not 0 <= residual <= Q(1,10000): return refused('motion_residual_exceeded')
    _nonnegative_tuple(angle_error_rad, 7); _nonnegative_tuple(differential_time_s, 7)
    _nonnegative_tuple(derivative_bounds, 4)
    if checked(common_time_s) < 0: raise ValueError('Negative common timing bound')
    if derivative_support_ns is None: return refused('motion_derivative_support_missing')
    if type(derivative_support_ns) is not tuple or len(derivative_support_ns) != 2:
        raise ValueError('Explicit derivative support interval required')
    lower, upper = (checked(v) for v in derivative_support_ns)
    if lower >= upper: raise ValueError('Empty derivative support')
    for sample, differential in zip(samples, differential_time_s):
        expansion = _product(checked(common_time_s + differential), Q(10**9))
        if checked(sample.time_ns-expansion) < lower or checked(sample.time_ns+expansion) > upper:
            return refused('motion_derivative_support_missing')
    omega, acceleration, third, fourth = derivative_bounds
    if omega <= 0: return refused('motion_derivative_basis_invalid')
    offsets = tuple(checked((s.time_ns-center_ns)/10**9) for s in samples)
    weights = exact_weights(offsets)
    values = tuple(checked(s.value_si) for s in samples)
    exact = tuple(_sum(_product(w, v) for w,v in zip(row,values)) for row in weights)
    errors = tuple(checked(error + _product(omega, differential))
                   for error,differential in zip(angle_error_rad,differential_time_s))
    base = tuple(checked(_sum(_product(abs(w),error) for w,error in zip(row,errors))
                 + _product(checked(fourth/24), _sum(_product(abs(w),_power(abs(x),4)) for w,x in zip(row,offsets))))
                 for row in weights)
    if base[1] > Q(1,100000): return refused('motion_base_speed_budget_exceeded',exact,base)
    physical_base = (base[0], Q(1,100000), base[2])
    physical = tuple(_sum((b, _product(residual,_sum(map(abs,row))), _product(derivative,common_time_s)))
                     for b,row,derivative in zip(physical_base,weights,(omega,acceleration,third)))
    numerical = tuple(checked(abs(Q.from_float(n)-v)) for n,v in zip(numbers[:3],exact))
    return MotionBounds(power_index,center_ns,indices,'valid',(),exact,base,physical,numerical)
