"""Deterministic bounded BP model and two declared condition baselines."""
import math
import random
import re
import copy

from .contracts import digest
from .condition_data import (VERSION, exact, fail, features, integer, number, ood_features,
                             partition_summary, scaled, scaler, sequence, string,
                             validate_rows, validate_training)
from .condition_evaluation import evaluate_predictions

WEIGHT_LIMIT = 1e6


def _finite(values, what):
    if any(not math.isfinite(v) or abs(v) > WEIGHT_LIMIT for v in values):
        fail(f"nonfinite or unbounded {what}", "nonconvergence")


def _forward(x, wi, bh, wo, bo):
    hidden = [math.tanh(math.fsum(x[i] * wi[i][j] for i in range(len(x))) + bh[j])
              for j in range(len(bh))]
    logits = [math.fsum(hidden[j] * wo[j][k] for j in range(len(hidden))) + bo[k]
              for k in range(len(bo))]
    _finite(logits, "network output")
    top = max(logits)
    exp = [math.exp(v - top) for v in logits]
    denominator = math.fsum(exp)
    probabilities = [v / denominator for v in exp]
    return hidden, probabilities, top + math.log(denominator), logits


def _loss(rows, labels, fitted, wi, bh, wo, bo, l2):
    total = 0.0
    for row in rows:
        _, _, logsum, logits = _forward(scaled(row["values"], fitted), wi, bh, wo, bo)
        total += logsum - logits[labels.index(row["label"])]
    penalty = math.fsum(v * v for line in wi + wo for v in line)
    value = total / len(rows) + .5 * l2 * penalty
    _finite((value,), "loss")
    return value


def _fit(train, validation, labels, fitted, options):
    f, h, c = len(fitted["mean"]), options["hidden_units"], len(labels)
    rng = random.Random(options["seed"])
    wi = [[rng.uniform(-.5, .5) / math.sqrt(f) for _ in range(h)] for _ in range(f)]
    bh = [0.0] * h
    wo = [[rng.uniform(-.5, .5) / math.sqrt(h) for _ in range(c)] for _ in range(h)]
    bo = [0.0] * c
    best, best_loss, best_epoch, curve = None, math.inf, 0, []
    for epoch in range(1, options["epochs"] + 1):
        gi = [[0.0] * h for _ in range(f)]
        gh = [0.0] * h
        go = [[0.0] * c for _ in range(h)]
        gb = [0.0] * c
        for row in train:
            x = scaled(row["values"], fitted)
            hidden, probabilities, _, _ = _forward(x, wi, bh, wo, bo)
            delta = probabilities[:]
            delta[labels.index(row["label"])] -= 1
            for k in range(c):
                gb[k] += delta[k]
                for j in range(h):
                    go[j][k] += hidden[j] * delta[k]
            for j in range(h):
                back = (1 - hidden[j] * hidden[j]) * math.fsum(wo[j][k] * delta[k] for k in range(c))
                gh[j] += back
                for i in range(f):
                    gi[i][j] += x[i] * back
        rate, n, l2 = options["learning_rate"], len(train), options["l2"]
        for i in range(f):
            for j in range(h):
                wi[i][j] -= rate * (gi[i][j] / n + l2 * wi[i][j])
        for j in range(h):
            bh[j] -= rate * gh[j] / n
            for k in range(c):
                wo[j][k] -= rate * (go[j][k] / n + l2 * wo[j][k])
        for k in range(c):
            bo[k] -= rate * gb[k] / n
        _finite([v for line in wi + wo for v in line] + bh + bo, "trained weights")
        training_loss = _loss(train, labels, fitted, wi, bh, wo, bo, l2)
        validation_loss = _loss(validation, labels, fitted, wi, bh, wo, bo, l2)
        curve.append({"epoch": epoch, "train_loss": training_loss, "validation_loss": validation_loss})
        if validation_loss < best_loss:
            best_loss, best_epoch = validation_loss, epoch
            best = ([line[:] for line in wi], bh[:], [line[:] for line in wo], bo[:])
    return best, curve, best_epoch


