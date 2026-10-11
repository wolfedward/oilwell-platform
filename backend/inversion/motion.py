"""Local crank motion at each original power-mean center, without resampling.

Sample times are already calibrated. Subtract Fraction epochs exactly and
convert only local seconds to float; never apply calibration a second time.
Theta grows along actual motion: encoder_direction maps the readings, while
geometry.rotation_sign belongs solely to the world-coordinate geometry.

This kernel establishes LOCAL fit eligibility only. A complete revolution is
an additional solver/input gate. Mean support (including time uncertainty)
must lie inside the inclusive cycle. Angular support must contain the center;
one-sided endpoint fits are allowed. The independently approved model envelope
covers averaging mismatch and unobserved dynamics; fit residuals do not prove
physical derivative accuracy or exclude hidden frequencies.

The approved angular residual ceiling also covers the conditional local angle
numerical estimate and final theta reconstruction loss. Unwrapped theta stays
unwrapped: reconstruct its exact offset plus fitted local value before one
float rounding, and measure that rounding loss exactly. This tightens the
existing ceiling; it introduces no additional tolerance or phase convention.
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from fractions import Fraction
import math

from .parameter_contracts import ParameterSet
from .policy import AlgorithmPolicy
from .series import PredictionInputs, Sample
from .small_linalg import cubic_fit


@dataclass(frozen=True)
class MotionPoint:
    power_index: int
    time_ns: Fraction
    status: str
    theta_rad: float | None = None
    omega_rad_s: float | None = None
    alpha_rad_s2: float | None = None
    angle_indices: tuple[int, ...] = ()
    window_span_ns: Fraction | None = None
    residual_rad: float | None = None
    condition: float | None = None
    speed_error_bound_rad_s: float | None = None
    theta_error_estimate_rad: Fraction | None = None


def _angle_runs(inputs: PredictionInputs, parameters: ParameterSet) -> list[
        tuple[tuple[Sample, ...], tuple[Fraction, ...], str | None]]:
    """Return (native samples, unwrapped radians, refusal) per contiguous run.

    Runs are split by quality/index gaps and clipped to the inclusive cycle.
    A refusal poisons its entire run; its angle tuple contains only the prefix
    before the failing increment. Successful runs retain all samples, including
    cycle-edge angles, for the solver's separate full-revolution eligibility.
    Wrapped values use the binary64 representation of 2*pi as their period.
    """
    runs = []
    for sample in inputs.crank_angle.samples:
        if not inputs.cycle.start_ns <= sample.time_ns <= inputs.cycle.end_ns:
            continue
        if sample.quality != 0:
            if runs and runs[-1]:
                runs.append([])
            continue
        if not runs or runs[-1] and runs[-1][-1].index+1 != sample.index:
            runs.append([])
        runs[-1].append(sample)
    result = []
    phase = parameters.phase
    speed_max = Fraction(parameters.domain.speed_rad_s.max)
    tau = Fraction.from_float(math.tau)
    for run in runs:
        if not run:
            continue
        mapped = [phase.encoder_direction*(sample.value_si-Fraction(phase.zero_reading_rad)) for sample in run]
        status = None
        if phase.unwrap_kind == 'wrapped_2pi':
            angles = [mapped[0] % tau]
        else:
            angles = [mapped[0]]
        for i in range(1, len(run)):
            maximum = speed_max*(run[i].time_ns-run[i-1].time_ns)/10**9
            delta = mapped[i]-mapped[i-1]
            if phase.unwrap_kind == 'wrapped_2pi':
                delta %= tau
                # Exactly one forward candidate in [0, approved max speed*dt].
                if delta+tau <= maximum:
                    status = 'ambiguous_angle_unwrap'
                    break
            if delta < 0 or delta > maximum:
                status = 'invalid_angle_motion'
                break
            angles.append(angles[-1]+delta)
        result.append((tuple(run), tuple(angles), status))
    return result


def estimate_motion(inputs: PredictionInputs, parameters: ParameterSet,
                    policy: AlgorithmPolicy) -> tuple[MotionPoint, ...]:
    """Return one immutable status/result for every native power sample.

    Seven indices are present whenever a fit window was selected. Unavailable
    windows have an empty index tuple and no fabricated numerical outputs.
    speed_error_bound = approved envelope + residual slope amplification +
    approved |acceleration|max * time uncertainty + numerical QR estimate.
    These added terms can overlap the approved envelope; retaining all terms
    is conservative. Residual amplification is an observed-fit diagnostic,
    never a substitute for the independent physical/model envelope.
    theta_error_estimate_rad stores the conditional local coefficient-angle
    estimate plus exact final reconstruction loss, as a Fraction to avoid
    losing precision at the combined ceiling. Raw residual_rad stays separate.
    """
    groups = ('provenance', 'geometry', 'phase', 'bodies', 'drive', 'domain', 'uncertainty', 'policy')
    if (parameters.is_draft or parameters.missing
            or any(getattr(parameters, group) is None for group in groups)):
        raise ValueError('incomplete_motion_parameters')
    if (parameters.phase.unwrap_kind not in ('wrapped_2pi', 'unwrapped')
            or parameters.phase.encoder_direction not in (-1, 1)
            or parameters.domain.speed_rad_s.max <= 0):
        raise ValueError('invalid_motion_parameters')
    runs = _angle_runs(inputs, parameters)
    starts = [run[0][0].time_ns for run in runs]
    run_times = [tuple(sample.time_ns for sample in run[0]) for run in runs]
    half_support = Fraction(inputs.power_window_ns, 2)+inputs.time_bound_ns
    points = []
    for power in inputs.power.samples:
        point = MotionPoint(power.index, power.time_ns, 'insufficient_angle_support')
        if power.quality != 0:
            points.append(replace(point, status='invalid_power_quality'))
            continue
        if (power.time_ns-half_support < inputs.cycle.start_ns
                or power.time_ns+half_support > inputs.cycle.end_ns):
            points.append(replace(point, status='power_support_outside_cycle'))
            continue
        run_index = bisect_right(starts, power.time_ns)-1
        if run_index < 0:
            points.append(point)
            continue
        samples, angles, unwrap_error = runs[run_index]
        if samples[-1].time_ns < power.time_ns or len(samples) < 7:
            points.append(point)
            continue
        if unwrap_error:
            points.append(replace(point, status=unwrap_error))
            continue
        # Three strictly preceding points, four at/after; shift only at edges.
        insertion = bisect_left(run_times[run_index], power.time_ns)
        start = max(0, min(insertion-3, len(samples)-7))
        selected = samples[start:start+7]
        values = angles[start:start+7]
        point = replace(point, angle_indices=tuple(sample.index for sample in selected),
                        window_span_ns=selected[-1].time_ns-selected[0].time_ns)
        if point.window_span_ns > policy.motion_max_time_span_ns:
            points.append(replace(point, status='motion_time_span_exceeded'))
            continue
        if values[-1]-values[0] > Fraction(policy.motion_max_angle_span_rad):
            points.append(replace(point, status='motion_angle_span_exceeded'))
            continue
        try:
            times = tuple(float((sample.time_ns-power.time_ns)/10**9) for sample in selected)
            # Exact angular subtraction preserves tiny changes at large offsets.
            fit = cubic_fit(times, tuple(float(value-values[0]) for value in values))
            reconstructed_theta = values[0] + Fraction.from_float(fit.coefficients[0])
            theta = float(reconstructed_theta)
            # QR's speed estimate originates from the whole scaled coefficient
            # vector; undo its seconds conversion for the angular component.
            # This remains a conditional software estimate, not a certified
            # physical bound. Only the final float rounding loss is exact.
            theta_error = (Fraction.from_float(fit.numerical_speed_error_rad_s)
                           * Fraction.from_float(fit.time_scale_s)
                           + abs(Fraction.from_float(theta)-reconstructed_theta))
            omega = fit.coefficients[1]
            alpha = 2*fit.coefficients[2]
            speed_error = (float(parameters.uncertainty.speed_bound_rad_s)
                           + fit.residual_rad*fit.speed_sensitivity_per_s
                           + float(max(abs(parameters.domain.acceleration_rad_s2.min),
                                       abs(parameters.domain.acceleration_rad_s2.max)))
                           * float(inputs.time_bound_ns/10**9)
                           + fit.numerical_speed_error_rad_s)
            if not all(math.isfinite(x) for x in (theta, omega, alpha, speed_error)):
                raise ValueError('nonfinite_motion')
        except (ValueError, OverflowError, ZeroDivisionError):
            points.append(replace(point, status='numerical_failure'))
            continue
        point = replace(point, theta_rad=theta, omega_rad_s=omega, alpha_rad_s2=alpha,
                        residual_rad=fit.residual_rad, condition=fit.condition,
                        speed_error_bound_rad_s=speed_error,
                        theta_error_estimate_rad=theta_error)
        if fit.condition > float(policy.motion_max_condition):
            status = 'motion_condition_exceeded'
        elif Fraction.from_float(fit.residual_rad) > Fraction(policy.motion_max_residual_rad):
            status = 'motion_residual_exceeded'
        elif Fraction.from_float(fit.residual_rad)+theta_error > Fraction(policy.motion_max_residual_rad):
            status = 'motion_theta_precision_exceeded'
        elif not float(parameters.domain.speed_rad_s.min) <= omega <= float(parameters.domain.speed_rad_s.max):
            status = 'speed_outside_domain'
        elif not float(parameters.domain.acceleration_rad_s2.min) <= alpha <= float(parameters.domain.acceleration_rad_s2.max):
            status = 'acceleration_outside_domain'
        elif omega-speed_error <= max(0.0, float(parameters.domain.speed_rad_s.min)):
            status = 'speed_uncertain'
        else:
            status = 'valid'
        points.append(replace(point, status=status))
    return tuple(points)
