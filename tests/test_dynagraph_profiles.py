"""Inverse electric profiles must survive the real forward conversion."""

from dataclasses import asdict
import math
import random
import unittest

from backend.algorithms.dynagraph import ElectricalSample, MechanismParams, convert_electric_card
from backend.algorithms.dynagraph_profiles import build_profile_card, profile_condition
from backend.algorithms import dynagraph_profiles
from backend.services.analysis import compare_dynagraphs


CONDITIONS = ("normal", "supply_shortage", "intermittent", "pump_leak", "empty_pump", "rod_break")
MECHANISM = {"stroke_m": 2.8, "strokes_per_minute": 4.2}
PROFILE_CODES = (
    "normal", "traveling_valve_leak", "standing_valve_leak", "double_valve_leak",
    "rod_break", "tubing_leak", "supply_shortage", "gas_interference", "gas_lock",
    "discharge_blockage", "filter_blockage", "high_water_cut", "tight_packing",
    "sand_obstruction", "partial_plunger_exit", "upstroke_hang", "downstroke_hang",
    "stuck_plunger", "loose_liner", "flowing_well",
)


def convert(card):
    return convert_electric_card([
        ElectricalSample(sample["phase"], sample["current"], sample["voltage"], sample["power"])
        for sample in card["electric_samples"]
    ], MechanismParams(**card["mechanism"]))


