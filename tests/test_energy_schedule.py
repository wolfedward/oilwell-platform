"""Finite supplied-candidate shadow scheduling tests."""
import unittest

from backend.studies.contracts import StudyError
from backend.studies.energy_schedule import optimize_schedule


def well(well_id="W-1"):
    return {"well_id": well_id, "recovery_record_id": "record-1", "parameter_basis": "measured site inputs",
            "initial_depth_m": 120, "initial_state": "off", "initial_state_elapsed_minutes": 60,
            "annulus_storage_area_m2": 1, "pump_intake_depth_m": 200,
            "minimum_submergence_m": 1, "pumping_liquid_rate_m3_h": 2,
            "run_input_power_kw": 4, "standby_input_power_kw": 1,
            "startup_input_power_kw": 8, "startup_duration_minutes": 30,
            "min_on_minutes": 60, "min_off_minutes": 60,
            "max_starts_per_calendar_day": 2, "minimum_liquid_m3_per_calendar_day": 1,
            "terminal_max_depth_m": 150,
            "candidates": [
                {"candidate_id": "short", "segments": [{"state": "off", "duration_minutes": 60},
                                                        {"state": "on", "duration_minutes": 60},
                                                        {"state": "off", "duration_minutes": 1320}]},
                {"candidate_id": "long", "segments": [{"state": "off", "duration_minutes": 60},
                                                       {"state": "on", "duration_minutes": 120},
                                                       {"state": "off", "duration_minutes": 1260}]}],
            "recovery": {"status": "fitted", "well_id": well_id, "model_version": "recovery_depth/1",
                         "parameters": {"equilibrium_depth_m": 100, "initial_depth_m": 120,
                                        "tau_minutes": 120},
                         "support": {"depth_min_m": 50, "depth_max_m": 150,
                                     "duration_minutes": 1440},
                         "scenarios": [{"equilibrium_depth_m": 100, "initial_depth_m": 120,
                                        "tau_minutes": 120}]}}


def schedule_example():
    return {"schema": "energy.schedule/1", "horizon_start": "2026-01-01T00:00:00+08:00",
            "horizon_end": "2026-01-02T00:00:00+08:00",
            "production_day_timezone": "Asia/Shanghai", "site_power_limit_kw": 20,
            "max_simultaneous_starts": 2, "wells": [well()]}


