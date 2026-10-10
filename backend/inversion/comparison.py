"""Independent, continuous-time comparison on native evaluated power centers.

Errors, signals and J*omega are piecewise linear between eligible centers.
Integrals use exact rational endpoint algebra and relative times. No reference
signal changes prediction validity or identity. Field independence/approval
cannot be established by this numerical kernel and always remains unknown.
"""
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, replace
from fractions import Fraction
import math
from types import MappingProxyType
from typing import Mapping

from .display_fill import fill_deadpoints
from .policy import AlgorithmPolicy
from .series import ChannelPin, ReferenceInputs
from .solver import PredictionSeries


@dataclass(frozen=True)
class Metric:
    value: float | int | Fraction | None
    unit: str
    reason: str | None = None


@dataclass(frozen=True)
class ReferenceValue:
    value_si: Fraction | None
    indices: tuple[int, ...]
    weights: tuple[Fraction, ...]
    source: ChannelPin
    reason: str | None


@dataclass(frozen=True)
class Evaluation:
    time_ns: Fraction
    power_index: int
    load: ReferenceValue
    displacement: ReferenceValue
    valid: bool
    reason_codes: tuple[str, ...]
    reference_edge_from_previous: bool


@dataclass(frozen=True)
class ComparisonReport:
    primary: Mapping[str, Metric]
    by_stroke: Mapping[str, Mapping[str, Metric]]
    coverage: Mapping[str, Metric]
    invalid_reasons: Mapping[str, int]
    work: Mapping[str, Metric]
    independence: Mapping
    evaluations: tuple[Evaluation, ...]
    prediction_status: str
    prediction_reason_codes: tuple[str, ...]
    filled_comparison: 'ComparisonReport | None' = None
    scope: str = 'original_valid_support'


def _metric(value, unit, reason=None):
    if value is None:
        return Metric(None, unit, reason)
    try:
        number = float(value)
        if math.isfinite(number):
            return Metric(number, unit)
    except (OverflowError, ValueError):
        pass
    return Metric(None, unit, 'numerical_overflow')


class _Reference:
    """Prefix gap counts give O(log n) interval checks without filtering gaps."""
    def __init__(self, series, cycle, policy):
        self.series, self.cycle = series, cycle
        self.times = [p.time_ns for p in series.samples]
        self.bad = [0]
        for a, b in zip(series.samples, series.samples[1:]):
            bad = (a.quality != 0 or b.quality != 0 or b.index != a.index+1
                   or b.time_ns-a.time_ns > policy.reference_max_gap_ns)
            self.bad.append(self.bad[-1]+int(bad))

    def at(self, t):
        samples, source = self.series.samples, self.series.source
        def result(value=None, indices=(), weights=(), reason=None):
            return ReferenceValue(value, indices, weights, source, reason)
        if not self.cycle.start_ns <= t <= self.cycle.end_ns:
            return result(reason='outside_cycle')
        i = bisect_left(self.times, t)
        if i < len(samples) and samples[i].time_ns == t:
            p = samples[i]
            return result(p.value_si, (p.index, p.index), (Fraction(1), Fraction(0)),
                          None if p.quality == 0 else 'reference_quality')
        if i == 0 or i == len(samples):
            return result(reason='reference_no_bracket')
        a, b = samples[i-1:i+1]
        if a.time_ns < self.cycle.start_ns or b.time_ns > self.cycle.end_ns:
            return result(reason='reference_bracket_outside_cycle')
        if self.bad[i] != self.bad[i-1]:
            return result(reason='reference_gap')
        w = (t-a.time_ns)/(b.time_ns-a.time_ns)
        return result((1-w)*a.value_si+w*b.value_si, (a.index, b.index), (1-w, w))

    def supports(self, a, b):
        i = bisect_right(self.times, a)-1
        j = bisect_left(self.times, b)
        return 0 <= i <= j < len(self.times) and self.bad[j] == self.bad[i]


def _absolute(a, b):
    if a*b < 0:
        return (a*a+b*b)/(2*(abs(a)+abs(b)))
    return (abs(a)+abs(b))/2


def _square(a, b):
    return (a*a+a*b+b*b)/3


def _product(a, b, c, d):
    return (2*a*c+a*d+b*c+2*b*d)/6