def _bp(values, artifact, confidence_min, margin):
    names = [item["name"] for item in artifact["features"]]
    outside = ood_features(values, artifact["scaler"], names, margin)
    if outside:
        return {"label": "unknown", "score": None, "score_kind": "uncalibrated_model_score",
                "ood_features": outside, "reasons": ["ood"]}
    x = scaled(values, artifact["scaler"])
    _, probabilities, _, _ = _forward(x, artifact["weights_input"], artifact["bias_hidden"],
                                      artifact["weights_output"], artifact["bias_output"])
    index = max(range(len(probabilities)), key=lambda k: probabilities[k])
    score = probabilities[index]
    return {"label": artifact["labels"][index] if score >= confidence_min else "unknown",
            "score": score, "score_kind": "uncalibrated_model_score", "ood_features": [],
            "reasons": [] if score >= confidence_min else ["low_confidence"]}


def _rule(values, rules, feature_names, outside):
    if outside:
        return "unknown", "ood"
    by_name = dict(zip(feature_names, values))
    hits = set()
    for rule in rules:
        if all(by_name[clause["feature"]] <= clause["value"] if clause["op"] == "le"
               else by_name[clause["feature"]] >= clause["value"] for clause in rule["clauses"]):
            hits.add(rule["label"])
    if len(hits) == 1:
        return next(iter(hits)), None
    return "unknown", "rule_conflict" if hits else "no_rule_match"


def _centroid(values, centroids, fitted, outside):
    if outside:
        return "unknown", "ood"
    x = scaled(values, fitted)
    distances = [(math.fsum((a - b) ** 2 for a, b in zip(x, center)), label)
                 for label, center in centroids.items()]
    nearest = min(distance for distance, _ in distances)
    tied_labels = [label for distance, label in distances
                   if math.isclose(distance, nearest, rel_tol=1e-12, abs_tol=1e-12)]
    return min(tied_labels), "centroid_tie" if len(tied_labels) > 1 else None


def train_conditions(payload: dict) -> dict:
    partitions, labels = validate_training(payload)
    train, validation, test = (partitions[p] for p in ("train", "validation", "test"))
    fitted = scaler(train, len(payload["features"]))
    (wi, bh, wo, bo), curve, best_epoch = _fit(train, validation, labels, fitted, payload["options"])
    training_digest = digest({"features": payload["features"], "train": train,
                              "validation": validation, "options": payload["options"]})
    artifact = {"schema": "condition.artifact/1", "algorithm_version": VERSION,
                "features": copy.deepcopy(payload["features"]), "labels": labels, "scaler": fitted,
                "hidden_units": payload["options"]["hidden_units"],
                "weights_input": wi, "bias_hidden": bh,
                "weights_output": wo, "bias_output": bo,
                "training_digest": training_digest, "source_scope": "research_only"}
    names = [f["name"] for f in payload["features"]]
    centers = {label: [math.fsum(scaled(r["values"], fitted)[i] for r in train if r["label"] == label)
                       / sum(r["label"] == label for r in train) for i in range(len(names))]
               for label in labels}
    guesses = {"rule": [], "centroid": [], "bp": []}
    for row in test:
        outside = ood_features(row["values"], fitted, names, payload["options"]["ood_margin"])
        guesses["rule"].append(_rule(row["values"], payload["rules"], names, outside))
        guesses["centroid"].append(_centroid(row["values"], centers, fitted, outside))
        bp = _bp(row["values"], artifact, payload["options"]["confidence_min"],
                 payload["options"]["ood_margin"])
        guesses["bp"].append((bp["label"], bp["reasons"][0] if bp["reasons"] else None))
    metrics = {name: evaluate_predictions(test, guessed, labels) for name, guessed in guesses.items()}
    return {"status": "completed", "schema": "condition.training.result/1",
            "algorithm_version": VERSION, "artifact": artifact, "metrics": metrics,
            "partitions": partition_summary(partitions, labels), "training_curve": curve,
            "seed": payload["options"]["seed"], "epochs_executed": len(curve),
            "best_epoch": best_epoch, "stopping_reason": "epoch_limit",
            "data_digest": digest(payload["rows"]),
            "feature_ranges": [{"name": name, "unit": item["unit"], "min": lo, "max": hi}
                               for name, item, lo, hi in zip(names, payload["features"], fitted["min"], fitted["max"])],
            "warnings": ["Research-only software evaluation; model scores are uncalibrated."],
            "field_validation": False, "control_eligible": False}


