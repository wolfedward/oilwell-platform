"""Observed-depth recovery and measured-effects contract tests."""
import math
import unittest

from backend.studies.contracts import StudyError
from backend.studies.recovery import fit_recovery
from backend.studies.energy_effects import evaluate_effects


def recovery_example():
    depths = [100 + 40 * math.exp(-t / 120) for t in (0, 30, 60, 90, 120, 180, 240, 300)]
    return {
        "schema": "recovery.depth/1", "well_id": "W-1", "datum": "surface", "unit": "m",
        "shutdown_at": "2026-01-01T00:00:00+00:00", "resolution_m": 0.01,
        "accuracy_m": 0.1,
        "policy": {"tau_min_minutes": 20, "tau_max_minutes": 500, "tau_grid_points": 256,
                   "min_r_squared": 0.99, "max_rmse_m": 1,
                   "max_gap_minutes": 60, "max_relative_tau_spread": 0.3,
                   "max_equilibrium_spread_m": 5},
        "observations": [
            {"at": f"2026-01-01T{t // 60:02d}:{t % 60:02d}:00+00:00",
             "depth_m": depth, "quality": "valid", "pump_state": "off"}
            for t, depth in zip((0, 30, 60, 90, 120, 180, 240, 300), depths)]}


def effects_example():
    return {"schema": "energy.effects/1", "well_id": "W-1",
            "baseline": {"start": "2026-01-01T00:00:00+08:00",
                         "end": "2026-01-01T02:00:00+08:00",
                         "intervals": [{"start": "2026-01-01T00:00:00+08:00",
                                        "end": "2026-01-01T01:00:00+08:00",
                                        "liquid_m3": 2, "energy_kwh": 4, "quality": "valid"}]},
            "treatment": {"start": "2026-01-02T00:00:00+08:00",
                          "end": "2026-01-02T02:00:00+08:00",
                          "intervals": [{"start": "2026-01-02T00:00:00+08:00",
                                         "end": "2026-01-02T01:00:00+08:00",
                                         "liquid_m3": 2, "energy_kwh": 3, "quality": "valid"}]},
            "min_coverage_fraction": 0.5,
            "comparability": {"same_meter": True, "consistent_units": True,
                              "similar_operating_conditions": True,
                              "other_changes_recorded": True}}