class DynagraphProfileTests(unittest.TestCase):
    def test_twenty_profiles_have_unique_features_and_sensor_samples(self):
        self.assertEqual(tuple(getattr(dynagraph_profiles, "PROFILE_DEFINITIONS", {})), PROFILE_CODES)
        vectors = set()
        for code in PROFILE_CODES:
            card = build_profile_card(code, MECHANISM, variation=.3)
            self.assertEqual(card["profile_code"], code)
            self.assertTrue(any("\u4e00" <= c <= "\u9fff" for c in card["profile_label"]))
            self.assertEqual(card, build_profile_card(code, MECHANISM, variation=.3))
            self.assertEqual(len(card["sensor_samples"]), 120)
            for point in card["sensor_samples"]:
                self.assertEqual(set(point), {"phase", "displacement", "load"})
                self.assertTrue(all(math.isfinite(v) for v in point.values()))
            vectors.add(tuple(round(v, 3) for v in asdict(convert(card).features).values()))
            self.assertLess(compare_dynagraphs(convert(card).points, card["sensor_samples"])["rmse_kn"], 3, code)
        self.assertEqual(len(vectors), 20)
        normal = convert(build_profile_card("normal", MECHANISM)).features
        self.assertGreaterEqual(normal.load_difference_kn, 20)
        self.assertLessEqual(normal.load_difference_kn, 35)
        self.assertGreater(normal.fillage, .8)

    def test_electrical_profiles_have_distinct_shape_not_only_power_scale(self):
        vectors = set()
        for code in PROFILE_CODES:
            card = build_profile_card(code, MECHANISM)
            powers = [p["power"] for p in card["electric_samples"]]
            scale = max(abs(p) for p in powers)
            vectors.add(tuple(round(p / scale, 3) for p in powers[::5]))
        self.assertEqual(len(vectors), 20)

    def test_profiles_produce_complete_closed_engineering_cards(self):
        """Wrong inverse torque direction or a missing static load breaks physical bounds."""
        for condition in CONDITIONS:
            with self.subTest(condition=condition):
                card = build_profile_card(condition, MECHANISM)
                self.assertEqual(card["kind"], "electric_card")
                self.assertEqual(card["profile"], condition)
                samples = card["electric_samples"]
                self.assertEqual(len(samples), 120)
                self.assertEqual(samples[0]["phase"], 0.0)
                self.assertEqual(samples[-1]["phase"], 357.0)
                self.assertTrue(all(a["phase"] < b["phase"] for a, b in zip(samples, samples[1:])))
                result = convert(card)
                self.assertEqual(len(result.points), 120)
                self.assertEqual(result.points[0].load_kn, result.points[-1].load_kn)
                self.assertEqual(result.points[0].displacement_m, result.points[-1].displacement_m)
                self.assertEqual(min(p.displacement_m for p in result.points), 0.0)
                self.assertAlmostEqual(max(p.displacement_m for p in result.points), 2.8, delta=.002)
                self.assertGreaterEqual(result.features.min_load_kn, 25.0)
                self.assertLessEqual(result.features.max_load_kn, 90.0)
                self.assertEqual(result.features.closure_error, 0.0)
                self.assertGreater(result.energy.conversion_ratio, .7)
                self.assertLess(result.energy.conversion_ratio, 1.15)

    def test_all_six_shapes_have_distinct_features_and_leak_is_not_power_scaling(self):
        """Amplitude-only faults lose branch shape and cannot represent valve leakage."""
        cards = {name: build_profile_card(name, MECHANISM) for name in CONDITIONS}
        features = {name: convert(card).features for name, card in cards.items()}
        vectors = {tuple(round(v, 3) for v in asdict(f).values()) for f in features.values()}
        self.assertEqual(len(vectors), 6)
        self.assertGreater(features["normal"].fillage, .8)
        self.assertLess(features["supply_shortage"].fillage, features["normal"].fillage)
        self.assertLess(features["empty_pump"].fillage, features["pump_leak"].fillage)
        self.assertLess(features["rod_break"].area_kn_m, features["empty_pump"].area_kn_m)
        ratios = [leak["power"] / normal["power"] for normal, leak in zip(
            cards["normal"]["electric_samples"], cards["pump_leak"]["electric_samples"]
        ) if abs(normal["power"]) > 1.0]
        self.assertGreater(max(ratios) - min(ratios), .2)

    def test_current_matches_signed_active_power_magnitude(self):
        """A fixed current-to-power multiplier gives electrically inconsistent samples."""
        for sample in build_profile_card("normal", MECHANISM)["electric_samples"]:
            self.assertGreaterEqual(sample["current"], 0.0)
            self.assertAlmostEqual(abs(sample["power"]), math.sqrt(3) * sample["voltage"] * sample["current"] * .88 / 1000, places=5)

    def test_allocation_and_variation_are_stable_without_global_random_state(self):
        """Random profile allocation would make refreshes change the assigned condition."""
        self.assertEqual([profile_condition(f"D-{index:03d}") for index in range(1, 21)], list(PROFILE_CODES))
        self.assertEqual(profile_condition("NS-D-001"), "normal")
        self.assertEqual(profile_condition("controller-west"), profile_condition("controller-west"))
        before = random.getstate()
        baseline = build_profile_card("pump_leak", MECHANISM, variation=.3)
        self.assertEqual(baseline, build_profile_card("pump_leak", MECHANISM, variation=.3))
        self.assertNotEqual(baseline["electric_samples"], build_profile_card("pump_leak", MECHANISM, variation=.8)["electric_samples"])
        self.assertEqual(random.getstate(), before)
        self.assertEqual(MECHANISM, {"stroke_m": 2.8, "strokes_per_minute": 4.2})

    def test_custom_sampling_and_calibration_remain_coherent(self):
        mechanism = {**MECHANISM, "stroke_m": 3.4, "static_rod_load_kn": 40.0,
                     "full_load_span_kn": 30.0, "balance_torque_nm": 600.0,
                     "balance_phase_deg": 20.0, "gear_ratio": 45.0,
                     "transmission_efficiency": .8, "motor_efficiency": .89}
        card = build_profile_card("normal", mechanism, point_count=240)
        self.assertEqual(len(card["electric_samples"]), 240)
        result = convert(card)
        self.assertGreater(result.features.max_load_kn, 65.0)
        self.assertLess(result.features.max_load_kn, 75.0)
        self.assertGreater(result.features.min_load_kn, 38.0)
        self.assertGreater(result.energy.conversion_ratio, .9)