def _metrics(segments, values, policy):
    result = {}
    duration = sum((s[0] for s in segments), Fraction(0))
    result['duration'] = _metric(duration, 's')
    result['sample_count'] = Metric(len(values), 'count')
    extrema_values = values+[v for _, a, b in segments for v in (a, b)]
    for column, name, unit in ((0, 'load', 'N'), (2, 'displacement', 'm')):
        result[name+'_max_absolute_error'] = _metric(
            max((abs(v[column]-v[column+1]) for v in extrema_values), default=None), unit, 'no_valid_samples')
        if not duration:
            for metric, metric_unit in [('mae', unit), ('rmse', unit), ('bias', unit), ('correlation', '1')]:
                result[name+'_'+metric] = Metric(None, metric_unit, 'no_valid_duration')
            if name == 'load':
                result['load_relative_mae_percent'] = Metric(None, '%', 'no_valid_duration')
            continue
        mae = bias = square = pmean = rmean = p2 = r2 = cross = absolute_reference = Fraction(0)
        for dt, left, right in segments:
            p, r = left[column:column+2]
            q, s = right[column:column+2]
            a, b = p-r, q-s
            mae += dt*_absolute(a, b)
            bias += dt*(a+b)/2
            square += dt*_square(a, b)
            pmean += dt*(p+q)/2
            rmean += dt*(r+s)/2
            p2 += dt*_square(p, q)
            r2 += dt*_square(r, s)
            cross += dt*_product(p, q, r, s)
            absolute_reference += dt*_absolute(r, s)
        result[name+'_mae'] = _metric(mae/duration, unit)
        result[name+'_bias'] = _metric(bias/duration, unit)
        # sqrt before conversion avoids intermediate binary64 overflow.
        result[name+'_rmse'] = _sqrt_metric(square/duration, unit)
        vp, vr = p2/duration-(pmean/duration)**2, r2/duration-(rmean/duration)**2
        cov = cross/duration-pmean*rmean/duration**2
        if vr == 0 or vp == 0:
            result[name+'_correlation'] = Metric(None, '1', 'constant_reference' if vr == 0 else 'constant_prediction')
        else:
            magnitude = _sqrt_metric(cov*cov/(vp*vr), '1').value
            result[name+'_correlation'] = Metric(-magnitude if cov < 0 else magnitude, '1')
        if name == 'load':
            # Relative MAE = 100*integral|error|/integral|reference|.
            # Compare mean|reference| with the approved N threshold, not an epsilon.
            ok = absolute_reference > 0 and absolute_reference/duration >= Fraction(policy.relative_error_min_denominator_N)
            result['load_relative_mae_percent'] = (_metric(100*mae/absolute_reference, '%') if ok else
                Metric(None, '%', 'reference_denominator_below_threshold'))
    return MappingProxyType(result)


def _sqrt_metric(value, unit):
    from decimal import Decimal, localcontext
    with localcontext() as context:
        context.prec = 40
        return _metric((Decimal(value.numerator)/Decimal(value.denominator)).sqrt(), unit)


def _stroke(value):
    return 'up' if value > 0 else 'down' if value < 0 else 'stationary'