class ScheduleTests(unittest.TestCase):
    def test_complete_enumeration_ranks_by_actual_startup_energy(self):
        result = optimize_schedule(schedule_example())
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["evaluated_combinations"], 2)
        self.assertTrue(result["search_complete"])
        self.assertEqual(result["selected_candidate_id"], "W-1:short")
        first = result["ranked_candidates"][0]
        self.assertAlmostEqual(first["daily_energy_kwh"]["2026-01-01"], 29)
        self.assertAlmostEqual(first["daily_liquid_m3"]["2026-01-01"], 2)
        self.assertEqual(first["daily_starts"]["2026-01-01"], 1)
        self.assertEqual(first["peak_power_kw"], 8)
        self.assertAlmostEqual(first["depth_trace"]["W-1"][0][1]["depth_m"], 112.13061319425267)
        self.assertAlmostEqual(first["depth_trace"]["W-1"][0][2]["depth_m"], 108.93146618457831)
        self.assertIn("declared_parameters_not_independently_verified", result["warnings"])
        self.assertIsNone(result["actual_energy_saving_kwh"])

    def test_midnight_start_count_and_daily_production_are_exact(self):
        payload = schedule_example()
        payload["horizon_end"] = "2026-01-03T00:00:00+08:00"
        payload["wells"][0]["candidates"] = [{"candidate_id": "two-days", "segments": [
            {"state": "off", "duration_minutes": 60}, {"state": "on", "duration_minutes": 60},
            {"state": "off", "duration_minutes": 1320}, {"state": "on", "duration_minutes": 60},
            {"state": "off", "duration_minutes": 1380}]}]
        result = optimize_schedule(payload)
        self.assertEqual(result["status"], "feasible")
        candidate = result["ranked_candidates"][0]
        self.assertEqual(candidate["daily_starts"], {"2026-01-01": 1, "2026-01-02": 1})
        self.assertEqual(candidate["daily_liquid_m3"], {"2026-01-01": 2, "2026-01-02": 2})

    def test_midnight_cuts_startup_energy_without_counting_second_start(self):
        payload = schedule_example()
        payload["horizon_end"] = "2026-01-03T00:00:00+08:00"
        item = payload["wells"][0]
        item["recovery"]["support"]["duration_minutes"] = 1500
        item["candidates"] = [{"candidate_id": "cross-midnight", "segments": [
            {"state": "off", "duration_minutes": 1410},
            {"state": "on", "duration_minutes": 60},
            {"state": "off", "duration_minutes": 1410}]}]
        result = optimize_schedule(payload)
        candidate = result["ranked_candidates"][0]
        self.assertEqual(candidate["daily_starts"], {"2026-01-01": 1, "2026-01-02": 0})
        self.assertEqual(candidate["daily_liquid_m3"], {"2026-01-01": 1, "2026-01-02": 1})
        self.assertAlmostEqual(candidate["daily_energy_kwh"]["2026-01-01"], 27.5)
        self.assertAlmostEqual(candidate["daily_energy_kwh"]["2026-01-02"], 25.5)

    def test_instantaneous_power_and_simultaneous_starts_reject_combination(self):
        payload = schedule_example()
        payload["wells"] = [well("W-1"), well("W-2")]
        for item in payload["wells"]:
            item["candidates"] = item["candidates"][:1]
        payload["site_power_limit_kw"] = 15
        payload["max_simultaneous_starts"] = 1
        result = optimize_schedule(payload)
        self.assertEqual(result["status"], "no_feasible_candidate")
        self.assertEqual(result["evaluated_combinations"], 1)
        self.assertIsNone(result["selected_candidate_id"])
        self.assertIn("site_power_limit", result["no_feasible_evidence"]["rejection_counts"])
        self.assertIn("simultaneous_starts", result["no_feasible_evidence"]["rejection_counts"])

    def test_no_invented_fallback_for_model_extrapolation(self):
        payload = schedule_example()
        payload["wells"][0]["recovery"]["support"]["depth_max_m"] = 119
        result = optimize_schedule(payload)
        self.assertEqual(result["status"], "no_feasible_candidate")
        self.assertEqual(result["ranked_candidates"], [])
        self.assertEqual(result["no_feasible_evidence"]["rejection_counts"]["model_extrapolation"], 2)

    def test_zero_production_does_not_meet_tiny_positive_daily_minimum(self):
        payload = schedule_example()
        item = payload["wells"][0]
        item["recovery"]["support"]["duration_minutes"] = 2000
        item["minimum_liquid_m3_per_calendar_day"] = 5e-324
        item["candidates"] = [{"candidate_id": "never-on", "segments": [
            {"state": "off", "duration_minutes": 1440}]}]
        result = optimize_schedule(payload)
        self.assertEqual(result["status"], "no_feasible_candidate")
        self.assertEqual(result["no_feasible_evidence"]["rejection_counts"]["minimum_daily_liquid"], 1)

    def test_tiny_positive_power_overload_is_not_rounded_into_feasibility(self):
        payload = schedule_example()
        payload["wells"] = [well("W-1"), well("W-2")]
        payload["site_power_limit_kw"] = 1
        for item in payload["wells"]:
            item["recovery"]["support"]["duration_minutes"] = 2000
            item["minimum_liquid_m3_per_calendar_day"] = 0
            item["candidates"] = [{"candidate_id": "never-on", "segments": [
                {"state": "off", "duration_minutes": 1440}]}]
        payload["wells"][1]["standby_input_power_kw"] = 5e-324
        result = optimize_schedule(payload)
        self.assertEqual(result["status"], "no_feasible_candidate")
        self.assertEqual(result["no_feasible_evidence"]["rejection_counts"]["site_power_limit"], 1)

    def test_decimal_power_values_equal_to_declared_limit_are_feasible(self):
        payload = schedule_example()
        payload["wells"] = [well("W-1"), well("W-2")]
        payload["site_power_limit_kw"] = 0.3
        for item, power in zip(payload["wells"], (0.1, 0.2)):
            item["standby_input_power_kw"] = power
            item["recovery"]["support"]["duration_minutes"] = 2000
            item["minimum_liquid_m3_per_calendar_day"] = 0
            item["candidates"] = [{"candidate_id": "never-on", "segments": [
                {"state": "off", "duration_minutes": 1440}]}]
        result = optimize_schedule(payload)
        self.assertEqual(result["status"], "feasible")

    def test_preexisting_shutdown_cannot_exceed_measured_recovery_duration(self):
        payload = schedule_example()
        payload["wells"][0]["initial_state_elapsed_minutes"] = 1500
        payload["wells"][0]["candidates"] = [{"candidate_id": "immediate", "segments": [
            {"state": "on", "duration_minutes": 60}, {"state": "off", "duration_minutes": 1380}]}]
        result = optimize_schedule(payload)
        self.assertEqual(result["status"], "no_feasible_candidate")
        self.assertEqual(result["no_feasible_evidence"]["rejection_counts"]["recovery_support_duration"], 1)

    def test_rejection_counts_remain_visible_with_a_feasible_choice(self):
        payload = schedule_example()
        payload["wells"][0]["candidates"][1]["segments"] = [{"state": "off", "duration_minutes": 1440}]
        result = optimize_schedule(payload)
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["no_feasible_evidence"]["rejection_counts"]["minimum_daily_liquid"], 1)

    def test_all_4096_allowed_combinations_are_evaluated_before_top_twenty(self):
        payload = schedule_example()
        payload["wells"] = [well(f"W-{index}") for index in range(4)]
        payload["site_power_limit_kw"] = 100
        payload["max_simultaneous_starts"] = 4
        for item in payload["wells"]:
            base = item["candidates"][0]["segments"]
            item["candidates"] = [{"candidate_id": f"c{index}", "segments": base}
                                  for index in range(8)]
        result = optimize_schedule(payload)
        self.assertEqual(result["evaluated_combinations"], 4096)
        self.assertEqual(result["selected_candidate_id"], "W-0:c0|W-1:c0|W-2:c0|W-3:c0")
        self.assertEqual(len(result["ranked_candidates"]), 20)
        self.assertEqual(result["no_feasible_evidence"]["rejection_counts"], {})

    def test_initial_dwell_terminal_dwell_and_daily_minimum(self):
        for edit, violation in (
            (lambda p: p["wells"][0].update(initial_state_elapsed_minutes=0, min_off_minutes=120), "minimum_off_dwell"),
            (lambda p: p["wells"][0]["candidates"][0]["segments"][-1].update(duration_minutes=30), None),
            (lambda p: p["wells"][0].update(minimum_liquid_m3_per_calendar_day=10), "minimum_daily_liquid")):
            payload = schedule_example()
            edit(payload)
            with self.subTest(violation=violation):
                if violation is None:
                    with self.assertRaises(StudyError):
                        optimize_schedule(payload)
                else:
                    result = optimize_schedule(payload)
                    self.assertEqual(result["status"], "no_feasible_candidate")
                    self.assertIn(violation, result["no_feasible_evidence"]["rejection_counts"])

    def test_malformed_inputs_and_unapproved_recovery_refused(self):
        for edit in (lambda p: p.update(production_day_timezone="UTC"),
                     lambda p: p["wells"][0]["recovery"].update(status="rejected"),
                     lambda p: p["wells"][0].update(extra=1),
                     lambda p: p["wells"][0]["recovery"]["parameters"].update(tau_minutes=0),
                     lambda p: p["wells"][0]["candidates"][0]["segments"][0].update(duration_minutes=True)):
            payload = schedule_example()
            edit(payload)
            with self.subTest(edit=edit), self.assertRaises(StudyError) as raised:
                optimize_schedule(payload)
            self.assertEqual(raised.exception.status, 422)

    def test_extreme_but_finite_geometry_never_leaks_nonfinite_depth(self):
        payload = schedule_example()
        payload["wells"][0]["annulus_storage_area_m2"] = 5e-324
        with self.assertRaises(StudyError) as raised:
            optimize_schedule(payload)
        self.assertEqual(raised.exception.code, "numeric_overflow")
