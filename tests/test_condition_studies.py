"""Contract tests for deterministic, source-separated condition research."""
import copy
import math
import unittest

from backend.studies.contracts import StudyError
from backend.studies.condition_models import train_conditions, predict_conditions, _centroid
from backend.studies.condition_evaluation import evaluate_predictions


def example():
    rows = []
    for partition, day in (("train", 1), ("validation", 2), ("test", 3)):
        for index, (label, x, y) in enumerate((("A", -2, 0), ("A", -1.8, .1),
                                              ("B", 2, 0), ("B", 1.8, -.1),
                                              ("C", 0, 2), ("C", .1, 1.8))):
            rows.append({"sample_id": f"{partition}-{index}",
                         "well_id": f"{partition}-well-{index // 2}",
                         "cycle_id": f"{partition}-cycle-{index}",
                         "observed_at": f"2026-01-0{day}T00:0{index}:00+00:00",
                         "partition": partition, "label": label,
                         "values": [x + (day - 1) * .02, y + (day - 1) * .02, 10]})
    return {"schema": "condition.training/1",
            "features": [{"name": "x", "unit": "m"}, {"name": "y", "unit": "m"},
                         {"name": "constant", "unit": "m"}],
            "rows": rows,
            "rules": [{"label": "A", "clauses": [{"feature": "x", "op": "le", "value": -1}]},
                      {"label": "B", "clauses": [{"feature": "x", "op": "ge", "value": 1}]},
                      {"label": "C", "clauses": [{"feature": "y", "op": "ge", "value": 1}]}],
            "options": {"seed": 7, "hidden_units": 5, "epochs": 80,
                        "learning_rate": .2, "l2": 0.001,
                        "confidence_min": .5, "ood_margin": .2}}


