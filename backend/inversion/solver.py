"""Signed surface-load energy balance, independent of reference measurements."""
from dataclasses import dataclass
from fractions import Fraction
import math
from types import MappingProxyType
from typing import Mapping

from .geometry import _require_parameters, build_geometry, NUMERICAL_ASSUMPTIONS
from .losses import _finite, _fraction, _upper, lookup_loss
from .mechanics import EnergyTerms, energy_terms
from .motion import MotionPoint, estimate_motion, _angle_runs
from .parameter_contracts import ParameterSet
from .policy import AlgorithmPolicy
from .series import CycleWindow, PredictionInputs


@dataclass(frozen=True)
class LoadPoint:
    load_N: float | None
    error_bound_N: float | None
    status: str
    reason_codes: tuple[str, ...]
    intermediates: Mapping


def solve_load(*, drive_Nm: float, energy: EnergyTerms, motion: MotionPoint,
               J_m: float, parameters: ParameterSet) -> LoadPoint:
    """Solve F=N/J without static load offset or deadpoint regularization.

    N=Tdrive-U'-M*alpha-.5*M'*omega**2-(Tc+b*omega).
    uN=approved torque bound + independent numerical torque budget;
    uJ=approved factor bound + independent numerical factor budget.
    For |J|>uJ, uF=(uN+|F|*uJ)/(|J|-uJ). For N=10, J=.5,
    uN=1, uJ=.1 and explicit zero numerical budgets: F=20, uF=7.5.

    Rational algebra measures final numerator/load rounding exactly. The
    torque budget must cover the energy estimate and these rounding terms.
    Direct callers supply an exact nominal drive; predict also accounts for
    conversion of power/loss to drive. Physical loss uncertainty is NOT added
    again: its coverage is an independent model/admission evidence obligation.
    """
    return _solve_load(drive_Nm=drive_Nm, energy=energy, motion=motion, J_m=J_m,
                       parameters=parameters, drive_error_Nm=Fraction(0))


def _solve_load(*, drive_Nm, energy, motion, J_m, parameters, drive_error_Nm):
    _require_parameters(parameters)
    diagnostic = {}
    def result(status, reason=None, load=None, error=None):
        return LoadPoint(load, error, status, () if reason is None else (reason,),
                         MappingProxyType(dict(diagnostic)))
    if motion.status != 'valid':
        return result('motion_unreliable', motion.status)
    try:
        drive, J = _fraction(drive_Nm), _fraction(J_m)
        omega, alpha = _fraction(motion.omega_rad_s), _fraction(motion.alpha_rad_s2)
        speed_error = _fraction(motion.speed_error_bound_rad_s)
        uncertainty = parameters.uncertainty
        torque_budget = _fraction(uncertainty.numerical_budget_Nm)
        factor_budget = _fraction(uncertainty.numerical_factor_budget_m)
        uN = _fraction(uncertainty.torque_bound_Nm)+torque_budget
        uJ = _fraction(uncertainty.factor_bound_m)+factor_budget
        energy_error = _fraction(energy.numerical_torque_error_Nm)
        if min(speed_error, torque_budget, factor_budget, uN, uJ, energy_error, drive_error_Nm) < 0:
            raise ValueError('negative_uncertainty')
        diagnostic.update(drive_Nm=_finite(drive), J_m=_finite(J),
                          omega_rad_s=_finite(omega), alpha_rad_s2=_finite(alpha),
                          torque_bound_Nm=_upper(uN), factor_bound_m=_upper(uJ))
        if omega-speed_error <= max(Fraction(0), _fraction(parameters.domain.speed_rad_s.min)):
            return result('unobservable', 'speed_uncertain')
        if (not _fraction(parameters.domain.speed_rad_s.min) <= omega <= _fraction(parameters.domain.speed_rad_s.max)
                or not _fraction(parameters.domain.acceleration_rad_s2.min) <= alpha <= _fraction(parameters.domain.acceleration_rad_s2.max)):
            return result('outside_model_domain', 'motion_outside_domain')
        friction = (_fraction(parameters.drive.crank_friction_Nm)
                    + _fraction(parameters.drive.viscous_friction_Nm_s)*omega)
        N = (drive-_fraction(energy.U_prime_Nm)-_fraction(energy.M_kg_m2)*alpha
             - _fraction(energy.M_prime_kg_m2)*omega*omega/2-friction)
        rounded_N = _finite(N)
        diagnostic.update(N_Nm=rounded_N, friction_Nm=_finite(friction),
                          U_prime_Nm=_finite(energy.U_prime_Nm), M_kg_m2=_finite(energy.M_kg_m2),
                          M_prime_kg_m2=_finite(energy.M_prime_kg_m2))
        # A decimal boundary such as .1 rounds upward in binary64. Refuse
        # equality to that outward representation as well as exact equality.
        if abs(J) <= Fraction(_upper(uJ)):
            return result('unobservable', 'factor_uncertain')
        nominal = N/J
        load = _finite(nominal)
        # N is retained for diagnostics; division uses its unrounded value.
        arithmetic_error = abs(N-Fraction(rounded_N))+abs(nominal-Fraction(load))*abs(J)
        numerical_error = energy_error+drive_error_Nm+arithmetic_error
        diagnostic.update(nominal_load_N=load, numerical_torque_error_Nm=_upper(numerical_error))
        if numerical_error > torque_budget:
            return result('motion_unreliable', 'solver_numerical_budget_exceeded')
        error = _upper((uN+abs(Fraction(load))*uJ)/(abs(J)-uJ))
        if not _fraction(parameters.domain.load_N.min) <= Fraction(load) <= _fraction(parameters.domain.load_N.max):
            return result('outside_model_domain', 'load_outside_domain', error=error)
        if Fraction(error) > _fraction(parameters.policy.max_load_error_N):
            return result('unobservable', 'load_error_exceeded', error=error)
        return result('valid', load=load, error=error)
    except (ValueError, OverflowError, ZeroDivisionError, TypeError):
        return result('motion_unreliable', 'numerical_failure')


