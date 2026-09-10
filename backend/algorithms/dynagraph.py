"""Explainable electric-card to surface-dynagraph conversion.

The module is deliberately pure: callers provide one electrical cycle and all
mechanism/calibration parameters.  No database, clock, random state, or hidden
field calibration is consulted.
"""

from dataclasses import dataclass
from bisect import bisect_right
import math


MODEL_VERSION = "dynagraph-2.0"
NORMALIZED_POINT_COUNT = 120


class DynagraphValidationError(ValueError):
    """Input cannot be used safely by the conversion model."""


@dataclass(frozen=True)
class ElectricalSample:
    """One crank-angle sample in degrees, or a legacy sequential phase index."""

    phase: float
    current_a: float
    voltage_v: float
    active_power_kw: float


@dataclass(frozen=True)
class MechanismParams:
    stroke_m: float
    strokes_per_minute: float
    motor_efficiency: float
    gear_ratio: float
    transmission_efficiency: float
    balance_torque_nm: float
    balance_phase_deg: float
    torque_factor_m: float
    static_rod_load_kn: float
    dead_center_exclusion_deg: float = 10.0


@dataclass(frozen=True)
class LoadDisplacementPoint:
    phase_deg: float
    displacement_m: float
    load_kn: float


@dataclass(frozen=True)
class TorquePoint:
    phase_deg: float
    power_kw: float
    motor_torque_nm: float
    crank_torque_nm: float
    balance_torque_nm: float
    net_torque_nm: float
    effective_torque_factor_m: float
    load_inferred: bool


@dataclass(frozen=True)
class EnergyCheck:
    input_electrical_power_kw: float
    available_mechanical_power_kw: float
    card_power_kw: float
    conversion_ratio: float


@dataclass(frozen=True)
class DynagraphFeatures:
    max_load_kn: float
    min_load_kn: float
    area_kn_m: float
    fillage: float
    upstroke_load_kn: float
    downstroke_load_kn: float
    load_difference_kn: float
    top_edge_tilt_kn_per_m: float
    bottom_edge_tilt_kn_per_m: float
    closure_error: float
    turning_points: int


@dataclass(frozen=True)
class DynagraphResult:
    raw_samples: tuple[ElectricalSample, ...]
    smoothed_power_kw: tuple[float, ...]
    intermediate: tuple[TorquePoint, ...]
    points: tuple[LoadDisplacementPoint, ...]
    features: DynagraphFeatures
    parameters: MechanismParams
    energy: EnergyCheck
    units: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    model_version: str = MODEL_VERSION