class RecoveryTests(unittest.TestCase):
    def test_bounded_depth_fit_preserves_observed_points_and_sensitivity(self):
        payload = recovery_example()
        result = fit_recovery(payload)
        self.assertEqual(result["status"], "fitted")
        self.assertAlmostEqual(result["parameters"]["equilibrium_depth_m"], 100, delta=0.3)
        self.assertAlmostEqual(result["parameters"]["tau_minutes"], 120, delta=2)
        self.assertEqual(result["support"], {"depth_min_m": min(p["depth_m"] for p in payload["observations"]),
                                             "depth_max_m": 140, "duration_minutes": 300})
        self.assertEqual(len(result["fitted_points"]), 8)
        self.assertEqual(result["uncertainty"]["method"], "leave_one_out_sensitivity")
        self.assertIsNone(result["uncertainty"]["statistical_confidence"])
        self.assertLessEqual(len(result["scenarios"]), 5)
        self.assertTrue(result["shadow_only"])
        self.assertFalse(result["dispatch_allowed"])

    def test_invalid_quality_is_excluded_but_long_observed_gap_rejects(self):
        payload = recovery_example()
        payload["observations"][5]["quality"] = "invalid"
        result = fit_recovery(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("excessive_gap", result["reason_codes"])
        self.assertEqual(result["excluded_count"], 1)
        self.assertEqual(result["excluded_points"][0]["reason"], "invalid_quality")
        self.assertIsNone(result["parameters"])

    def test_flat_reversed_sparse_and_ill_conditioned_are_rejected(self):
        for edit in (lambda p: [v.update(depth_m=100) for v in p["observations"]],
                     lambda p: [v.update(depth_m=100 + i) for i, v in enumerate(p["observations"])],
                     lambda p: [v.update(quality="invalid") for v in p["observations"][2:]],
                     lambda p: p["policy"].update(tau_min_minutes=200, tau_max_minutes=220)):
            payload = recovery_example()
            edit(payload)
            with self.subTest(edit=edit):
                result = fit_recovery(payload)
                self.assertEqual(result["status"], "rejected")
                self.assertIsNone(result["parameters"])
                self.assertTrue(result["reason_codes"])

    def test_structural_errors_are_typed_and_accuracy_unknown_is_preserved(self):
        for edit in (lambda p: p.update(unit="ft"),
                     lambda p: p["observations"][1].update(at=p["observations"][0]["at"]),
                     lambda p: p["observations"][0].update(depth_m=True),
                     lambda p: p["observations"][0].update(extra=1),
                     lambda p: p.update(shutdown_at="2026-01-01T00:00:00")):
            payload = recovery_example()
            edit(payload)
            with self.subTest(edit=edit), self.assertRaises(StudyError) as raised:
                fit_recovery(payload)
            self.assertEqual(raised.exception.status, 422)
        payload = recovery_example()
        payload["accuracy_m"] = None
        self.assertFalse(fit_recovery(payload)["uncertainty"]["measurement_accuracy_known"])

    def test_oversized_integer_is_a_typed_validation_error(self):
        payload = recovery_example()
        payload["resolution_m"] = 10 ** 1000
        with self.assertRaises(StudyError):
            fit_recovery(payload)

    def test_observation_span_is_separate_from_missing_shutdown_start(self):
        payload = recovery_example()
        payload["shutdown_at"] = "2025-12-25T00:00:00+00:00"
        result = fit_recovery(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("missing_shutdown_start", result["reason_codes"])


class EffectsTests(unittest.TestCase):
    def test_observed_rates_only_never_claim_causal_savings(self):
        result = evaluate_effects(effects_example())
        self.assertEqual(result["status"], "descriptive")
        self.assertEqual(result["baseline"]["coverage_fraction"], 0.5)
        self.assertEqual(result["treatment"]["energy_kwh"], 3)
        self.assertEqual(result["comparison"]["energy_kwh_per_observed_hour_difference"], -1)
        self.assertIsNone(result["actual_energy_saving_kwh"])
        self.assertFalse(result["causal_effect_proven"])

    def test_missing_coverage_and_noncomparable_conditions_are_insufficient(self):
        payload = effects_example()
        payload["min_coverage_fraction"] = 0.75
        result = evaluate_effects(payload)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["baseline"]["energy_kwh"], 4)
        payload = effects_example()
        payload["treatment"]["intervals"][0]["quality"] = "unknown"
        result = evaluate_effects(payload)
        self.assertIsNone(result["treatment"]["energy_kwh_per_observed_hour"])
        self.assertIsNone(result["comparison"]["energy_kwh_per_observed_hour_difference"])

    def test_overlap_and_unknown_fields_are_rejected(self):
        for edit in (lambda p: p["treatment"].update(start="2026-01-01T01:00:00+08:00"),
                     lambda p: p["baseline"]["intervals"][0].update(energy_kwh=-1),
                     lambda p: p["comparability"].update(extra=True)):
            payload = effects_example()
            edit(payload)
            with self.subTest(edit=edit), self.assertRaises(StudyError):
                evaluate_effects(payload)

    def test_nonoverlapping_effect_intervals_need_not_be_submitted_in_order(self):
        payload = effects_example()
        first = payload["baseline"]["intervals"][0]
        second = {"start": "2026-01-01T01:00:00+08:00",
                  "end": "2026-01-01T02:00:00+08:00",
                  "liquid_m3": 3, "energy_kwh": 5, "quality": "valid"}
        payload["baseline"]["intervals"] = [second, first]
        result = evaluate_effects(payload)
        self.assertEqual(result["baseline"]["coverage_fraction"], 1)
        self.assertEqual(result["baseline"]["energy_kwh"], 9)

    def test_unhashable_quality_is_typed_validation_error(self):
        for quality in ([], {}):
            payload = effects_example()
            payload["baseline"]["intervals"][0]["quality"] = quality
            with self.subTest(quality=quality):
                with self.assertRaises(StudyError) as raised:
                    evaluate_effects(payload)
                self.assertEqual(raised.exception.status, 422)
