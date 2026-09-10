"""Hybrid rule/template diagnosis tests."""

from dataclasses import replace
import math
import unittest

from backend.algorithms.diagnosis import (
    DiagnosisValidationError,
    Template,
    diagnose,
)
from backend.algorithms.dynagraph import DynagraphFeatures, convert_electric_card
from tests.test_dynagraph import fixture


def features_for(condition: str) -> DynagraphFeatures:
    if condition == "intermittent":
        normal = features_for("normal")
        return replace(normal, fillage=normal.fillage * .78, closure_error=.14, turning_points=11)
    return convert_electric_card(*fixture(condition)).features


def templates() -> list[Template]:
    return [
        Template(condition=name, features=features_for(name), sample_count=24)
        for name in ("normal", "supply_shortage", "intermittent", "pump_leak", "empty_pump")
    ]


class DiagnosisTests(unittest.TestCase):
    def test_known_cases_return_reason_confidence_and_ranked_candidates(self):
        for condition in ("normal", "supply_shortage", "intermittent", "pump_leak", "empty_pump"):
            with self.subTest(condition=condition):
                result = diagnose(features_for(condition), templates())
                self.assertEqual(result.condition, condition)
                self.assertGreaterEqual(result.confidence, .75)
                self.assertGreater(len(result.rule_hits), 0)
                self.assertEqual(len(result.candidates), 5)
                self.assertEqual(result.candidates[0].condition, condition)
                self.assertEqual(result.model_version, "diagnosis-1.0")
                self.assertEqual(result.selected_template_version, "1")
                self.assertEqual(result.candidates[0].template_version, "1")
                self.assertEqual(result.rule_baseline_version, "1")
                feature_names = set(DynagraphFeatures.__dataclass_fields__)
                self.assertEqual(set(dict(result.metric_scales)), feature_names)
                self.assertEqual(set(dict(result.metric_weights)), feature_names)
                self.assertTrue(result.evidence)
                self.assertTrue(result.recommendation)

    def test_noisy_and_perturbed_cards_remain_stable(self):
        noisy = diagnose(features_for("noisy_normal"), templates())
        self.assertEqual(noisy.condition, "normal")
        leaking = replace(
            features_for("pump_leak"),
            area_kn_m=features_for("pump_leak").area_kn_m * 1.03,
            fillage=features_for("pump_leak").fillage + .01,
        )
        self.assertEqual(diagnose(leaking, templates()).condition, "pump_leak")

    def test_supply_shortage_survives_single_sample_five_percent_drift(self):
        samples, params = fixture("supply_shortage")
        for index in range(len(samples)):
            for factor in (.95, 1.05):
                changed = list(samples)
                changed[index] = replace(
                    changed[index],
                    active_power_kw=changed[index].active_power_kw * factor,
                    current_a=changed[index].current_a * factor,
                )
                with self.subTest(index=index, factor=factor):
                    measured = convert_electric_card(changed, params).features
                    self.assertEqual(diagnose(measured, templates()).condition, "supply_shortage")

    def test_rule_support_is_continuous_at_the_supply_tilt_threshold(self):
        normal = features_for("normal")
        base = features_for("supply_shortage")
        below = replace(base, top_edge_tilt_kn_per_m=normal.top_edge_tilt_kn_per_m - .15 - 1e-8)
        above = replace(base, top_edge_tilt_kn_per_m=normal.top_edge_tilt_kn_per_m - .15 + 1e-8)
        first, second = diagnose(below, templates()), diagnose(above, templates())
        self.assertEqual((first.condition, second.condition), ("supply_shortage", "supply_shortage"))
        self.assertLess(abs(first.confidence - second.confidence), 1e-6)
        self.assertLess(abs(first.candidates[0].rule_score - second.candidates[0].rule_score), 1e-6)

    def test_rule_evidence_exposes_observed_values_and_thresholds(self):
        result = diagnose(features_for("pump_leak"), templates())
        codes = {hit.code for hit in result.rule_hits}
        self.assertTrue({"load_drop", "top_edge_tilt", "low_fillage"} & codes)
        for hit in result.rule_hits:
            self.assertTrue(math.isfinite(hit.observed))
            self.assertTrue(math.isfinite(hit.threshold))
            self.assertTrue(hit.description)
        self.assertTrue(set(result.rule_hits).issubset(set(result.all_rule_hits)))
        self.assertGreaterEqual(len({hit.condition for hit in result.all_rule_hits}), 2)

    def test_template_distance_is_normalized_and_candidates_are_ranked(self):
        result = diagnose(features_for("supply_shortage"), templates())
        scores = [candidate.combined_score for candidate in result.candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertAlmostEqual(result.candidates[0].distance, 0.0, places=12)
        self.assertTrue(all(0.0 <= candidate.template_score <= 1.0 for candidate in result.candidates))

    def test_input_and_template_validation_rejects_unsafe_models(self):
        base = features_for("normal")
        invalid_calls = (
            lambda: diagnose(base, []),
            lambda: diagnose(base, [Template("normal", base, sample_count=0)]),
            lambda: diagnose(replace(base, fillage=float("nan")), templates()),
            lambda: diagnose(base, [*templates(), Template("normal", base)]),
            lambda: diagnose(base, [Template("bad condition", base)]),
            lambda: diagnose(base, [Template("pump_leak", base), Template("empty_pump", base)]),
            lambda: diagnose(base, [
                replace(templates()[0], scale_overrides=(("fillage", 1_000_000.0),)),
                *templates()[1:],
            ]),
            lambda: diagnose(base, [
                replace(template, weight_overrides=tuple(
                    (name, 1.0 if name == "fillage" else 0.0)
                    for name in base.__dataclass_fields__
                )) for template in templates()
            ]),
            lambda: diagnose(base, [
                replace(templates()[0], weight_overrides=(("fillage", 0.0),)),
                *templates()[1:],
            ]),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(DiagnosisValidationError):
                    call()

    def test_calls_are_deterministic_and_do_not_mutate_templates(self):
        stored = templates()
        original = list(stored)
        first = diagnose(features_for("normal"), stored)
        second = diagnose(features_for("normal"), stored)
        self.assertEqual(first, second)
        self.assertEqual(stored, original)

    def test_a_single_template_feature_can_be_disabled_without_disabling_model(self):
        stored = [
            replace(template, weight_overrides=(("fillage", 0.0),))
            for template in templates()
        ]
        self.assertEqual(diagnose(features_for("normal"), stored).condition, "normal")


if __name__ == "__main__":
    unittest.main()
