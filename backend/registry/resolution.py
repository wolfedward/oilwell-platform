"""Observational checks against historical context, never a control authorization."""


def assess(sample: dict, context: dict) -> dict:
    reasons = []
    if not context["registered"]:
        reasons.append("not_registered")
    else:
        if not context["bound"]:
            reasons.append("unbound")
        if context["state"] != "active":
            reasons.append("state_not_active")
        if context["map_version"] is None:
            reasons.append("no_expected_map")
        elif context["point"] is None:
            reasons.append("unknown_point")
        else:
            point = context["point"]
            if sample["unit"] != point["unit"]:
                reasons.append("unit_mismatch")
            low, high = point.get("engineering_min"), point.get("engineering_max")
            if (low is not None and sample["value"] < low) or (high is not None and sample["value"] > high):
                reasons.append("out_of_range")
    if not context["clock_valid"]:
        reasons.append("clock_anomaly")
    if sample["quality"] in {"bad", "uncertain"}:
        reasons.append("quality_" + sample["quality"])
    return sample | {"reasons": reasons, "matched": not reasons,
                     "expected_map_version": str(context["map_version"]) if context["map_version"] is not None else None,
                     "reported_map_version": None, "application_status": "unverified", "eligible_for_automation": False}
