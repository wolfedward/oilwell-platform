"""Strict, bounded data contracts for condition research."""
from datetime import datetime, timezone
import math
import unicodedata

from .contracts import StudyError, digest

VERSION = "condition_bp/1"
LIMIT = 1e12


def fail(message, code="invalid_condition_input"):
    raise StudyError(code, message)


def exact(value, keys, name):
    if type(value) is not dict or set(value) != set(keys):
        fail(f"{name} has missing or unknown fields")


def sequence(value, lo, hi, name):
    if type(value) is not list or not lo <= len(value) <= hi:
        fail(f"{name} count must be {lo}..{hi}")


def string(value, name, maximum, *, forbidden=()):
    if (type(value) is not str or not value or len(value) > maximum
            or not value.strip() or any(unicodedata.category(c) == "Cc" for c in value)
            or value.lower() in forbidden):
        fail(f"invalid {name}")
    return value


def number(value, name, lo=-LIMIT, hi=LIMIT):
    if type(value) not in (int, float) or not lo <= value <= hi or not math.isfinite(value):
        fail(f"invalid finite {name}")
    return value


def integer(value, name, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        fail(f"invalid {name}")
    return value


def instant(value):
    string(value, "observed_at", 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail("observed_at must be ISO timestamp with timezone")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        fail("observed_at must include timezone")
    return parsed.astimezone(timezone.utc)


def features(value):
    sequence(value, 1, 16, "features")
    names = set()
    forbidden = {"well", "well_id", "cycle", "cycle_id", "label", "id", "sample_id",
                 "time", "timestamp", "observed_at", "partition"}
    for item in value:
        exact(item, ("name", "unit"), "feature")
        name = string(item["name"], "feature.name", 64, forbidden=forbidden)
        string(item["unit"], "feature.unit", 24)
        if name in names:
            fail("duplicate feature name")
        names.add(name)
    return names


def row_values(row, count):
    sequence(row["values"], count, count, "values")
    for value in row["values"]:
        number(value, "values")


def validate_rows(value, count, *, training):
    sequence(value, 18 if training else 1, 2048, "rows")
    keys = ("sample_id", "well_id", "cycle_id", "observed_at", "partition", "label", "values") if training else (
        "sample_id", "well_id", "cycle_id", "observed_at", "values")
    samples, cycles = set(), set()
    for row in value:
        exact(row, keys, "row")
        for name in ("sample_id", "well_id", "cycle_id"):
            string(row[name], name, 128)
        if row["sample_id"] in samples or row["cycle_id"] in cycles:
            fail("duplicate sample_id or cycle_id across rows")
        samples.add(row["sample_id"])
        cycles.add(row["cycle_id"])
        instant(row["observed_at"])
        row_values(row, count)
        if training:
            if row["partition"] not in ("train", "validation", "test"):
                fail("invalid partition")
            string(row["label"], "label", 64)


def validate_training(payload):
    exact(payload, ("schema", "features", "rows", "rules", "options"), "training payload")
    if payload["schema"] != "condition.training/1":
        fail("unsupported training schema")
    names = features(payload["features"])
    validate_rows(payload["rows"], len(names), training=True)
    partitions = {key: [row for row in payload["rows"] if row["partition"] == key]
                  for key in ("train", "validation", "test")}
    if any(len(rows) < 6 for rows in partitions.values()):
        fail("each partition needs at least six rows")
    wells = [{r["well_id"] for r in rows} for rows in partitions.values()]
    if any(wells[i] & wells[j] for i, j in ((0, 1), (0, 2), (1, 2))):
        fail("well_id leakage between partitions")
    time_sets = [[instant(r["observed_at"]) for r in rows] for rows in partitions.values()]
    if not max(time_sets[0]) < min(time_sets[1]) or not max(time_sets[1]) < min(time_sets[2]):
        fail("partition UTC time ranges must be strictly ordered")
    labels = sorted({r["label"] for r in partitions["train"]})
    if not 2 <= len(labels) <= 32 or "unknown" in labels:
        fail("train needs 2..32 labels")
    for label in labels:
        if sum(r["label"] == label for r in partitions["train"]) < 2:
            fail("each train class needs at least two rows")
    if any({r["label"] for r in rows} != set(labels) for rows in partitions.values()):
        fail("validation and test must cover exactly train classes")
    rules = payload["rules"]
    sequence(rules, 1, 32, "rules")
    for rule in rules:
        exact(rule, ("label", "clauses"), "rule")
        if rule["label"] not in labels:
            fail("rule label must be a training label")
        sequence(rule["clauses"], 1, 8, "clauses")
        for clause in rule["clauses"]:
            exact(clause, ("feature", "op", "value"), "clause")
            if (type(clause["feature"]) is not str or clause["feature"] not in names
                    or type(clause["op"]) is not str or clause["op"] not in ("le", "ge")):
                fail("invalid rule clause")
            number(clause["value"], "rule value")
    opts = payload["options"]
    exact(opts, ("seed", "hidden_units", "epochs", "learning_rate", "l2",
                 "confidence_min", "ood_margin"), "options")
    integer(opts["seed"], "seed", 0, 2**32 - 1)
    integer(opts["hidden_units"], "hidden_units", 1, 32)
    integer(opts["epochs"], "epochs", 1, 300)
    number(opts["learning_rate"], "learning_rate", 0, 1)
    if opts["learning_rate"] == 0:
        fail("learning_rate must be positive")
    number(opts["l2"], "l2", 0, 1)
    number(opts["confidence_min"], "confidence_min", .5, .999)
    number(opts["ood_margin"], "ood_margin", 0, 2)
    n, f, h, e, c = len(partitions["train"]), len(names), opts["hidden_units"], opts["epochs"], len(labels)
    if n * f * h * e * (1 + c / f) > 20_000_000:
        fail("training operation budget exceeded", "resource_limit")
    return partitions, labels


def partition_summary(partitions, labels):
    result = {}
    for name, rows in partitions.items():
        times = [instant(row["observed_at"]).isoformat() for row in rows]
        result[name] = {"well_count": len({r["well_id"] for r in rows}),
                        "sample_count": len(rows), "time_start_utc": min(times),
                        "time_end_utc": max(times),
                        "class_counts": {label: sum(r["label"] == label for r in rows) for label in labels},
                        "data_digest": digest(rows)}
    return result


def scaler(rows, count):
    columns = [[r["values"][i] for r in rows] for i in range(count)]
    means = [math.fsum(col) / len(col) for col in columns]
    std = [math.sqrt(math.fsum(((v - m) ** 2 for v in col)) / len(col))
           for col, m in zip(columns, means)]
    result = {"mean": means, "std": std, "min": [min(col) for col in columns],
              "max": [max(col) for col in columns], "constant": [s == 0 for s in std]}
    if any(not math.isfinite(v) for part in ("mean", "std", "min", "max") for v in result[part]):
        fail("nonfinite training scaler", "nonconvergence")
    return result


def scaled(values, fitted):
    result = [0.0 if constant else (v - mean) / std
              for v, mean, std, constant in zip(values, fitted["mean"], fitted["std"], fitted["constant"])]
    if any(not math.isfinite(v) or abs(v) > 1e6 for v in result):
        fail("scaled value outside finite model bound", "numeric_limit")
    return result


def ood_features(values, fitted, feature_names, margin):
    outside = []
    for value, lo, hi, mean, name in zip(values, fitted["min"], fitted["max"], fitted["mean"], feature_names):
        extension = margin * max(hi - lo, abs(mean) * 1e-12, 1e-12)
        if value < lo - extension or value > hi + extension:
            outside.append(name)
    return outside
