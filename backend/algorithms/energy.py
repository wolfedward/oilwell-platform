"""Deterministic recovery-curve fitting and adaptive pumping-cycle search."""

from dataclasses import dataclass
import math


MODEL_VERSION = "energy-1.0"
RECOVERY_FORMULA = "q(t) = baseline + capacity × (1 - exp(-t / tau_minutes))"
CANDIDATE_FORMULA = (
    "predicted_liquid_m3 = recovered_liquid_rate_m3_day × runtime_fraction; "
    "predicted_energy_kwh = motor_kw × runtime_hours"
)


class EnergyValidationError(ValueError):
    """Recovery observations or operating limits are not safe to evaluate."""


@dataclass(frozen=True)
class RecoveryPoint:
    shut_in_minutes: float
    liquid_rate_m3_day: float


@dataclass(frozen=True)
class RecoveryModel:
    capacity: float
    tau_minutes: float
    baseline: float
    r_squared: float
    observation_count: int
    formula: str = RECOVERY_FORMULA
    units: tuple[tuple[str, str], ...] = (
        ("time", "min"), ("liquid_rate", "m³/d"),
    )
    model_version: str = MODEL_VERSION


@dataclass(frozen=True)
class StrategyConstraints:
    min_on_minutes: int
    max_on_minutes: int
    min_off_minutes: int
    max_off_minutes: int
    min_liquid_m3_day: float
    max_starts_per_day: int


@dataclass(frozen=True)
class CycleCandidate:
    on_minutes: int
    off_minutes: int
    cycles_per_day: float
    starts_per_day: int
    runtime_fraction: float
    runtime_hours: float
    recovered_liquid_rate_m3_day: float
    predicted_liquid_m3: float
    predicted_energy_kwh: float
    continuous_energy_kwh: float
    saving_rate: float
    feasible: bool
    violations: tuple[str, ...]
    formula: str = CANDIDATE_FORMULA
    units: tuple[tuple[str, str], ...] = (
        ("time", "min"), ("liquid", "m³/d"), ("energy", "kWh/d"),
    )
    model_version: str = MODEL_VERSION


def _number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EnergyValidationError(f"{name} must be finite numeric data")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise EnergyValidationError(f"{name} must be finite numeric data") from error
    if not math.isfinite(result):
        raise EnergyValidationError(f"{name} must be finite numeric data")
    if not minimum <= result <= maximum:
        raise EnergyValidationError(f"{name} is outside the engineering range")
    return result


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise EnergyValidationError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise EnergyValidationError(f"{name} is outside the engineering range")
    return value


def fit_recovery_curve(observations: list[RecoveryPoint]) -> RecoveryModel:
    """Fit the bounded first-order recovery response by reproducible grid search.

    For each integer tau from 10 through 720 minutes, ordinary least squares
    solves the intercept (baseline) and response magnitude (capacity).  Only
    physically non-negative solutions participate in the final SSE ranking.
    """
    if not isinstance(observations, list) or len(observations) < 4:
        raise EnergyValidationError("at least four recovery observations are required")
    if not all(isinstance(point, RecoveryPoint) for point in observations):
        raise EnergyValidationError("observations must contain RecoveryPoint values")
    pairs = sorted([
        (
            _number(point.shut_in_minutes, "shut_in_minutes", 0.0, 10_080.0),
            _number(point.liquid_rate_m3_day, "liquid_rate_m3_day", 0.0, 10_000.0),
        )
        for point in observations
    ])
    times = [item[0] for item in pairs]
    values = [item[1] for item in pairs]
    if len(set(times)) != len(times):
        raise EnergyValidationError("recovery observation times must be unique")
    if max(times) - min(times) < 10.0:
        raise EnergyValidationError("recovery observations cover too little shut-in time")
    mean_value = sum(values) / len(values)
    total_variation = sum((value - mean_value) ** 2 for value in values)
    if total_variation <= 1e-12:
        raise EnergyValidationError("recovery response is not identifiable")

    best = None
    for tau in range(10, 721):
        x_values = [1.0 - math.exp(-time / tau) for time in times]
        mean_x = sum(x_values) / len(x_values)
        x_variation = sum((value - mean_x) ** 2 for value in x_values)
        if x_variation <= 1e-15:
            continue
        capacity = sum(
            (x_value - mean_x) * (value - mean_value)
            for x_value, value in zip(x_values, values)
        ) / x_variation
        baseline = mean_value - capacity * mean_x
        if capacity <= 1e-9 or capacity > 10_000.0 or not 0.0 <= baseline <= 10_000.0:
            continue
        residual = sum(
            (value - (baseline + capacity * x_value)) ** 2
            for x_value, value in zip(x_values, values)
        )
        candidate = (residual, tau, capacity, baseline)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        raise EnergyValidationError("recovery response has no physical first-order fit")
    residual, tau, capacity, baseline = best
    r_squared = max(0.0, min(1.0, 1.0 - residual / total_variation))
    if r_squared < .50:
        raise EnergyValidationError("recovery observations do not support a reliable first-order fit")
    return RecoveryModel(
        capacity=capacity,
        tau_minutes=float(tau),
        baseline=baseline,
        r_squared=r_squared,
        observation_count=len(pairs),
    )


