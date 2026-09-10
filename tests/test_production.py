"""Traceable pump displacement and liquid-production tests."""

from dataclasses import replace
import math
import unittest

from backend.algorithms.dynagraph import DynagraphFeatures, convert_electric_card
from backend.algorithms.production import (
    ProductionValidationError,
    PumpParams,
    estimate_liquid,
)
from tests.test_dynagraph import fixture


def features(fillage: float = .72) -> DynagraphFeatures:
    return DynagraphFeatures(
        max_load_kn=72.0,
        min_load_kn=8.0,
        area_kn_m=132.0,
        fillage=fillage,
        upstroke_load_kn=61.0,
        downstroke_load_kn=17.0,
        load_difference_kn=44.0,
        top_edge_tilt_kn_per_m=-2.0,
        bottom_edge_tilt_kn_per_m=-5.0,
        closure_error=.01,
        turning_points=5,
    )


def pump(**overrides) -> PumpParams:
    values = {
        "diameter_mm": 44.0,
        "stroke_m": 3.0,
        "strokes_per_minute": 4.0,
        "pump_efficiency": .82,
    }
    values.update(overrides)
    return PumpParams(**values)


class ProductionTests(unittest.TestCase):
    def test_production_formula_exposes_every_intermediate_and_unit(self):
        result = estimate_liquid(features(.72), pump(), 1.0)
        expected_area = math.pi * (.044 ** 2) / 4.0
        self.assertAlmostEqual(result.pump_area_m2, expected_area, places=12)
        self.assertAlmostEqual(result.displacement_per_stroke_m3, expected_area * 3.0, places=12)
        self.assertEqual(result.strokes_per_day, 4.0 * 60.0 * 24.0)
        self.assertAlmostEqual(result.effective_stroke_m, 3.0 * .72, places=12)
        self.assertGreater(result.theoretical_m3_day, result.estimated_m3_day)
        self.assertAlmostEqual(
            result.estimated_m3_day,
            result.theoretical_m3_day * result.fillage * result.pump_efficiency,
            places=12,
        )
        self.assertEqual(result.model_version, "production-1.0")
        self.assertEqual(dict(result.units)["estimated_liquid"], "m³/d")
        self.assertEqual(result.fillage_basis, "calibrated_full_card_area")
        self.assertIn("theoretical_m3_day", result.formula)

    def test_converted_dynagraph_features_flow_into_the_estimate(self):
        converted = convert_electric_card(*fixture("normal"))
        result = estimate_liquid(converted.features, pump(), 1.0)
        self.assertEqual(result.fillage, converted.features.fillage)
        self.assertGreater(result.estimated_m3_day, 0.0)

    def test_correction_and_engineering_factors_scale_independently(self):
        base = estimate_liquid(features(), pump(), 1.0)
        corrected = estimate_liquid(features(), pump(), .91)
        wider = estimate_liquid(features(), pump(diameter_mm=55.0), 1.0)
        faster = estimate_liquid(features(), pump(strokes_per_minute=5.0), 1.0)
        self.assertAlmostEqual(corrected.estimated_m3_day, base.estimated_m3_day * .91, places=12)
        self.assertAlmostEqual(wider.theoretical_m3_day / base.theoretical_m3_day, (55 / 44) ** 2, places=12)
        self.assertAlmostEqual(faster.theoretical_m3_day / base.theoretical_m3_day, 5 / 4, places=12)
        self.assertTrue(any("correction" in warning for warning in corrected.warnings))

    def test_zero_fillage_returns_zero_with_explanatory_warning(self):
        result = estimate_liquid(features(0.0), pump(), 1.0)
        self.assertEqual(result.estimated_m3_day, 0.0)
        self.assertTrue(any("zero" in warning for warning in result.warnings))

    def test_validation_rejects_nonfinite_boolean_and_unsafe_ranges(self):
        invalid_calls = (
            lambda: estimate_liquid(features(), pump(diameter_mm=0), 1.0),
            lambda: estimate_liquid(features(), pump(stroke_m=12), 1.0),
            lambda: estimate_liquid(features(), pump(strokes_per_minute=True), 1.0),
            lambda: estimate_liquid(features(), pump(pump_efficiency=1.2), 1.0),
            lambda: estimate_liquid(features(), pump(), float("nan")),
            lambda: estimate_liquid(replace(features(), fillage=1.1), pump(), 1.0),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ProductionValidationError):
                    call()

    def test_estimation_is_deterministic_and_preserves_inputs(self):
        measured = features()
        configured = pump()
        first = estimate_liquid(measured, configured, .98)
        second = estimate_liquid(measured, configured, .98)
        self.assertEqual(first, second)
        self.assertEqual(measured, features())
        self.assertEqual(configured, pump())


if __name__ == "__main__":
    unittest.main()
