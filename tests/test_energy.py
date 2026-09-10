"""Recovery-curve fitting and adaptive pumping-cycle optimizer tests."""

from dataclasses import replace
import json
import math
from pathlib import Path
import unittest

from backend.algorithms.energy import (
    EnergyValidationError,
    RecoveryModel,
    RecoveryPoint,
    StrategyConstraints,
    fit_recovery_curve,
    optimize_cycle,
)


FIXTURE = Path(__file__).with_name("fixtures") / "recovery_cases.json"


def recovery_fixture(name="low_output_a"):
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][name]
    points = [RecoveryPoint(**item) for item in case["observations"]]
    return points, case


class RecoveryFitTests(unittest.TestCase):
    def test_fit_recovers_time_constant_capacity_baseline_and_quality(self):
        points, case = recovery_fixture()
        model = fit_recovery_curve(points)
        self.assertAlmostEqual(model.tau_minutes, case["expected_tau_minutes"], delta=8.0)
        self.assertAlmostEqual(model.baseline, .82, delta=.12)
        self.assertAlmostEqual(model.capacity, 11.0, delta=.35)
        self.assertGreater(model.r_squared, .995)
        self.assertEqual(model.observation_count, len(points))
        self.assertEqual(model.formula, "q(t) = baseline + capacity × (1 - exp(-t / tau_minutes))")
        self.assertEqual(dict(model.units), {"time": "min", "liquid_rate": "m³/d"})

    def test_second_curve_and_repeated_calls_are_deterministic(self):
        points, case = recovery_fixture("low_output_b")
        before = list(points)
        first = fit_recovery_curve(points)
        second = fit_recovery_curve(points)
        reordered = fit_recovery_curve(list(reversed(points)))
        self.assertEqual(first, second)
        self.assertEqual(first, reordered)
        self.assertEqual(points, before)
        self.assertAlmostEqual(first.tau_minutes, case["expected_tau_minutes"], delta=12.0)
        self.assertGreater(first.r_squared, .995)

    def test_fit_rejects_unsafe_or_unidentifiable_observations(self):
        valid, _ = recovery_fixture()
        invalid = (
            valid[:3],
            [*valid[:-1], RecoveryPoint(valid[-2].shut_in_minutes, 10.0)],
            [RecoveryPoint(index * 10, 2.0) for index in range(5)],
            [*valid[:-1], RecoveryPoint(math.nan, 1.0)],
            [*valid[:-1], RecoveryPoint(400, -1.0)],
        )
        for points in invalid:
            with self.subTest(points=points):
                with self.assertRaises(EnergyValidationError):
                    fit_recovery_curve(points)

    def test_every_successful_fit_is_accepted_by_the_optimizer_contract(self):
        steep = [
            RecoveryPoint(time, rate)
            for time, rate in ((0, 100), (10, 433.33), (20, 766.67), (30, 1100))
        ]
        model = fit_recovery_curve(steep)
        self.assertLessEqual(model.capacity, 10_000.0)
        candidates = optimize_cycle(
            model,
            StrategyConstraints(20, 120, 15, 180, 2.4, 12),
            18.5,
        )
        self.assertTrue(candidates)


class CycleOptimizerTests(unittest.TestCase):
    def setUp(self):
        self.model = fit_recovery_curve(recovery_fixture()[0])
        self.constraints = StrategyConstraints(
            min_on_minutes=20,
            max_on_minutes=120,
            min_off_minutes=15,
            max_off_minutes=180,
            min_liquid_m3_day=2.4,
            max_starts_per_day=12,
        )

    def test_optimizer_keeps_production_constraint_and_sorts_energy(self):
        candidates = optimize_cycle(self.model, self.constraints, motor_kw=18.5)
        feasible = [item for item in candidates if item.feasible]
        self.assertGreater(len(feasible), 2)
        self.assertTrue(all(item.predicted_liquid_m3 >= 2.4 for item in feasible))
        self.assertTrue(all(math.ceil(item.cycles_per_day) <= 12 for item in feasible))
        self.assertEqual(
            feasible,
            sorted(feasible, key=lambda item: (
                item.predicted_energy_kwh, -item.predicted_liquid_m3,
                item.on_minutes, item.off_minutes,
            )),
        )
        best = feasible[0]
        self.assertGreater(best.saving_rate, 0.0)
        self.assertAlmostEqual(
            best.predicted_energy_kwh,
            18.5 * best.runtime_hours,
            places=9,
        )

    def test_candidates_expose_formula_inputs_and_rejected_reasons(self):
        candidates = optimize_cycle(self.model, self.constraints, motor_kw=18.5)
        self.assertTrue(any(not item.feasible for item in candidates))
        rejected = next(item for item in candidates if not item.feasible)
        self.assertTrue(rejected.violations)
        sample = candidates[0]
        self.assertGreater(sample.recovered_liquid_rate_m3_day, self.model.baseline)
        self.assertAlmostEqual(sample.runtime_fraction, sample.runtime_hours / 24.0, places=12)
        self.assertEqual(dict(sample.units)["energy"], "kWh/d")
        self.assertIn("runtime_fraction", sample.formula)

    def test_stricter_liquid_target_never_reduces_best_feasible_energy(self):
        base = [item for item in optimize_cycle(self.model, self.constraints, 18.5) if item.feasible]
        stricter = [item for item in optimize_cycle(
            self.model,
            replace(self.constraints, min_liquid_m3_day=3.5),
            18.5,
        ) if item.feasible]
        self.assertTrue(stricter)
        self.assertGreaterEqual(
            stricter[0].predicted_energy_kwh,
            base[0].predicted_energy_kwh,
        )

    def test_zero_recovery_and_impossible_constraints_are_honestly_infeasible(self):
        zero = RecoveryModel(
            capacity=0.0, tau_minutes=86.0, baseline=0.0,
            r_squared=1.0, observation_count=5,
            formula="q(t) = baseline + capacity × (1 - exp(-t / tau_minutes))",
            units=(("time", "min"), ("liquid_rate", "m³/d")),
        )
        candidates = optimize_cycle(zero, self.constraints, 18.5)
        self.assertFalse(any(item.feasible for item in candidates))
        self.assertTrue(all("minimum_liquid" in item.violations for item in candidates))

    def test_validation_rejects_boolean_nonfinite_and_unsafe_ranges(self):
        bad_constraints = replace(self.constraints, max_starts_per_day=0)
        for model, constraints, motor in (
            (self.model, bad_constraints, 18.5),
            (replace(self.model, tau_minutes=math.inf), self.constraints, 18.5),
            (replace(self.model, r_squared=.49), self.constraints, 18.5),
            (replace(self.model, units=(("time", "s"), ("liquid_rate", "bbl/d"))), self.constraints, 18.5),
            (self.model, self.constraints, True),
            (self.model, self.constraints, 0.0),
        ):
            with self.subTest(model=model, constraints=constraints, motor=motor):
                with self.assertRaises(EnergyValidationError):
                    optimize_cycle(model, constraints, motor)


if __name__ == "__main__":
    unittest.main()
