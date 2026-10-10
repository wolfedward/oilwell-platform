"""Separate, display-only deadpoint interpolation; never reads reference data."""
from dataclasses import dataclass
from fractions import Fraction
import math

from .policy import AlgorithmPolicy
from .solver import PredictionSeries


@dataclass(frozen=True)
class FilledPoint:
    time_ns: Fraction
    power_index: int
    s_m: float
    load_N: float
    original_reason_codes: tuple[str, ...]
    source_indices: tuple[int, int]
    weights: tuple[Fraction, Fraction]
    source_times_ns: tuple[Fraction, Fraction]
    periodic_assumption: bool
    method: str = 'display_only_time_linear'


@dataclass(frozen=True)
class FilledSeries:
    prediction: PredictionSeries
    fills: tuple[FilledPoint, ...]


def _number(value):
    return value is not None and math.isfinite(value)


def fill_deadpoints(prediction: PredictionSeries, policy: AlgorithmPolicy) -> FilledSeries:
    """Fill only a single observed J sign transition in a bounded bracket.

    factor_uncertain/load_error_exceeded alone do not identify a deadpoint.
    Require finite, increasing theta, reliable positive omega and one J sign
    transition (zero samples allowed at that transition). No other refusal
    qualifies. Wrapped brackets explicitly assume period/2pi repetition and
    require the solver's full_cycle_consistent evidence. Original points and
    prediction identity are never overwritten or relabelled as valid.
    """
    if type(prediction) is not PredictionSeries or type(policy) is not AlgorithmPolicy:
        raise ValueError('invalid_fill_contract')
    points = prediction.points
    valid = [i for i, p in enumerate(points) if p.status == 'valid'
             and _number(p.load_N) and _number(p.s_m)]
    filled = []

    def bracket(indices, wrapped=False):
        if len(indices) < 3:
            return
        selected = [points[i] for i in indices]
        if any(not prediction.cycle.start_ns <= p.time_ns <= prediction.cycle.end_ns for p in selected):
            return
        left, right = selected[0], selected[-1]
        middle = selected[1:-1]
        allowed = {'factor_uncertain', 'load_error_exceeded'}
        if any(p.status != 'unobservable' or not p.reason_codes
               or not set(p.reason_codes) <= allowed for p in middle):
            return
        for i, j in zip(indices, indices[1:]):
            if j != 0 and points[j].power_index != points[i].power_index+1:
                return
        period = prediction.cycle.end_ns-prediction.cycle.start_ns
        times, angles, signs = [], [], []
        offset = False
        for i, p in zip(indices, selected):
            if wrapped and i < indices[0]:
                offset = True
            data = p.intermediates
            J, theta, omega = (data.get(k) for k in ('J_m', 'theta_rad', 'omega_rad_s'))
            error = data.get('speed_error_bound_rad_s')
            if not all(_number(v) for v in (J, theta, omega, error)) or error < 0 or omega <= error:
                return
            times.append(p.time_ns+(period if offset else 0))
            angles.append(Fraction(theta)+(Fraction(math.tau) if offset else 0))
            if J:
                signs.append(1 if J > 0 else -1)
        transitions = sum(a != b for a, b in zip(signs, signs[1:]))
        # A zero touching the same sign is not proof of a stroke reversal.
        if transitions != 1:
            return
        if any(a > b for a, b in zip(times, times[1:])) or any(a > b for a, b in zip(angles, angles[1:])):
            return
        span = times[-1]-times[0]
        if not 0 < span <= policy.fill_max_time_span_ns or not 0 < angles[-1]-angles[0] <= Fraction(policy.fill_max_angle_span_rad):
            return
        for p, t in zip(middle, times[1:-1]):
            weight = (t-times[0])/span
            s = float((1-weight)*Fraction(left.s_m)+weight*Fraction(right.s_m))
            load = float((1-weight)*Fraction(left.load_N)+weight*Fraction(right.load_N))
            filled.append(FilledPoint(p.time_ns, p.power_index, s, load, p.reason_codes,
                (left.power_index, right.power_index), (1-weight, weight),
                (times[0], times[-1]), wrapped))

    for left, right in zip(valid, valid[1:]):
        bracket(list(range(left, right+1)))
    if len(valid) >= 2 and prediction.metadata.get('full_cycle', {}).get('reason') == 'full_cycle_consistent':
        # The declared period comes from independent angle support. Only
        # existing edge centers are filled; no raw boundary sample is made.
        bracket(list(range(valid[-1], len(points)))+list(range(0, valid[0]+1)), True)
    return FilledSeries(prediction, tuple(sorted(filled, key=lambda p: p.time_ns)))