def validate_artifact(artifact):
    exact(artifact, ("schema", "algorithm_version", "features", "labels", "scaler",
                     "hidden_units", "weights_input", "bias_hidden", "weights_output",
                     "bias_output", "training_digest", "source_scope"), "artifact")
    if artifact["schema"] != "condition.artifact/1" or artifact["algorithm_version"] != VERSION:
        fail("unsupported artifact schema/version")
    if artifact["source_scope"] != "research_only":
        fail("artifact source scope must be research_only")
    f = len(features(artifact["features"]))
    sequence(artifact["labels"], 2, 32, "labels")
    for label in artifact["labels"]:
        string(label, "artifact label", 64)
    if len(set(artifact["labels"])) != len(artifact["labels"]) or "unknown" in artifact["labels"]:
        fail("invalid artifact label set")
    c = len(artifact["labels"])
    h = integer(artifact["hidden_units"], "hidden_units", 1, 32)
    fitted = artifact["scaler"]
    exact(fitted, ("mean", "std", "min", "max", "constant"), "scaler")
    for name in ("mean", "std", "min", "max", "constant"):
        sequence(fitted[name], f, f, f"scaler.{name}")
    for i in range(f):
        for name in ("mean", "std", "min", "max"):
            number(fitted[name][i], f"scaler.{name}")
        if type(fitted["constant"][i]) is not bool:
            fail("invalid scaler.constant")
        if (fitted["min"][i] > fitted["mean"][i] or fitted["mean"][i] > fitted["max"][i]
                or fitted["std"][i] < 0 or fitted["constant"][i] != (fitted["std"][i] == 0)
                or (fitted["constant"][i] and fitted["min"][i] != fitted["max"][i])):
            fail("inconsistent scaler")
    for key, outer, inner in (("weights_input", f, h), ("weights_output", h, c)):
        sequence(artifact[key], outer, outer, key)
        for line in artifact[key]:
            sequence(line, inner, inner, key)
            for value in line:
                number(value, key, -WEIGHT_LIMIT, WEIGHT_LIMIT)
    for key, count in (("bias_hidden", h), ("bias_output", c)):
        sequence(artifact[key], count, count, key)
        for value in artifact[key]:
            number(value, key, -WEIGHT_LIMIT, WEIGHT_LIMIT)
    if type(artifact["training_digest"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", artifact["training_digest"]):
        fail("invalid training_digest")
    return artifact


def predict_conditions(payload: dict) -> dict:
    exact(payload, ("schema", "model_record_id", "artifact", "rows", "confidence_min", "ood_margin"),
          "prediction payload")
    if payload["schema"] != "condition.prediction/1":
        fail("unsupported prediction schema")
    string(payload["model_record_id"], "model_record_id", 128)
    artifact = validate_artifact(payload["artifact"])
    number(payload["confidence_min"], "confidence_min", .5, .999)
    number(payload["ood_margin"], "ood_margin", 0, 2)
    validate_rows(payload["rows"], len(artifact["features"]), training=False)
    predictions = []
    for row in payload["rows"]:
        result = _bp(row["values"], artifact, payload["confidence_min"], payload["ood_margin"])
        predictions.append({"sample_id": row["sample_id"], "well_id": row["well_id"],
                            "cycle_id": row["cycle_id"], **result})
    return {"status": "completed", "schema": "condition.prediction.result/1",
            "algorithm_version": VERSION, "model_record_id": payload["model_record_id"],
            "predictions": predictions, "input_digest": digest(payload["rows"]),
            "artifact_digest": digest(artifact), "field_validation": False,
            "control_eligible": False}