@dataclass(frozen=True)
class PredictionPoint:
    time_ns: Fraction
    power_index: int
    s_m: float | None
    load_N: float | None
    error_bound_N: float | None
    status: str
    reason_codes: tuple[str, ...]
    intermediates: Mapping


@dataclass(frozen=True)
class PredictionSeries:
    points: tuple[PredictionPoint, ...]
    cycle: CycleWindow
    status: str
    reason_codes: tuple[str, ...]
    coverage_fraction: Fraction
    metadata: Mapping


_MOTION_STATUS = {
    'invalid_power_quality': 'input_gap',
    'power_support_outside_cycle': 'input_gap',
    'insufficient_angle_support': 'input_gap',
    'ambiguous_angle_unwrap': 'motion_unreliable',
    'invalid_angle_motion': 'motion_unreliable',
    'motion_time_span_exceeded': 'motion_unreliable',
    'motion_angle_span_exceeded': 'motion_unreliable',
    'numerical_failure': 'motion_unreliable',
    'motion_condition_exceeded': 'motion_unreliable',
    'motion_residual_exceeded': 'motion_unreliable',
    'motion_theta_precision_exceeded': 'motion_unreliable',
    'speed_outside_domain': 'outside_model_domain',
    'acceleration_outside_domain': 'outside_model_domain',
    'speed_uncertain': 'unobservable',
}


def _full_cycle(inputs, parameters, policy):
    """Check original endpoint evidence; never extrapolate angular boundaries.

    The two endpoint consistency ceilings give 2*motion_max_residual_rad.
    This is conditional consistency with an independently evidenced physical
    cycle, not a measurement-derived proof of angular accuracy. The tolerance
    must be less than half a turn to keep the turn count unambiguous.
    """
    target = Fraction.from_float(math.tau)  # Same period as motion unwrap.
    tolerance = 2*_fraction(policy.motion_max_residual_rad)
    detail = dict(span_rad=None, target_rad=target, tolerance_rad=tolerance,
                  reason='missing_cycle_boundary_samples')
    if not 0 <= tolerance < target/2:
        detail['reason'] = 'ambiguous_cycle_tolerance'
        return False, MappingProxyType(detail)
    for samples, angles, refusal in _angle_runs(inputs, parameters):
        if samples[0].time_ns != inputs.cycle.start_ns or samples[-1].time_ns != inputs.cycle.end_ns:
            continue
        if refusal is not None:
            detail['reason'] = refusal
            return False, MappingProxyType(detail)
        span = angles[-1]-angles[0]
        detail['span_rad'] = span
        valid = abs(span-target) <= tolerance
        detail['reason'] = 'full_cycle_consistent' if valid else 'angle_span_not_one_turn'
        return valid, MappingProxyType(detail)
    return False, MappingProxyType(detail)


