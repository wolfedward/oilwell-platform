"""Bounded empirical recovery of measured liquid-level depth."""
import datetime as dt
import math

from .contracts import StudyError, digest, fields

VERSION = "recovery_depth/1"
POLICY_KEYS = ("tau_min_minutes", "tau_max_minutes", "tau_grid_points", "min_r_squared",
               "max_rmse_m", "max_gap_minutes", "max_relative_tau_spread",
               "max_equilibrium_spread_m")


def _fail(name):
    raise StudyError("invalid_field", f"invalid {name}")


def _text(value, name, limit):
    if type(value) is not str or not value.strip() or len(value) > limit:
        _fail(name)
    return value


def _number(value, name, minimum, maximum, *, lower_open=False):
    if type(value) not in (int, float):
        _fail(name)
    if value > maximum or (value <= minimum if lower_open else value < minimum):
        _fail(name)
    if not math.isfinite(value):
        _fail(name)
    return float(value)


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(name)
    return value


def _time(value, name):
    if type(value) is not str:
        _fail(name)
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        _fail(name)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(name)
    return parsed.astimezone(dt.timezone.utc)


def _fit(times, depths, taus):
    n = len(times)
    mean_y = math.fsum(depths) / n
    best = None
    for index, tau in enumerate(taus):
        x = [math.exp(-t / tau) for t in times]
        mean_x = math.fsum(x) / n
        centered = [v - mean_x for v in x]
        denom = math.fsum(v * v for v in centered)
        if denom <= 1e-20:
            continue
        amplitude = math.fsum(a * (b - mean_y) for a, b in zip(centered, depths)) / denom
        equilibrium = mean_y - amplitude * mean_x
        predicted = [equilibrium + amplitude * v for v in x]
        residuals = [observed - predicted_value for observed, predicted_value in zip(depths, predicted)]
        sse = math.fsum(v * v for v in residuals)
        if not all(math.isfinite(v) for v in (amplitude, equilibrium, sse)):
            continue
        candidate = (sse, tau, equilibrium, amplitude, index, predicted, residuals)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return best


def _parameters(fit):
    return {"equilibrium_depth_m": fit[2], "initial_depth_m": fit[2] + fit[3],
            "tau_minutes": fit[1]}


