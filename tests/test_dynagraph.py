"""Deterministic electric-card conversion and feature tests."""

from dataclasses import replace
import json
import math
from pathlib import Path
import unittest

from backend.algorithms.dynagraph import (
    DynagraphValidationError,
    ElectricalSample,
    MechanismParams,
    convert_electric_card,
    extract_features,
    smooth,
    split_cycle,
)


FIXTURE = Path(__file__).with_name("fixtures") / "dynagraph_cases.json"


def fixture(name):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    powers = data["cases"][name]
    samples = [
        ElectricalSample(
            phase=float(index), current_a=power * 3.8,
            voltage_v=380.0 + 2.0 * math.cos(index / len(powers) * math.tau),
            active_power_kw=power,
        )
        for index, power in enumerate(powers)
    ]
    return samples, MechanismParams(**data["mechanism"])


class DynagraphAlgorithmTests(unittest.TestCase):
    def analyze(self, name="normal"):
        return convert_electric_card(*fixture(name))

    def test_centered_smoothing_clips_boundaries_without_mutating_input(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        original = list(values)
        self.assertEqual(smooth(values, 3), [1.5, 2.0, 3.0, 4.0, 4.5])
        self.assertEqual(values, original)
        self.assertEqual(smooth(values, 1), values)

    def test_conversion_produces_120_finite_closed_physical_points(self):
        result = self.analyze()
        self.assertEqual((len(result.points), len(result.intermediate)), (120, 120))
        first, last = result.points[0], result.points[-1]
        self.assertAlmostEqual(first.displacement_m, last.displacement_m, places=12)
        self.assertAlmostEqual(first.load_kn, last.load_kn, places=12)
        self.assertAlmostEqual(min(point.displacement_m for point in result.points), 0.0, places=8)
        self.assertAlmostEqual(max(point.displacement_m for point in result.points), 2.8, delta=.002)
        self.assertTrue(all(math.isfinite(value) for point in result.points for value in (point.phase_deg, point.displacement_m, point.load_kn)))
        self.assertGreater(result.features.min_load_kn, 0.0)
        self.assertLess(result.features.max_load_kn, 120.0)
        self.assertGreater(result.features.area_kn_m, 10.0)
        self.assertGreater(result.features.max_load_kn, result.features.min_load_kn)
        self.assertEqual(result.model_version, "dynagraph-2.0")

    def test_result_exposes_units_parameters_intermediates_and_dead_center_warning(self):
        samples, params = fixture("normal")
        result = convert_electric_card(samples, params)
        units = dict(result.units)
        self.assertEqual(
            {"power", "torque", "displacement", "load", "area"}, set(units)
        )
        self.assertEqual((units["power"], units["torque"], units["area"]), ("kW", "N·m", "kN·m"))
        self.assertEqual(result.parameters, params)
        self.assertEqual(len(result.raw_samples), len(samples))
        self.assertEqual(len(result.smoothed_power_kw), len(samples))
        self.assertTrue(any("dead-center" in warning for warning in result.warnings))
        item = result.intermediate[20]
        self.assertTrue(all(math.isfinite(value) for value in (
            item.power_kw, item.motor_torque_nm, item.crank_torque_nm,
            item.balance_torque_nm, item.net_torque_nm, item.effective_torque_factor_m,
        )))

    def test_supply_shortage_has_lower_fillage_and_area_than_normal(self):
        normal = self.analyze("normal").features
        shortage = self.analyze("supply_shortage").features
        self.assertLess(shortage.fillage, normal.fillage)
        self.assertLess(shortage.area_kn_m, normal.area_kn_m)

    def test_fault_cases_have_distinguishable_feature_vectors(self):
        vectors = []
        values = {}
        for name in ("normal", "pump_leak", "empty_pump"):
            value = self.analyze(name).features
            values[name] = value
            vectors.append((round(value.area_kn_m, 3), round(value.fillage, 3), round(value.load_difference_kn, 3)))
        self.assertEqual(len(set(vectors)), 3)
        self.assertLess(values["pump_leak"].fillage, values["normal"].fillage)
        self.assertLess(values["empty_pump"].fillage, values["pump_leak"].fillage)

    def test_noisy_normal_remains_near_normal_after_smoothing(self):
        normal = self.analyze("normal").features
        noisy = self.analyze("noisy_normal").features
        self.assertLess(abs(noisy.area_kn_m / normal.area_kn_m - 1.0), .15)
        self.assertLess(abs(noisy.fillage - normal.fillage), .08)

    def test_scaling_power_stroke_and_efficiency_has_expected_direction(self):
        samples, params = fixture("normal")
        base = convert_electric_card(samples, params)
        powered = convert_electric_card([
            replace(sample, active_power_kw=sample.active_power_kw * 1.2) for sample in samples
        ], params)
        longer = convert_electric_card(samples, replace(params, stroke_m=params.stroke_m * 1.2))
        efficient = convert_electric_card(samples, replace(params, transmission_efficiency=.92))
        geared = convert_electric_card(samples, replace(params, gear_ratio=params.gear_ratio * 1.2))
        self.assertGreater(powered.features.max_load_kn, base.features.max_load_kn)
        self.assertGreater(powered.features.area_kn_m, base.features.area_kn_m)
        self.assertGreater(max(p.displacement_m for p in longer.points), max(p.displacement_m for p in base.points))
        self.assertGreater(efficient.features.area_kn_m, base.features.area_kn_m)
        self.assertLess(geared.intermediate[20].motor_torque_nm, base.intermediate[20].motor_torque_nm)
        self.assertAlmostEqual(geared.intermediate[20].crank_torque_nm, base.intermediate[20].crank_torque_nm, places=9)

    def test_repeated_calls_and_cycle_selection_are_deterministic(self):
        samples, params = fixture("normal")
        selected = split_cycle(samples, params.strokes_per_minute)
        self.assertEqual(selected, samples)
        self.assertIsNot(selected, samples)
        self.assertEqual(convert_electric_card(samples, params), convert_electric_card(samples, params))

    def test_sparse_and_dense_cycles_share_the_same_normalized_contract(self):
        sparse, params = fixture("normal")
        dense = [
            ElectricalSample(
                phase=float(index),
                current_a=(5.0 + math.sin(index / 48 * math.tau)) * 3.8,
                voltage_v=380.0,
                active_power_kw=5.0 + math.sin(index / 48 * math.tau),
            )
            for index in range(48)
        ]
        for samples in (sparse, dense):
            with self.subTest(sample_count=len(samples)):
                result = convert_electric_card(samples, params)
                self.assertEqual(len(result.points), 120)
                self.assertEqual(
                    (result.points[0].displacement_m, result.points[0].load_kn),
                    (result.points[-1].displacement_m, result.points[-1].load_kn),
                )

    def test_geometric_features_do_not_depend_on_curve_orientation(self):
        points = self.analyze("normal").points
        forward = extract_features(points)
        reverse = extract_features(tuple(reversed(points)))
        self.assertAlmostEqual(forward.area_kn_m, reverse.area_kn_m, places=10)
        self.assertAlmostEqual(forward.fillage, reverse.fillage, places=10)
        self.assertAlmostEqual(forward.top_edge_tilt_kn_per_m, reverse.top_edge_tilt_kn_per_m, places=10)
        self.assertAlmostEqual(forward.bottom_edge_tilt_kn_per_m, reverse.bottom_edge_tilt_kn_per_m, places=10)

    def test_validation_boundary_rejects_unsafe_inputs(self):
        samples, params = fixture("normal")
        invalid_calls = (
            lambda: smooth([1.0, 2.0], 2),
            lambda: smooth([1.0, float("nan"), 2.0], 3),
            lambda: split_cycle(samples[:7], params.strokes_per_minute),
            lambda: split_cycle([samples[0], samples[0], *samples[2:]], params.strokes_per_minute),
            lambda: convert_electric_card([replace(samples[0], active_power_kw=True), *samples[1:]], params),
            lambda: convert_electric_card(samples, replace(params, gear_ratio=0.0)),
            lambda: convert_electric_card(samples, replace(params, motor_efficiency=1.2)),
            lambda: extract_features([]),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(DynagraphValidationError):
                    call()


if __name__ == "__main__":
    unittest.main()