class ConditionStudiesTests(unittest.TestCase):
    def test_training_learns_and_reports_held_out_metrics(self):
        result = train_conditions(example())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["algorithm_version"], "condition_bp/1")
        self.assertEqual(len(result["metrics"]["bp"]["predictions"]), 6)
        self.assertEqual(result["partitions"]["test"]["sample_count"], 6)
        self.assertEqual(result["artifact"]["scaler"]["constant"], [False, False, True])
        self.assertLess(min(x["train_loss"] for x in result["training_curve"][1:]),
                        result["training_curve"][0]["train_loss"])
        self.assertTrue(all(math.isfinite(v) for row in result["artifact"]["weights_input"] for v in row))

    def test_same_seed_same_result_and_test_labels_never_choose_weights(self):
        payload = example()
        first = train_conditions(payload)
        self.assertEqual(first, train_conditions(copy.deepcopy(payload)))
        changed = copy.deepcopy(payload)
        for row in changed["rows"]:
            if row["partition"] == "test":
                row["label"] = {"A": "B", "B": "C", "C": "A"}[row["label"]]
        second = train_conditions(changed)
        self.assertEqual(first["artifact"], second["artifact"])
        self.assertNotEqual(first["metrics"]["bp"]["confusion_matrix"],
                            second["metrics"]["bp"]["confusion_matrix"])

    def test_scaler_uses_train_only(self):
        payload = example()
        initial = train_conditions(payload)["artifact"]["scaler"]
        payload["rows"][-1]["values"][0] = 1_000_000
        self.assertEqual(train_conditions(payload)["artifact"]["scaler"], initial)

    def test_split_leakage_refused(self):
        for edit in (lambda p: p["rows"][6].update(well_id=p["rows"][0]["well_id"]),
                     lambda p: p["rows"][6].update(cycle_id=p["rows"][0]["cycle_id"]),
                     lambda p: p["rows"][6].update(sample_id=p["rows"][0]["sample_id"]),
                     lambda p: p["rows"][6].update(observed_at=p["rows"][0]["observed_at"]),
                     lambda p: p["rows"][6].update(label="Z")):
            with self.subTest(edit=edit):
                payload = example()
                edit(payload)
                with self.assertRaises(StudyError):
                    train_conditions(payload)

    def test_invalid_values_keys_feature_and_budget_refused(self):
        edits = (lambda p: p["rows"][0]["values"].__setitem__(0, True),
                 lambda p: p["rows"][0]["values"].__setitem__(0, float("nan")),
                 lambda p: p["rows"][0].update(extra=1),
                 lambda p: p["features"][0].update(name="well_id"))
        for edit in edits:
            payload = example()
            edit(payload)
            with self.assertRaises(StudyError):
                train_conditions(payload)

    def test_operation_budget_rejected_before_training(self):
        payload = example()
        source = payload["rows"][0]
        for i in range(700):
            duplicate = copy.deepcopy(source)
            duplicate.update(sample_id=f"extra-{i}", cycle_id=f"extra-cycle-{i}",
                             well_id=f"extra-well-{i}")
            payload["rows"].append(duplicate)
        payload["options"].update(hidden_units=32, epochs=300)
        with self.assertRaises(StudyError) as found:
            train_conditions(payload)
        self.assertEqual(found.exception.code, "resource_limit")

    def test_hand_checked_confusion_and_coverage(self):
        rows = [{"sample_id": str(i), "label": truth} for i, truth in enumerate(("A", "A", "B", "B", "C", "C"))]
        guesses = [("A", None), ("unknown", "low_confidence"), ("A", None),
                   ("B", None), ("C", None), ("unknown", "ood")]
        actual = evaluate_predictions(rows, guesses, ["A", "B", "C"])
        self.assertEqual(actual["confusion_matrix"], [[1, 0, 0, 1], [1, 1, 0, 0], [0, 0, 1, 1]])
        self.assertEqual(actual["coverage"], 4 / 6)
        self.assertEqual(actual["accuracy"], .5)
        self.assertEqual(actual["accepted_accuracy"], .75)
        self.assertAlmostEqual(actual["macro_F1"], (0.5 + 2/3 + 2/3) / 3)

    def test_prediction_validates_artifact_and_rejects_ood(self):
        result = train_conditions(example())
        row = {k: v for k, v in example()["rows"][0].items() if k not in ("partition", "label")}
        row["sample_id"] = "new-id"
        row["cycle_id"] = "new-cycle"
        payload = {"schema": "condition.prediction/1", "model_record_id": "approved-1",
                   "artifact": result["artifact"], "rows": [row],
                   "confidence_min": .5, "ood_margin": .2}
        out = predict_conditions(payload)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["field_validation"], False)
        self.assertNotIn("accuracy", out)
        self.assertEqual(out["predictions"][0]["score_kind"], "uncalibrated_model_score")
        far = copy.deepcopy(payload)
        far["rows"][0]["values"][0] = 10_000
        self.assertEqual(predict_conditions(far)["predictions"][0]["label"], "unknown")
        bad = copy.deepcopy(payload)
        bad["artifact"]["weights_input"][0].append(1)
        with self.assertRaises(StudyError):
            predict_conditions(bad)

        low = copy.deepcopy(payload)
        for key in ("weights_input", "weights_output"):
            low["artifact"][key] = [[0.0 for _ in line] for line in low["artifact"][key]]
        for key in ("bias_hidden", "bias_output"):
            low["artifact"][key] = [0.0 for _ in low["artifact"][key]]
        prediction = predict_conditions(low)["predictions"][0]
        self.assertEqual(prediction["label"], "unknown")
        self.assertEqual(prediction["reasons"], ["low_confidence"])
        self.assertAlmostEqual(prediction["score"], 1 / 3)

    def test_rules_conflict_and_drift_change_only_held_out_evaluation(self):
        original = example()
        base = train_conditions(original)
        shifted = copy.deepcopy(original)
        shifted["rows"][-1]["values"] = [1.8, 1.8, 10]
        result = train_conditions(shifted)
        self.assertEqual(base["artifact"], result["artifact"])
        self.assertEqual(result["metrics"]["rule"]["predictions"][-1]["reason"], "rule_conflict")
        self.assertEqual(result["metrics"]["rule"]["predictions"][-1]["label"], "unknown")

    def test_artifact_rejects_unknown_fields_nonfinite_and_false_scaler(self):
        artifact = train_conditions(example())["artifact"]
        row = {key: value for key, value in example()["rows"][0].items()
               if key not in ("partition", "label")}
        base = {"schema": "condition.prediction/1", "model_record_id": "approved-1",
                "artifact": artifact, "rows": [row], "confidence_min": .5, "ood_margin": .2}
        edits = (lambda a: a.update(source_scope="field_validated"),
                 lambda a: a.update(execute="import os"),
                 lambda a: a["weights_input"][0].__setitem__(0, float("inf")),
                 lambda a: a["scaler"]["constant"].__setitem__(0, True),
                 lambda a: a["weights_output"][0].__setitem__(0, 1e9))
        for edit in edits:
            candidate = copy.deepcopy(base)
            edit(candidate["artifact"])
            with self.assertRaises(StudyError):
                predict_conditions(candidate)

    def test_malformed_rule_feature_is_study_error(self):
        payload = example()
        payload["rules"][0]["clauses"][0]["feature"] = ["x"]
        with self.assertRaises(StudyError):
            train_conditions(payload)

    def test_unstable_feature_scale_refuses_finite_outlier(self):
        payload = example()
        for index, row in enumerate(payload["rows"][:6]):
            row["values"][0] = index * 1e-9
        with self.assertRaises(StudyError) as found:
            train_conditions(payload)
        self.assertEqual(found.exception.code, "numeric_limit")

    def test_huge_integer_is_study_error_in_training_and_prediction(self):
        training = example()
        training["rows"][0]["values"][0] = 10**400
        with self.assertRaises(StudyError):
            train_conditions(training)

        artifact = train_conditions(example())["artifact"]
        row = {key: value for key, value in example()["rows"][0].items()
               if key not in ("partition", "label")}
        row["values"][0] = -(10**400)
        prediction = {"schema": "condition.prediction/1", "model_record_id": "approved-1",
                      "artifact": artifact, "rows": [row], "confidence_min": .5, "ood_margin": .2}
        with self.assertRaises(StudyError):
            predict_conditions(prediction)

    def test_no_accepted_predictions_has_null_accuracy(self):
        rows = [{"sample_id": "one", "label": "A"}, {"sample_id": "two", "label": "B"}]
        report = evaluate_predictions(rows, [("unknown", "ood"), ("unknown", "ood")], ["A", "B"])
        self.assertEqual(report["coverage"], 0)
        self.assertIsNone(report["accepted_accuracy"])

    def test_near_equal_centroid_tie_uses_lexical_label(self):
        fitted = {"mean": [0.0], "std": [1.0], "min": [-1.0], "max": [1.0],
                  "constant": [False]}
        prediction = _centroid([0.0], {"A": [1 + 1e-13], "B": [1.0]}, fitted, [])
        self.assertEqual(prediction, ("A", "centroid_tie"))

    def test_inputs_unchanged(self):
        payload = example()
        original = copy.deepcopy(payload)
        result = train_conditions(payload)
        self.assertEqual(payload, original)
        result["artifact"]["features"][0]["name"] = "changed"
        self.assertEqual(payload, original)

    def test_unknown_cannot_be_a_training_class(self):
        payload = example()
        for row in payload["rows"]:
            if row["label"] == "C":
                row["label"] = "unknown"
        payload["rules"][-1]["label"] = "unknown"
        with self.assertRaises(StudyError):
            train_conditions(payload)

    def test_nonlinear_xor_learning_uses_backprop(self):
        payload = example()
        payload["features"] = [{"name": "x", "unit": "1"}, {"name": "y", "unit": "1"}]
        payload["rules"] = [{"label": "A", "clauses": [{"feature": "x", "op": "le", "value": -2}] }]
        payload["options"].update(hidden_units=8, epochs=300, learning_rate=.8, l2=0.0)
        payload["rows"] = []
        corners = (("A", -1, -1), ("B", -1, 1), ("B", 1, -1), ("A", 1, 1))
        for partition, day, repeats in (("train", 1, 4), ("validation", 2, 2), ("test", 3, 2)):
            for repeat in range(repeats):
                for index, (label, x, y) in enumerate(corners):
                    n = repeat * 4 + index
                    payload["rows"].append({"sample_id": f"{partition}-{n}",
                                            "well_id": f"{partition}-well-{n}",
                                            "cycle_id": f"{partition}-cycle-{n}",
                                            "observed_at": f"2026-01-0{day}T00:{n:02d}:00Z",
                                            "partition": partition, "label": label,
                                            "values": [x, y]})
        fitted = train_conditions(payload)
        self.assertLess(fitted["training_curve"][-1]["train_loss"],
                        fitted["training_curve"][0]["train_loss"] * .5)
        self.assertGreaterEqual(fitted["metrics"]["bp"]["accuracy"], .75)


if __name__ == "__main__":
    unittest.main()