def fit_recovery(payload):
    fields(payload, ("schema", "well_id", "datum", "unit", "shutdown_at",
                     "resolution_m", "accuracy_m", "policy", "observations"))
    if payload["schema"] != "recovery.depth/1" or payload["unit"] != "m":
        _fail("schema or unit")
    well_id = _text(payload["well_id"], "well_id", 128)
    _text(payload["datum"], "datum", 256)
    shutdown = _time(payload["shutdown_at"], "shutdown_at")
    resolution = _number(payload["resolution_m"], "resolution_m", 0, 1000, lower_open=True)
    accuracy = None if payload["accuracy_m"] is None else _number(payload["accuracy_m"], "accuracy_m", 0, 1000)
    policy = payload["policy"]
    fields(policy, POLICY_KEYS)
    tau_min = _number(policy["tau_min_minutes"], "tau_min_minutes", 0, 10080, lower_open=True)
    tau_max = _number(policy["tau_max_minutes"], "tau_max_minutes", tau_min, 10080, lower_open=True)
    points = _integer(policy["tau_grid_points"], "tau_grid_points", 16, 256)
    min_r2 = _number(policy["min_r_squared"], "min_r_squared", 0, 1)
    max_rmse = _number(policy["max_rmse_m"], "max_rmse_m", 0, 10000, lower_open=True)
    max_gap = _number(policy["max_gap_minutes"], "max_gap_minutes", 0, 10080, lower_open=True)
    max_tau_spread = _number(policy["max_relative_tau_spread"], "max_relative_tau_spread", 0, 100, lower_open=True)
    max_eq_spread = _number(policy["max_equilibrium_spread_m"], "max_equilibrium_spread_m", 0, 100000, lower_open=True)
    observations = payload["observations"]
    if type(observations) is not list or not 6 <= len(observations) <= 128:
        _fail("observations")
    accepted, excluded, first, last = [], [], None, None
    non_off = False
    for item in observations:
        fields(item, ("at", "depth_m", "quality", "pump_state"))
        at = _time(item["at"], "observation.at")
        if at < shutdown or (last is not None and at <= last):
            _fail("observation order")
        if first is None:
            first = at
        last = at
        depth = _number(item["depth_m"], "depth_m", 0, 100000)
        if item["quality"] not in ("valid", "invalid", "unknown"):
            _fail("quality")
        if item["pump_state"] not in ("off", "on", "unknown"):
            _fail("pump_state")
        if item["pump_state"] != "off":
            non_off = True
        minute = (at - shutdown).total_seconds() / 60
        if item["quality"] == "valid":
            accepted.append((minute, depth, item["at"]))
        else:
            excluded.append({"at": item["at"], "depth_m": depth,
                             "quality": item["quality"], "pump_state": item["pump_state"],
                             "reason": f"{item['quality']}_quality"})
    if (last - first).total_seconds() / 60 > 10080:
        _fail("observation span")
    result = {"status": "rejected", "well_id": well_id, "model_version": VERSION,
              "input_digest": digest(payload), "parameters": None, "support": None,
              "scenarios": [], "fitted_points": [], "metrics": None, "uncertainty": None,
              "accepted_count": len(accepted), "excluded_count": len(excluded),
              "excluded_points": excluded, "reason_codes": [],
              "assumptions": ["Empirical first-order depth model; depth increases downward.",
                              "Policy values are user-supplied software screening thresholds, not field safety limits.",
                              "No dynamic extrapolation or statistical confidence claim."],
              "shadow_only": True, "dispatch_allowed": False, "field_validation": False}
    reasons = result["reason_codes"]
    if non_off:
        reasons.append("pump_not_off")
    if len(accepted) < 6:
        reasons.append("insufficient_valid_points")
    if not accepted or accepted[0][0] != 0:
        reasons.append("missing_shutdown_start")
    if accepted and max((b[0] - a[0] for a, b in zip(accepted, accepted[1:])), default=0) > max_gap:
        reasons.append("excessive_gap")
    if accepted and max(v[1] for v in accepted) - min(v[1] for v in accepted) <= 2 * resolution:
        reasons.append("insufficient_depth_change")
    if len(accepted) < 6:
        result["input_observations"] = observations
        return result
    times = [v[0] for v in accepted]
    depths = [v[1] for v in accepted]
    ratio = tau_max / tau_min
    taus = [tau_min * ratio ** (i / (points - 1)) for i in range(points)]
    main = _fit(times, depths, taus)
    if main is None:
        reasons.append("ill_conditioned_fit")
        result["input_observations"] = observations
        return result
    sse, tau, equilibrium, amplitude, index, predicted, residuals = main
    sst = math.fsum((v - math.fsum(depths) / len(depths)) ** 2 for v in depths)
    r2 = None if sst <= 0 else 1 - sse / sst
    rmse = math.sqrt(sse / len(depths))
    if equilibrium < 0 or amplitude <= 0:
        reasons.append("nonphysical_fit")
    if index in (0, points - 1):
        reasons.append("tau_search_boundary")
    if r2 is None or r2 < min_r2:
        reasons.append("poor_r_squared")
    if rmse > max_rmse:
        reasons.append("excessive_rmse")
    loo = []
    for omitted in range(len(accepted)):
        reduced = _fit(times[:omitted] + times[omitted + 1:],
                       depths[:omitted] + depths[omitted + 1:], taus)
        if reduced is None or reduced[2] < 0 or reduced[3] <= 0 or reduced[4] in (0, points - 1):
            reasons.append("unstable_leave_one_out")
            break
        loo.append(reduced)
    if len(loo) == len(accepted):
        tau_low, tau_high = min(v[1] for v in loo), max(v[1] for v in loo)
        eq_low, eq_high = min(v[2] for v in loo), max(v[2] for v in loo)
        if (tau_high - tau_low) / tau > max_tau_spread:
            reasons.append("tau_sensitivity")
        if eq_high - eq_low > max_eq_spread:
            reasons.append("equilibrium_sensitivity")
    if reasons:
        result["input_observations"] = observations
        return result
    scenario_fits = [main, min(loo, key=lambda v: v[1]), max(loo, key=lambda v: v[1]),
                     min(loo, key=lambda v: v[2]), max(loo, key=lambda v: v[2])]
    scenarios = []
    for fit in scenario_fits:
        value = _parameters(fit)
        if value not in scenarios:
            scenarios.append(value)
    result.update(status="fitted", parameters=_parameters(main),
                  support={"depth_min_m": min(depths), "depth_max_m": max(depths),
                           "duration_minutes": times[-1]}, scenarios=scenarios,
                  fitted_points=[{"at": row[2], "observed_m": row[1],
                                  "predicted_m": pred, "residual_m": residual}
                                 for row, pred, residual in zip(accepted, predicted, residuals)],
                  metrics={"sse_m2": sse, "rmse_m": rmse,
                           "max_abs_residual_m": max(abs(v) for v in residuals),
                           "r_squared": r2},
                  uncertainty={"method": "leave_one_out_sensitivity",
                               "tau_min_minutes": tau_low, "tau_max_minutes": tau_high,
                               "equilibrium_depth_min_m": eq_low,
                               "equilibrium_depth_max_m": eq_high,
                               "depth_residual_envelope_m": max(abs(v) for v in residuals) + (accuracy or 0),
                               "measurement_accuracy_known": accuracy is not None,
                               "statistical_confidence": None})
    return result