def _number(value, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DynagraphValidationError(f"{name} must be finite numeric data")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise DynagraphValidationError(f"{name} must be finite numeric data") from error
    if not math.isfinite(result):
        raise DynagraphValidationError(f"{name} must be finite numeric data")
    if not minimum <= result <= maximum:
        raise DynagraphValidationError(f"{name} is outside the engineering range")
    return result


def smooth(samples: list[float], window: int = 5) -> list[float]:
    """Return a centered moving average using clipped endpoint windows."""
    if not isinstance(samples, list) or not samples:
        raise DynagraphValidationError("samples must be a non-empty list")
    if (
        not isinstance(window, int) or isinstance(window, bool)
        or window < 1 or window % 2 == 0 or window > len(samples)
    ):
        raise DynagraphValidationError("window must be an odd integer within the sample count")
    values = [_number(value, "sample", -1_000_000.0, 1_000_000.0) for value in samples]
    radius = window // 2
    return [
        sum(values[max(0, index - radius):min(len(values), index + radius + 1)])
        / len(values[max(0, index - radius):min(len(values), index + radius + 1)])
        for index in range(len(values))
    ]


def split_cycle(samples: list[ElectricalSample], strokes_per_minute: float) -> list[ElectricalSample]:
    """Validate and copy one degree-indexed electric-card cycle."""
    _number(strokes_per_minute, "strokes_per_minute", .1, 20.0)
    if not isinstance(samples, list) or not 8 <= len(samples) <= 512:
        raise DynagraphValidationError("one cycle must contain 8-512 samples")
    copied = []
    last_phase = None
    for sample in samples:
        if not isinstance(sample, ElectricalSample):
            raise DynagraphValidationError("samples must be ElectricalSample values")
        phase = _number(sample.phase, "phase", 0.0, 511.0)
        _number(sample.current_a, "current_a", 0.0, 200.0)
        _number(sample.voltage_v, "voltage_v", 0.0, 1_000.0)
        _number(sample.active_power_kw, "active_power_kw", -100.0, 100.0)
        if last_phase is not None and phase <= last_phase:
            raise DynagraphValidationError("sample phases must be strictly increasing")
        last_phase = phase
        copied.append(sample)
    phases = [float(sample.phase) for sample in copied]
    legacy_sequential = all(
        math.isclose(phase, float(index), rel_tol=0.0, abs_tol=1e-9)
        for index, phase in enumerate(phases)
    )
    if legacy_sequential:
        return list(copied)
    if phases[-1] >= 360.0:
        raise DynagraphValidationError("degree phase must be within [0, 360)")
    circular_gaps = [
        following - current
        for current, following in zip(phases, [*phases[1:], phases[0] + 360.0])
    ]
    maximum_allowed_gap = max(60.0, 2.5 * 360.0 / len(phases))
    if max(circular_gaps) > maximum_allowed_gap:
        raise DynagraphValidationError(
            "sample phases do not cover one 360-degree cycle with adequate continuity"
        )
    return list(copied)


def _validate_params(params: MechanismParams) -> None:
    if not isinstance(params, MechanismParams):
        raise DynagraphValidationError("params must be MechanismParams")
    _number(params.stroke_m, "stroke_m", .1, 10.0)
    _number(params.strokes_per_minute, "strokes_per_minute", .1, 20.0)
    _number(params.motor_efficiency, "motor_efficiency", .1, 1.0)
    _number(params.gear_ratio, "gear_ratio", 1.0, 500.0)
    _number(params.transmission_efficiency, "transmission_efficiency", .1, 1.0)
    _number(params.balance_torque_nm, "balance_torque_nm", 0.0, 100_000.0)
    _number(params.balance_phase_deg, "balance_phase_deg", -360.0, 360.0)
    _number(params.torque_factor_m, "torque_factor_m", .02, 10.0)
    _number(params.static_rod_load_kn, "static_rod_load_kn", 0.0, 500.0)
    _number(params.dead_center_exclusion_deg, "dead_center_exclusion_deg", 1.0, 20.0)
    expected_factor = params.stroke_m / 2.0
    if not math.isclose(params.torque_factor_m, expected_factor, rel_tol=.02, abs_tol=.002):
        raise DynagraphValidationError(
            "torque_factor_m must equal stroke_m / 2 within the simple-harmonic geometry tolerance"
        )


def _periodic_resample(phases_deg: list[float], values: list[float], unique_count: int) -> list[float]:
    """Interpolate a cyclic signal on its measured degree coordinates."""
    extended_phases = [phases_deg[-1] - 360.0, *phases_deg, phases_deg[0] + 360.0]
    extended_values = [values[-1], *values, values[0]]
    result = []
    for index in range(unique_count):
        target = 360.0 * index / unique_count
        left = bisect_right(extended_phases, target) - 1
        span = extended_phases[left + 1] - extended_phases[left]
        fraction = (target - extended_phases[left]) / span
        result.append(
            extended_values[left] * (1.0 - fraction)
            + extended_values[left + 1] * fraction
        )
    return result


def _interpolate_unobservable(values: list[float | None]) -> list[float]:
    """Interpolate short dead-centre gaps on a uniform circular phase grid."""
    if all(value is None for value in values):
        raise DynagraphValidationError("dead-center exclusion removed the entire cycle")
    result = list(values)
    count = len(result)
    for index, value in enumerate(result):
        if value is not None:
            continue
        previous = (index - 1) % count
        while result[previous] is None:
            previous = (previous - 1) % count
        following = (index + 1) % count
        while result[following] is None:
            following = (following + 1) % count
        previous_distance = (index - previous) % count
        following_distance = (following - index) % count
        total = previous_distance + following_distance
        result[index] = (
            float(result[previous]) * following_distance
            + float(result[following]) * previous_distance
        ) / total
    return [float(value) for value in result]


def convert_electric_card(samples: list[ElectricalSample], params: MechanismParams) -> DynagraphResult:
    """Convert one electrical cycle into a parameterized surface dynagraph."""
    _validate_params(params)
    cycle = split_cycle(samples, params.strokes_per_minute)
    smoothed = smooth([sample.active_power_kw for sample in cycle], 5)
    unique_count = NORMALIZED_POINT_COUNT - 1
    phases = [float(sample.phase) for sample in cycle]
    legacy_phase = all(
        math.isclose(phase, float(index), rel_tol=0.0, abs_tol=1e-9)
        for index, phase in enumerate(phases)
    )
    interpolation_phases = (
        [360.0 * index / len(phases) for index in range(len(phases))]
        if legacy_phase else phases
    )
    powers = _periodic_resample(interpolation_phases, smoothed, unique_count)
    omega_crank = math.tau * params.strokes_per_minute / 60.0
    omega_motor = omega_crank * params.gear_ratio
    phase_offset = math.radians(params.balance_phase_deg)
    torque_values = []
    load_values = []
    inferred_flags = []

    for index, power_kw in enumerate(powers):
        theta = math.tau * index / unique_count
        phase_deg = 360.0 * index / unique_count
        motor_torque = power_kw * 1000.0 * params.motor_efficiency / omega_motor
        crank_torque = motor_torque * params.gear_ratio * params.transmission_efficiency
        balance_torque = params.balance_torque_nm * math.sin(theta + phase_offset)
        net_torque = crank_torque - balance_torque
        raw_factor = params.torque_factor_m * math.sin(theta)
        distance_to_dead_center = min(
            phase_deg, abs(phase_deg - 180.0), 360.0 - phase_deg,
        )
        inferred = distance_to_dead_center < params.dead_center_exclusion_deg
        load = None if inferred else (
            params.static_rod_load_kn + net_torque / raw_factor / 1000.0
        )
        torque_values.append((
            phase_deg, power_kw, motor_torque, crank_torque,
            balance_torque, net_torque, raw_factor,
        ))
        load_values.append(load)
        inferred_flags.append(inferred)

    loads = _interpolate_unobservable(load_values)
    intermediates = []
    points = []
    for values, load, inferred in zip(torque_values, loads, inferred_flags):
        phase_deg = values[0]
        theta = math.radians(phase_deg)
        displacement = params.torque_factor_m * (1.0 - math.cos(theta))
        intermediates.append(TorquePoint(*values, inferred))
        points.append(LoadDisplacementPoint(phase_deg, displacement, load))

    first_intermediate = intermediates[0]
    first_point = points[0]
    intermediates.append(TorquePoint(
        360.0, first_intermediate.power_kw, first_intermediate.motor_torque_nm,
        first_intermediate.crank_torque_nm, first_intermediate.balance_torque_nm,
        first_intermediate.net_torque_nm, first_intermediate.effective_torque_factor_m,
        first_intermediate.load_inferred,
    ))
    points.append(LoadDisplacementPoint(360.0, first_point.displacement_m, first_point.load_kn))
    warnings = [
        "dead-center load was inferred within "
        f"±{params.dead_center_exclusion_deg:.1f}° "
        f"({sum(inferred_flags)}/{unique_count} normalized points)"
    ]
    if legacy_phase:
        warnings.append(
            "legacy sequential phase indices were mapped to a 360-degree cycle; raw samples were preserved"
        )
    if min(powers) < 0:
        warnings.append("negative active power indicates regenerative motor operation")
    point_tuple = tuple(points)
    features = extract_features(point_tuple)
    electrical_power = sum(powers) / len(powers)
    available_power = electrical_power * params.motor_efficiency * params.transmission_efficiency
    card_power = features.area_kn_m * params.strokes_per_minute / 60.0
    conversion_ratio = card_power / available_power if available_power > 1e-12 else 0.0
    if conversion_ratio > 1.15:
        warnings.append("card power exceeds the available mechanical-power tolerance")
    elif conversion_ratio < .50:
        warnings.append("card power is below half of available mechanical power")
    return DynagraphResult(
        raw_samples=tuple(cycle),
        smoothed_power_kw=tuple(smoothed),
        intermediate=tuple(intermediates),
        points=point_tuple,
        features=features,
        parameters=params,
        energy=EnergyCheck(
            electrical_power, available_power, card_power, conversion_ratio,
        ),
        units=(("phase", "deg"), ("power", "kW"), ("torque", "N·m"), ("torque_factor", "m"), ("displacement", "m"), ("load", "kN"), ("area", "kN·m")),
        warnings=tuple(warnings),
    )


def _linear_slope(points: list[LoadDisplacementPoint]) -> float:
    if len(points) < 2:
        return 0.0
    x_mean = sum(point.displacement_m for point in points) / len(points)
    y_mean = sum(point.load_kn for point in points) / len(points)
    denominator = sum((point.displacement_m - x_mean) ** 2 for point in points)
    if denominator <= 1e-15:
        return 0.0
    return sum(
        (point.displacement_m - x_mean) * (point.load_kn - y_mean)
        for point in points
    ) / denominator


def extract_features(points: list[LoadDisplacementPoint] | tuple[LoadDisplacementPoint, ...]) -> DynagraphFeatures:
    """Extract orientation-independent geometric and stroke-load features."""
    if not isinstance(points, (list, tuple)) or len(points) < 8:
        raise DynagraphValidationError("at least eight dynagraph points are required")
    for point in points:
        if not isinstance(point, LoadDisplacementPoint):
            raise DynagraphValidationError("points must be LoadDisplacementPoint values")
        _number(point.phase_deg, "phase_deg", -3600.0, 3600.0)
        _number(point.displacement_m, "displacement_m", -1000.0, 1000.0)
        _number(point.load_kn, "load_kn", -10_000.0, 10_000.0)
    minimum_x = min(point.displacement_m for point in points)
    maximum_x = max(point.displacement_m for point in points)
    minimum_y = min(point.load_kn for point in points)
    maximum_y = max(point.load_kn for point in points)
    stroke_span = maximum_x - minimum_x
    load_span = maximum_y - minimum_y
    if stroke_span <= 1e-12 or load_span <= 1e-12:
        raise DynagraphValidationError("dynagraph spans must be non-degenerate")

    area = abs(sum(
        current.displacement_m * following.load_kn - following.displacement_m * current.load_kn
        for current, following in zip(points, [*points[1:], points[0]])
    )) * .5
    # Normalized card-area occupancy is invariant to the chosen load zero.  It
    # is a geometric fillage proxy; field pump-fillage calibration is applied
    # separately by the production model.
    fillage_reference = stroke_span * load_span
    fillage = max(0.0, min(1.0, area / fillage_reference))
    upstroke = [point for point in points if 0.0 <= point.phase_deg < 180.0]
    downstroke = [point for point in points if 180.0 <= point.phase_deg < 360.0]
    if not upstroke or not downstroke:
        raise DynagraphValidationError("points must cover both stroke halves")
    up_load = sum(point.load_kn for point in upstroke) / len(upstroke)
    down_load = sum(point.load_kn for point in downstroke) / len(downstroke)
    top_band = [point for point in points if point.displacement_m >= minimum_x + .8 * stroke_span]
    bottom_band = [point for point in points if point.displacement_m <= minimum_x + .2 * stroke_span]
    closure = math.hypot(
        points[-1].displacement_m - points[0].displacement_m,
        points[-1].load_kn - points[0].load_kn,
    ) / math.hypot(stroke_span, load_span)
    tolerance = max(.05, load_span * .01)
    differences = [
        following.load_kn - current.load_kn
        for current, following in zip(points, [*points[1:], points[0]])
    ]
    signs = [1 if value > tolerance else -1 if value < -tolerance else 0 for value in differences]
    nonzero = [value for value in signs if value]
    turning_points = sum(
        first != second
        for first, second in zip(nonzero, [*nonzero[1:], nonzero[0]])
    ) if nonzero else 0
    return DynagraphFeatures(
        max_load_kn=maximum_y,
        min_load_kn=minimum_y,
        area_kn_m=area,
        fillage=fillage,
        upstroke_load_kn=up_load,
        downstroke_load_kn=down_load,
        load_difference_kn=up_load - down_load,
        top_edge_tilt_kn_per_m=_linear_slope(top_band),
        bottom_edge_tilt_kn_per_m=_linear_slope(bottom_band),
        closure_error=closure,
        turning_points=turning_points,
    )