def _report(prediction, reference, policy, filled_indices=frozenset()):
    cycle = prediction.cycle
    load = _Reference(reference.load, cycle, policy)
    displacement = _Reference(reference.displacement, cycle, policy)
    evaluations, rows = [], []
    reasons = Counter()
    for i, p in enumerate(prediction.points):
        l, s = load.at(p.time_ns), displacement.at(p.time_ns)
        bad = list(p.reason_codes if p.status != 'valid' else ())
        if p.status != 'valid' and not bad:
            bad.append(p.status)
        bad.extend(v.reason for v in (l, s) if v.reason)
        if p.status == 'valid' and not all(v is not None and math.isfinite(v) for v in (p.load_N, p.s_m)):
            bad.append('invalid_prediction_value')
        ok = p.status == 'valid' and not bad
        previous = prediction.points[i-1] if i else None
        reference_edge = bool(previous is not None and p.power_index == previous.power_index+1
            and all(v.reason is None and v.value_si is not None
                    for v in (l, s, evaluations[-1].load, evaluations[-1].displacement))
            and load.supports(previous.time_ns, p.time_ns)
            and displacement.supports(previous.time_ns, p.time_ns))
        evaluations.append(Evaluation(p.time_ns, p.power_index, l, s, ok, tuple(bad), reference_edge))
        reasons.update(set(bad))
        rows.append((Fraction(p.load_N), l.value_si, Fraction(p.s_m), s.value_si) if ok else None)
    segments = []
    stroke_segments = {name: [] for name in ('up', 'down', 'stationary', 'unknown')}
    supported_indices = set()
    duration_ns = Fraction(0)
    prediction_work = reference_work = Fraction(0)
    for i, (a, b) in enumerate(zip(prediction.points, prediction.points[1:])):
        if filled_indices and a.power_index not in filled_indices and b.power_index not in filled_indices:
            continue
        if rows[i] is None or rows[i+1] is None:
            continue
        if b.power_index != a.power_index+1:
            reasons['power_index_gap'] += 1
            continue
        if not load.supports(a.time_ns, b.time_ns) or not displacement.supports(a.time_ns, b.time_ns):
            reasons['reference_interval_gap'] += 1
            continue
        dt_ns = b.time_ns-a.time_ns
        if dt_ns <= 0:
            raise ValueError('prediction_time_not_increasing')
        duration_ns += dt_ns
        dt = dt_ns/10**9
        left, right = rows[i:i+2]
        segments.append((dt, left, right))
        supported_indices.update((i, i+1))
        prediction_work += (left[0]+right[0])*(right[2]-left[2])/2
        reference_work += (left[1]+right[1])*(right[3]-left[3])/2
        velocities = []
        for p in (a, b):
            J, omega = p.intermediates.get('J_m'), p.intermediates.get('omega_rad_s')
            velocities.append(Fraction(J)*Fraction(omega) if all(v is not None and math.isfinite(v) for v in (J, omega)) else None)
        v, w = velocities
        if v is None or w is None:
            stroke_segments['unknown'].append((dt, left, right))
        elif v*w < 0:
            split = abs(v)/(abs(v)+abs(w))
            middle = tuple(x+(y-x)*split for x, y in zip(left, right))
            stroke_segments[_stroke(v)].append((dt*split, left, middle))
            stroke_segments[_stroke(w)].append((dt*(1-split), middle, right))
        else:
            stroke_segments[_stroke(v+w)].append((dt, left, right))
    sample_rows = [row for i, row in enumerate(rows) if row is not None
                   and (not filled_indices or i in supported_indices)]
    by_stroke = {}
    for name, parts in stroke_segments.items():
        samples = []
        for i, row in enumerate(rows):
            if row is None or filled_indices and i not in supported_indices:
                continue
            p = prediction.points[i]
            J, omega = p.intermediates.get('J_m'), p.intermediates.get('omega_rad_s')
            label = _stroke(Fraction(J)*Fraction(omega)) if all(v is not None and math.isfinite(v) for v in (J, omega)) else 'unknown'
            if label == name:
                samples.append(row)
        by_stroke[name] = _metrics(parts, samples, policy)
    complete = duration_ns == cycle.end_ns-cycle.start_ns and not filled_indices
    work = dict(prediction_valid_segments=_metric(prediction_work if segments else None, 'J', 'no_valid_duration'),
                reference_valid_segments=_metric(reference_work if segments else None, 'J', 'no_valid_duration'),
                prediction_complete_cycle=_metric(prediction_work if complete else None, 'J', 'incomplete_cycle_support'),
                reference_complete_cycle=_metric(reference_work if complete else None, 'J', 'incomplete_cycle_support'))
    coverage = dict(joint_duration=_metric(duration_ns/10**9, 's'),
        joint_fraction=Metric(duration_ns/(cycle.end_ns-cycle.start_ns), '1'),
        prediction_numerical_fraction=Metric(prediction.coverage_fraction, '1'))
    independence = MappingProxyType(dict(status='unknown', reason='external_evidence_admission_required',
        field_accuracy_certified=False, prediction_reference_inputs_separate=True))
    return ComparisonReport(_metrics(segments, sample_rows, policy), MappingProxyType(by_stroke),
        MappingProxyType(coverage), MappingProxyType(dict(reasons)), MappingProxyType(work), independence,
        tuple(evaluations), prediction.status, prediction.reason_codes,
        scope='display_only_filled_segments' if filled_indices else 'original_valid_support')


def compare(prediction: PredictionSeries, reference: ReferenceInputs,
            policy: AlgorithmPolicy) -> ComparisonReport:
    """Compare original support and separately report display-filled segments.

    Isolated jointly valid points can supply maximum error and sample counts,
    but never time coverage, means, correlation or work. Full-cycle work needs
    exact continuous support across the declared cycle, without closure.
    filled_comparison is display-only, restricted to intervals touching a
    derived point; its values cannot be used for primary/field acceptance.
    """
    if type(prediction) is not PredictionSeries or type(reference) is not ReferenceInputs or type(policy) is not AlgorithmPolicy:
        raise ValueError('invalid_comparison_contract')
    if any(b.time_ns <= a.time_ns or b.power_index <= a.power_index
           for a, b in zip(prediction.points, prediction.points[1:])):
        raise ValueError('prediction_order_invalid')
    primary = _report(prediction, reference, policy)
    fills = {p.power_index: p for p in fill_deadpoints(prediction, policy).fills}
    if not fills:
        return primary
    derived = tuple(replace(p, load_N=fills[p.power_index].load_N, s_m=fills[p.power_index].s_m,
                           status='valid', reason_codes=()) if p.power_index in fills else p for p in prediction.points)
    display = _report(replace(prediction, points=derived), reference, policy, frozenset(fills))
    return replace(primary, filled_comparison=display)