def predict(inputs: PredictionInputs, parameters: ParameterSet,
            policy: AlgorithmPolicy) -> PredictionSeries:
    """Numerically conditional prediction at every native power center.

    No reference channel can enter this interface. Coverage is the sum of
    intervals between valid, adjacent native indices divided by the exact
    selected cycle duration. Invalid points and index gaps split support;
    isolated points and unsupported cycle edges contribute no duration. No-valid,
    zero-duration and low-coverage series are not_applicable, with diagnostics
    retained. Runtime must enforce independent evidence approval, applicability,
    loss/model-envelope coverage and current authorizations BEFORE calling this
    pure kernel. Evidence pins alone do not prove field accuracy or admission.
    """
    if type(inputs) is not PredictionInputs or type(policy) is not AlgorithmPolicy:
        raise ValueError('invalid_prediction_contract')
    _require_parameters(parameters)
    if (parameters.source_kind == 'unknown' or not parameters.uncertainty.evidence_refs
            or not parameters.drive.loss_grid.evidence_refs):
        raise ValueError('uncertainty_coverage_unknown')
    if policy != parameters.policy:
        raise ValueError('parameter_policy_mismatch')
    if not 1 <= len(inputs.power.samples) <= 32768 or not 1 <= len(inputs.crank_angle.samples) <= 32768:
        raise ValueError('selection_sample_limit_exceeded')
    full_cycle, cycle_detail = _full_cycle(inputs, parameters, policy)
    motions = estimate_motion(inputs, parameters, policy)
    geometry = build_geometry(parameters) if full_cycle else None
    points = []
    for sample, moving in zip(inputs.power.samples, motions):
        diagnostic = {name: getattr(moving, name) for name in (
            'theta_rad', 'omega_rad_s', 'alpha_rad_s2', 'angle_indices', 'window_span_ns',
            'residual_rad', 'condition', 'speed_error_bound_rad_s', 'theta_error_estimate_rad')}
        displacement = None
        def append(status, reasons, load=None, error=None):
            points.append(PredictionPoint(sample.time_ns, sample.index, displacement,
                load, error, status, reasons, MappingProxyType(dict(diagnostic))))
        if moving.status != 'valid':
            append(_MOTION_STATUS[moving.status], (moving.status,))
            continue
        if not full_cycle:
            append('motion_unreliable', ('full_cycle_unconfirmed', cycle_detail['reason']))
            continue
        if not parameters.domain.window_ns.min <= inputs.power_window_ns <= parameters.domain.window_ns.max:
            append('outside_model_domain', ('power_window_outside_domain',))
            continue
        if not _fraction(parameters.domain.power_W.min) <= sample.value_si <= _fraction(parameters.domain.power_W.max):
            append('outside_model_domain', ('power_outside_domain',))
            continue
        try:
            omega, alpha = _fraction(moving.omega_rad_s), _fraction(moving.alpha_rad_s2)
            motor_speed = _fraction(parameters.drive.ratio)*omega
            loss = lookup_loss(parameters, sample.value_si, motor_speed)
            diagnostic.update(power_W=_finite(sample.value_si), motor_speed_rad_s=_finite(motor_speed),
                              loss_W=loss.loss_W, loss_error_W=loss.error_W,
                              loss_numerical_error_W=loss.numerical_error_W)
            # Check a necessary envelope consistency condition, without treating
            # numeric coverage as proof of independent evidence approval.
            if _fraction(loss.error_W)/omega > _fraction(parameters.uncertainty.torque_bound_Nm):
                append('outside_model_domain', ('loss_error_exceeds_torque_envelope',))
                continue
            drive_exact = (sample.value_si-_fraction(loss.loss_W))/omega-_fraction(parameters.drive.upstream_inertia_kg_m2)*alpha
            drive = _finite(drive_exact)
            drive_error = abs(drive_exact-Fraction(drive))+_fraction(loss.numerical_error_W)/omega
            point = geometry.at(moving.theta_rad)
            displacement = point.s_m
            energy = energy_terms(geometry, moving.theta_rad, parameters)
            solved = _solve_load(drive_Nm=drive, energy=energy, motion=moving, J_m=point.J_m,
                                 parameters=parameters, drive_error_Nm=drive_error)
            diagnostic.update(solved.intermediates)
            diagnostic.update(geometry_factor_error_m=point.numerical_factor_error_m,
                              geometry_displacement_error_m=point.numerical_displacement_error_m,
                              energy_numerical_torque_error_Nm=energy.numerical_torque_error_Nm)
            append(solved.status, solved.reason_codes, solved.load_N, solved.error_bound_N)
        except (ValueError, OverflowError, ZeroDivisionError) as error:
            if str(error) == 'loss_outside_domain':
                append('outside_model_domain', ('loss_outside_domain',))
            else:
                append('motion_unreliable', ('numerical_failure',))
    valid_count = sum(point.status == 'valid' for point in points)
    supported_duration_ns = sum((right.time_ns-left.time_ns
        for left, right in zip(points, points[1:])
        if left.status == right.status == 'valid'
        and right.power_index == left.power_index+1), Fraction(0))
    coverage = supported_duration_ns/(inputs.cycle.end_ns-inputs.cycle.start_ns)
    reasons = []
    if not full_cycle:
        reasons.append('full_cycle_unconfirmed')
    if valid_count == 0:
        reasons.append('no_valid_points')
    if supported_duration_ns <= 0 or coverage < _fraction(policy.min_coverage_fraction):
        reasons.append('insufficient_coverage')
    metadata = MappingProxyType(dict(
        algorithm=policy.algorithm, model_version=policy.model_version,
        parameter_sha256=parameters.raw_sha256, policy_sha256=policy.raw_sha256,
        applicability_scope='conditional_numerical_only',
        external_evidence_admission_required=True, field_accuracy_certified=False,
        full_cycle=cycle_detail,
        numerical_assumptions=NUMERICAL_ASSUMPTIONS + (
            'Motion QR and residual amplification are conditional estimates, not certified physical derivative bounds',
            'Approved physical envelopes must cover loss calibration, angular policy, timing and unmodeled dynamics; pins alone are not evidence admission',
            'Solver algebra and bilinear interpolation use exact rationals; output rounding is checked against independent numerical budgets',
        )))
    return PredictionSeries(tuple(points), inputs.cycle, 'not_applicable' if reasons else 'applicable',
                            tuple(reasons), coverage, metadata)
