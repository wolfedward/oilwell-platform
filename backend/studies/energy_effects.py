"""Descriptive comparison of measured energy and liquid intervals only."""
from .contracts import digest, fields
from .recovery import _fail, _number, _text, _time

VERSION = "energy_effects/1"
COMPARABILITY = ("same_meter", "consistent_units", "similar_operating_conditions",
                 "other_changes_recorded")


def _period(period, name):
    fields(period, ("start", "end", "intervals"))
    start, end = _time(period["start"], f"{name}.start"), _time(period["end"], f"{name}.end")
    if end <= start:
        _fail(f"{name}.period")
    intervals = period["intervals"]
    if type(intervals) is not list or len(intervals) > 512:
        _fail(f"{name}.intervals")
    bounds = []
    valid_hours = liquid = energy = 0.0
    counts = {"valid": 0, "invalid": 0, "unknown": 0}
    for row in intervals:
        fields(row, ("start", "end", "liquid_m3", "energy_kwh", "quality"))
        left, right = _time(row["start"], "interval.start"), _time(row["end"], "interval.end")
        if left < start or right <= left or right > end:
            _fail(f"{name}.interval_bounds")
        bounds.append((left, right))
        measured_liquid = _number(row["liquid_m3"], "liquid_m3", 0, 1e12)
        measured_energy = _number(row["energy_kwh"], "energy_kwh", 0, 1e12)
        if row["quality"] not in counts:
            _fail("quality")
        counts[row["quality"]] += 1
        if row["quality"] == "valid":
            valid_hours += (right - left).total_seconds() / 3600
            liquid += measured_liquid
            energy += measured_energy
    last = start
    for left, right in sorted(bounds):
        if left < last:
            _fail(f"{name}.overlapping_intervals")
        last = right
    duration = (end - start).total_seconds() / 3600
    return (start, end, {"duration_hours": duration, "observed_hours": valid_hours,
                         "coverage_fraction": valid_hours / duration,
                         "liquid_m3": liquid if valid_hours else None,
                         "energy_kwh": energy if valid_hours else None,
                         "liquid_m3_per_observed_hour": liquid / valid_hours if valid_hours else None,
                         "energy_kwh_per_observed_hour": energy / valid_hours if valid_hours else None,
                         "interval_counts": counts})


def evaluate_effects(payload):
    fields(payload, ("schema", "well_id", "baseline", "treatment",
                     "min_coverage_fraction", "comparability"))
    if payload["schema"] != "energy.effects/1":
        _fail("schema")
    well_id = _text(payload["well_id"], "well_id", 128)
    min_coverage = _number(payload["min_coverage_fraction"], "min_coverage_fraction", 0, 1,
                           lower_open=True)
    comparison = payload["comparability"]
    fields(comparison, COMPARABILITY)
    if any(type(comparison[key]) is not bool for key in COMPARABILITY):
        _fail("comparability")
    base_start, base_end, baseline = _period(payload["baseline"], "baseline")
    treatment_start, treatment_end, treatment = _period(payload["treatment"], "treatment")
    if base_start < treatment_end and treatment_start < base_end:
        _fail("overlapping_periods")
    reasons = []
    if baseline["coverage_fraction"] < min_coverage or treatment["coverage_fraction"] < min_coverage:
        reasons.append("insufficient_coverage")
    if not all(comparison.values()):
        reasons.append("comparability_not_declared")
    def difference(key):
        a, b = baseline[key], treatment[key]
        return b - a if a is not None and b is not None else None
    return {"status": "insufficient_evidence" if reasons else "descriptive",
            "algorithm_version": VERSION, "well_id": well_id, "input_digest": digest(payload),
            "baseline": baseline, "treatment": treatment,
            "comparison": {"liquid_m3_per_observed_hour_difference": difference("liquid_m3_per_observed_hour"),
                           "energy_kwh_per_observed_hour_difference": difference("energy_kwh_per_observed_hour")},
            "reason_codes": reasons, "comparability_declarations": comparison.copy(),
            "actual_energy_saving_kwh": None, "causal_effect_proven": False,
            "shadow_only": True, "dispatch_allowed": False, "field_validation": False,
            "assumptions": ["Only valid observed intervals are summed; uncovered time is not imputed.",
                            "Observed rate differences are descriptive and do not establish causality."]}