def _validated_model(model):
    if not isinstance(model, RecoveryModel):
        raise EnergyValidationError("model must be a RecoveryModel")
    capacity = _number(model.capacity, "capacity", 0.0, 10_000.0)
    tau = _number(model.tau_minutes, "tau_minutes", 10.0, 720.0)
    baseline = _number(model.baseline, "baseline", 0.0, 10_000.0)
    r_squared = _number(model.r_squared, "r_squared", .50, 1.0)
    _integer(model.observation_count, "observation_count", 4, 1_000_000)
    if model.formula != RECOVERY_FORMULA:
        raise EnergyValidationError("unsupported recovery formula")
    if model.units != (("time", "min"), ("liquid_rate", "m³/d")):
        raise EnergyValidationError("recovery model units are incompatible")
    if model.model_version != MODEL_VERSION:
        raise EnergyValidationError("unsupported recovery model version")
    return capacity, tau, baseline, r_squared


def _validated_constraints(constraints):
    if not isinstance(constraints, StrategyConstraints):
        raise EnergyValidationError("constraints must be StrategyConstraints")
    min_on = _integer(constraints.min_on_minutes, "min_on_minutes", 10, 720)
    max_on = _integer(constraints.max_on_minutes, "max_on_minutes", 10, 720)
    min_off = _integer(constraints.min_off_minutes, "min_off_minutes", 15, 720)
    max_off = _integer(constraints.max_off_minutes, "max_off_minutes", 15, 720)
    minimum_liquid = _number(
        constraints.min_liquid_m3_day, "min_liquid_m3_day", 0.01, 1_000.0,
    )
    maximum_starts = _integer(
        constraints.max_starts_per_day, "max_starts_per_day", 1, 96,
    )
    if min_on > max_on:
        raise EnergyValidationError("minimum on-time cannot exceed maximum on-time")
    if min_off > max_off:
        raise EnergyValidationError("minimum off-time cannot exceed maximum off-time")
    return min_on, max_on, min_off, max_off, minimum_liquid, maximum_starts


def _grid_start(minimum, step):
    return int(math.ceil(minimum / step) * step)


def optimize_cycle(
    model: RecoveryModel,
    constraints: StrategyConstraints,
    motor_kw: float,
) -> list[CycleCandidate]:
    """Enumerate bounded on/off cycles and rank feasible energy use first."""
    capacity, tau, baseline, _r_squared = _validated_model(model)
    min_on, max_on, min_off, max_off, minimum_liquid, maximum_starts = (
        _validated_constraints(constraints)
    )
    motor_power = _number(motor_kw, "motor_kw", .1, 5_000.0)
    continuous_energy = motor_power * 24.0
    candidates = []
    for on_minutes in range(_grid_start(min_on, 10), max_on + 1, 10):
        for off_minutes in range(_grid_start(min_off, 15), max_off + 1, 15):
            cycle_minutes = on_minutes + off_minutes
            cycles = 1440.0 / cycle_minutes
            starts = math.ceil(cycles - 1e-12)
            runtime_fraction = on_minutes / cycle_minutes
            runtime_hours = 24.0 * runtime_fraction
            recovered_rate = baseline + capacity * (1.0 - math.exp(-off_minutes / tau))
            predicted_liquid = recovered_rate * runtime_fraction
            predicted_energy = motor_power * runtime_hours
            saving_rate = max(0.0, min(1.0, 1.0 - predicted_energy / continuous_energy))
            violations = []
            if predicted_liquid + 1e-12 < minimum_liquid:
                violations.append("minimum_liquid")
            if starts > maximum_starts:
                violations.append("maximum_starts")
            candidates.append(CycleCandidate(
                on_minutes=on_minutes,
                off_minutes=off_minutes,
                cycles_per_day=cycles,
                starts_per_day=starts,
                runtime_fraction=runtime_fraction,
                runtime_hours=runtime_hours,
                recovered_liquid_rate_m3_day=recovered_rate,
                predicted_liquid_m3=predicted_liquid,
                predicted_energy_kwh=predicted_energy,
                continuous_energy_kwh=continuous_energy,
                saving_rate=saving_rate,
                feasible=not violations,
                violations=tuple(violations),
            ))
    if not candidates:
        raise EnergyValidationError("constraints produce no ten/fifteen-minute candidates")
    return sorted(
        candidates,
        key=lambda item: (
            not item.feasible,
            item.predicted_energy_kwh if item.feasible else len(item.violations),
            -item.predicted_liquid_m3,
            item.on_minutes,
            item.off_minutes,
        ),
    )
