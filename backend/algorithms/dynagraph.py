"""Explainable electric-card to surface-dynagraph conversion.

The module is deliberately pure: callers provide one electrical cycle and all
mechanism/calibration parameters.  No database, clock, random state, or hidden
field calibration is consulted.
"""

from dataclasses import dataclass
import math


MODEL_VERSION = "dynagraph-2.0"
NORMALIZED_POINT_COUNT = 120


class DynagraphValidationError(ValueError):
    """Input cannot be used safely by the conversion model."""


@dataclass(frozen=True)
class ElectricalSample:
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
    units: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    model_version: str = MODEL_VERSION


def _number(value, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DynagraphValidationError(f"{name} must be finite numeric data")
    value = float(value)
    if not minimum <= value <= maximum:
        raise DynagraphValidationError(f"{name} is outside the engineering range")
    return value


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
    """Validate and copy one monotonically sampled electric-card cycle."""
    _number(strokes_per_minute, "strokes_per_minute", .1, 20.0)
    if not isinstance(samples, list) or not 8 <= len(samples) <= 512:
        raise DynagraphValidationError("one cycle must contain 8-512 samples")
    copied = []
    last_phase = None
    for sample in samples:
        if not isinstance(sample, ElectricalSample):
            raise DynagraphValidationError("samples must be ElectricalSample values")
        phase = _number(sample.phase, "phase", -1_000_000.0, 1_000_000.0)
        _number(sample.current_a, "current_a", 0.0, 200.0)
        _number(sample.voltage_v, "voltage_v", 0.0, 1_000.0)
        _number(sample.active_power_kw, "active_power_kw", -100.0, 100.0)
        if last_phase is not None and phase <= last_phase:
            raise DynagraphValidationError("sample phases must be strictly increasing")
        last_phase = phase
        copied.append(sample)
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


def _periodic_resample(values: list[float], unique_count: int) -> list[float]:
    count = len(values)
    result = []
    for index in range(unique_count):
        position = index * count / unique_count
        left = int(math.floor(position)) % count
        fraction = position - math.floor(position)
        right = (left + 1) % count
        result.append(values[left] * (1.0 - fraction) + values[right] * fraction)
    return result


def convert_electric_card(samples: list[ElectricalSample], params: MechanismParams) -> DynagraphResult:
    """Convert one electrical cycle into a parameterized surface dynagraph."""
    _validate_params(params)
    cycle = split_cycle(samples, params.strokes_per_minute)
    smoothed = smooth([sample.active_power_kw for sample in cycle], 5)
    unique_count = NORMALIZED_POINT_COUNT - 1
    powers = _periodic_resample(smoothed, unique_count)
    omega_crank = math.tau * params.strokes_per_minute / 60.0
    omega_motor = omega_crank * params.gear_ratio
    # Near crank dead centres the idealised sine torque factor approaches zero,
    # while measured motor power still contains inertia and balance work that
    # cannot be assigned wholly to polished-rod load.  A conservative 70% floor
    # keeps the inversion finite and explicitly reports the regularisation.
    minimum_factor = max(.02, params.torque_factor_m * .70)
    phase_offset = math.radians(params.balance_phase_deg)
    intermediates = []
    points = []
    regularized = False

    for index, power_kw in enumerate(powers):
        theta = math.tau * index / unique_count
        phase_deg = 360.0 * index / unique_count
        motor_torque = power_kw * 1000.0 * params.motor_efficiency / omega_motor
        crank_torque = motor_torque * params.gear_ratio * params.transmission_efficiency
        balance_torque = params.balance_torque_nm * math.sin(theta + phase_offset)
        net_torque = crank_torque - balance_torque
        raw_factor = params.torque_factor_m * math.sin(theta)
        if abs(raw_factor) < minimum_factor:
            regularized = True
            if abs(raw_factor) < 1e-15:
                sign = 1.0 if theta <= math.pi else -1.0
            else:
                sign = math.copysign(1.0, raw_factor)
            effective_factor = sign * minimum_factor
        else:
            effective_factor = raw_factor
        displacement = params.stroke_m * .5 * (1.0 - math.cos(theta))
        load = params.static_rod_load_kn + net_torque / effective_factor / 1000.0
        intermediates.append(TorquePoint(
            phase_deg, power_kw, motor_torque, crank_torque,
            balance_torque, net_torque, effective_factor,
        ))
        points.append(LoadDisplacementPoint(phase_deg, displacement, load))

    first_intermediate = intermediates[0]
    first_point = points[0]
    intermediates.append(TorquePoint(
        360.0, first_intermediate.power_kw, first_intermediate.motor_torque_nm,
        first_intermediate.crank_torque_nm, first_intermediate.balance_torque_nm,
        first_intermediate.net_torque_nm, first_intermediate.effective_torque_factor_m,
    ))
    points.append(LoadDisplacementPoint(360.0, first_point.displacement_m, first_point.load_kn))
    warnings = []
    if regularized:
        warnings.append(
            "dead-center torque factor was regularized to avoid a kinematic singularity"
        )
    if min(powers) < 0:
        warnings.append("negative active power indicates regenerative motor operation")
    point_tuple = tuple(points)
    return DynagraphResult(
        raw_samples=tuple(cycle),
        smoothed_power_kw=tuple(smoothed),
        intermediate=tuple(intermediates),
        points=point_tuple,
        features=extract_features(point_tuple),
        parameters=params,
        units=(("power", "kW"), ("torque", "N·m"), ("displacement", "m"), ("load", "kN"), ("area", "kN·m")),
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
    # Work-area occupancy is normalized against stroke times peak absolute
    # surface load.  Unlike bounding-box occupancy, this falls as a card
    # collapses toward an empty-pump trace instead of increasing merely because
    # its vertical span shrank.
    fillage_reference = stroke_span * max(abs(maximum_y), abs(minimum_y))
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
    differences = [following.load_kn - current.load_kn for current, following in zip(points, points[1:])]
    signs = [1 if value > tolerance else -1 if value < -tolerance else 0 for value in differences]
    nonzero = [value for value in signs if value]
    turning_points = sum(first != second for first, second in zip(nonzero, nonzero[1:]))
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
