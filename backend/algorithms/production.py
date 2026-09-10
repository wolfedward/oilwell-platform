"""Traceable liquid-production estimate from pump geometry and card fillage."""

from dataclasses import dataclass, fields
import math

from backend.algorithms.dynagraph import DynagraphFeatures


MODEL_VERSION = "production-1.0"


class ProductionValidationError(ValueError):
    """Production estimate input is outside a safe engineering range."""


@dataclass(frozen=True)
class PumpParams:
    diameter_mm: float
    stroke_m: float
    strokes_per_minute: float
    pump_efficiency: float
    fillage_reference_load_span_kn: float = 25.0


@dataclass(frozen=True)
class ProductionEstimate:
    diameter_mm: float
    pump_area_m2: float
    stroke_m: float
    effective_stroke_m: float
    strokes_per_minute: float
    strokes_per_day: float
    displacement_per_stroke_m3: float
    theoretical_m3_day: float
    fillage: float
    pump_efficiency: float
    correction_coefficient: float
    estimated_m3_day: float
    fillage_basis: str
    fillage_reference_load_span_kn: float
    formula: str
    units: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    model_version: str = MODEL_VERSION


def _number(value, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProductionValidationError(f"{name} must be finite numeric data")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ProductionValidationError(f"{name} must be finite numeric data") from error
    if not math.isfinite(result):
        raise ProductionValidationError(f"{name} must be finite numeric data")
    if not minimum <= result <= maximum:
        raise ProductionValidationError(f"{name} is outside the engineering range")
    return result


def _validate_features(features: DynagraphFeatures) -> float:
    if not isinstance(features, DynagraphFeatures):
        raise ProductionValidationError("features must be DynagraphFeatures")
    ranges = {
        "max_load_kn": (-10_000.0, 10_000.0),
        "min_load_kn": (-10_000.0, 10_000.0),
        "area_kn_m": (0.0, 100_000.0),
        "fillage": (0.0, 1.0),
        "upstroke_load_kn": (-10_000.0, 10_000.0),
        "downstroke_load_kn": (-10_000.0, 10_000.0),
        "load_difference_kn": (-20_000.0, 20_000.0),
        "top_edge_tilt_kn_per_m": (-10_000.0, 10_000.0),
        "bottom_edge_tilt_kn_per_m": (-10_000.0, 10_000.0),
        "closure_error": (0.0, 100.0),
        "turning_points": (0.0, 10_000.0),
    }
    if tuple(field.name for field in fields(features)) != tuple(ranges):
        raise ProductionValidationError("unsupported DynagraphFeatures contract")
    for name, (minimum, maximum) in ranges.items():
        _number(getattr(features, name), name, minimum, maximum)
    if features.max_load_kn < features.min_load_kn:
        raise ProductionValidationError("max_load_kn cannot be lower than min_load_kn")
    if isinstance(features.turning_points, bool) or not isinstance(features.turning_points, int):
        raise ProductionValidationError("turning_points must be an integer")
    return float(features.fillage)


def estimate_liquid(
    features: DynagraphFeatures, pump: PumpParams, correction: float,
) -> ProductionEstimate:
    """Estimate daily liquid volume and expose every formula factor.

    theoretical = pump area × physical stroke × strokes/day
    estimated = theoretical × fillage × pump efficiency × correction
    """
    fillage = _validate_features(features)
    if not isinstance(pump, PumpParams):
        raise ProductionValidationError("pump must be PumpParams")
    diameter_mm = _number(pump.diameter_mm, "diameter_mm", 20.0, 200.0)
    stroke_m = _number(pump.stroke_m, "stroke_m", .1, 10.0)
    strokes_per_minute = _number(pump.strokes_per_minute, "strokes_per_minute", .1, 20.0)
    pump_efficiency = _number(pump.pump_efficiency, "pump_efficiency", .1, 1.0)
    fillage_reference = _number(
        pump.fillage_reference_load_span_kn,
        "fillage_reference_load_span_kn", 1.0, 500.0,
    )
    correction_coefficient = _number(correction, "correction", .1, 2.0)

    diameter_m = diameter_mm / 1000.0
    pump_area_m2 = math.pi * diameter_m ** 2 / 4.0
    strokes_per_day = strokes_per_minute * 60.0 * 24.0
    displacement_per_stroke_m3 = pump_area_m2 * stroke_m
    theoretical_m3_day = displacement_per_stroke_m3 * strokes_per_day
    effective_stroke_m = stroke_m * fillage
    estimated_m3_day = (
        theoretical_m3_day * fillage * pump_efficiency * correction_coefficient
    )
    warnings = [
        "dynagraph fillage depends on the well-specific full-load-span calibration used during conversion"
    ]
    if fillage == 0.0:
        warnings.append("zero fillage produces zero estimated liquid volume")
    if correction_coefficient != 1.0:
        warnings.append("field correction coefficient was applied")
    if estimated_m3_day > theoretical_m3_day:
        warnings.append("estimated liquid exceeds theoretical displacement because correction is above unity")
    return ProductionEstimate(
        diameter_mm=diameter_mm,
        pump_area_m2=pump_area_m2,
        stroke_m=stroke_m,
        effective_stroke_m=effective_stroke_m,
        strokes_per_minute=strokes_per_minute,
        strokes_per_day=strokes_per_day,
        displacement_per_stroke_m3=displacement_per_stroke_m3,
        theoretical_m3_day=theoretical_m3_day,
        fillage=fillage,
        pump_efficiency=pump_efficiency,
        correction_coefficient=correction_coefficient,
        estimated_m3_day=estimated_m3_day,
        fillage_basis="calibrated_full_card_area",
        fillage_reference_load_span_kn=fillage_reference,
        formula="estimated_m3_day = theoretical_m3_day × fillage × pump_efficiency × correction_coefficient",
        units=(
            ("diameter", "mm"),
            ("pump_area", "m²"),
            ("stroke", "m"),
            ("strokes_per_minute", "min⁻¹"),
            ("displacement_per_stroke", "m³"),
            ("theoretical_liquid", "m³/d"),
            ("estimated_liquid", "m³/d"),
        ),
        warnings=tuple(warnings),
    )
