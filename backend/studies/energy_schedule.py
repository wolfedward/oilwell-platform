"""Complete finite search of supplied, shadow-only pump schedules."""
import datetime as dt
import itertools
import math

from .contracts import StudyError, digest, fields
from .recovery import _fail, _integer, _number, _text, _time

VERSION = "shadow_schedule/1"
LOCAL = dt.timezone(dt.timedelta(hours=8))
WELL_KEYS = ("well_id", "recovery_record_id", "parameter_basis", "initial_depth_m",
             "initial_state", "initial_state_elapsed_minutes", "annulus_storage_area_m2",
             "pump_intake_depth_m", "minimum_submergence_m", "pumping_liquid_rate_m3_h",
             "run_input_power_kw", "standby_input_power_kw", "startup_input_power_kw",
             "startup_duration_minutes", "min_on_minutes", "min_off_minutes",
             "max_starts_per_calendar_day", "minimum_liquid_m3_per_calendar_day",
             "terminal_max_depth_m", "candidates", "recovery")
PARAM_KEYS = ("equilibrium_depth_m", "initial_depth_m", "tau_minutes")
SUPPORT_KEYS = ("depth_min_m", "depth_max_m", "duration_minutes")


def _recovery(value, well_id):
    if type(value) is not dict or value.get("status") != "fitted" or value.get("well_id") != well_id or value.get("model_version") != "recovery_depth/1":
        _fail("approved fitted recovery")
    parameters, support = value.get("parameters"), value.get("support")
    fields(parameters, PARAM_KEYS)
    fields(support, SUPPORT_KEYS)
    scenarios = value.get("scenarios")
    if type(scenarios) is not list or not 1 <= len(scenarios) <= 5:
        _fail("recovery.scenarios")
    for parameter in [parameters] + scenarios:
        fields(parameter, PARAM_KEYS)
        _number(parameter["equilibrium_depth_m"], "equilibrium_depth_m", 0, 100000)
        _number(parameter["initial_depth_m"], "initial_depth_m", 0, 100000)
        _number(parameter["tau_minutes"], "tau_minutes", 0, 10080, lower_open=True)
        if parameter["initial_depth_m"] <= parameter["equilibrium_depth_m"]:
            _fail("nonphysical recovery amplitude")
    minimum = _number(support["depth_min_m"], "support.depth_min_m", 0, 100000)
    maximum = _number(support["depth_max_m"], "support.depth_max_m", minimum, 100000)
    duration = _number(support["duration_minutes"], "support.duration_minutes", 0, 10080, lower_open=True)
    return scenarios, (minimum, maximum, duration)


def _well(value, minutes):
    fields(value, WELL_KEYS)
    name = _text(value["well_id"], "well_id", 128)
    _text(value["recovery_record_id"], "recovery_record_id", 128)
    _text(value["parameter_basis"], "parameter_basis", 2000)
    for key in ("initial_depth_m", "pump_intake_depth_m", "minimum_submergence_m", "terminal_max_depth_m"):
        _number(value[key], key, 0, 100000)
    _number(value["annulus_storage_area_m2"], "annulus_storage_area_m2", 0, 100, lower_open=True)
    _number(value["pumping_liquid_rate_m3_h"], "pumping_liquid_rate_m3_h", 0, 10000, lower_open=True)
    for key in ("run_input_power_kw", "standby_input_power_kw", "startup_input_power_kw"):
        _number(value[key], key, 0, 1e6)
    if value["initial_state"] not in ("on", "off"):
        _fail("initial_state")
    _integer(value["initial_state_elapsed_minutes"], "initial_state_elapsed_minutes", 0, 10080)
    _integer(value["startup_duration_minutes"], "startup_duration_minutes", 0, 60)
    _integer(value["min_on_minutes"], "min_on_minutes", 1, 1440)
    _integer(value["min_off_minutes"], "min_off_minutes", 1, 1440)
    _integer(value["max_starts_per_calendar_day"], "max_starts_per_calendar_day", 1, 1440)
    _number(value["minimum_liquid_m3_per_calendar_day"], "minimum_liquid_m3_per_calendar_day", 0, 1e6)
    _recovery(value["recovery"], name)
    candidates = value["candidates"]
    if type(candidates) is not list or not 1 <= len(candidates) <= 8:
        _fail("candidates")
    ids = set()
    for candidate in candidates:
        fields(candidate, ("candidate_id", "segments"))
        candidate_id = _text(candidate["candidate_id"], "candidate_id", 64)
        if candidate_id in ids:
            _fail("duplicate candidate_id")
        ids.add(candidate_id)
        segments = candidate["segments"]
        if type(segments) is not list or not 1 <= len(segments) <= 128:
            _fail("segments")
        total, previous = 0, None
        for segment in segments:
            fields(segment, ("state", "duration_minutes"))
            state = segment["state"]
            if state not in ("on", "off") or state == previous:
                _fail("segment state")
            total += _integer(segment["duration_minutes"], "segment.duration_minutes", 1, 10080)
            previous = state
        if total != minutes:
            _fail("candidate coverage")
    return name


def _candidate(well, candidate, dates):
    name = well["well_id"]
    scenarios, support = _recovery(well["recovery"], name)
    minimum_depth, maximum_depth, max_off = support
    violations = set()
    daily_liquid = {date: 0.0 for date in dates}
    daily_energy = {date: 0.0 for date in dates}
    daily_starts = {date: 0 for date in dates}
    power_intervals = []
    start_events = []
    segments = candidate["segments"]
    elapsed, previous, t = well["initial_state_elapsed_minutes"], well["initial_state"], 0
    run_start = -elapsed if previous == "on" else None
    off_start = -elapsed if previous == "off" else None
    if previous == "off" and elapsed > max_off:
        violations.add("recovery_support_duration")
    for index, segment in enumerate(segments):
        state, duration = segment["state"], segment["duration_minutes"]
        if state != previous:
            if elapsed < well[f"min_{previous}_minutes"]:
                violations.add(f"minimum_{previous}_dwell")
            if state == "on":
                run_start = t
                off_start = None
                start_events.append(t)
                daily_starts[dates[t // 1440]] += 1
            else:
                off_start = t
                run_start = None
        elif index != 0:
            _fail("adjacent equal segment states")
        end = t + duration
        if state == "off" and end - off_start > max_off:
            violations.add("recovery_support_duration")
        split = {t, end}
        for midnight in range((t // 1440 + 1) * 1440, end, 1440):
            split.add(midnight)
        if state == "on":
            startup_end = run_start + well["startup_duration_minutes"]
            if t < startup_end < end:
                split.add(startup_end)
        cut = sorted(split)
        for left, right in zip(cut, cut[1:]):
            date = dates[left // 1440]
            startup = state == "on" and left < run_start + well["startup_duration_minutes"]
            power = (well["startup_input_power_kw"] if startup else well["run_input_power_kw"])
            if state == "off":
                power = well["standby_input_power_kw"]
            energy = power * (right - left) / 60
            liquid = (well["pumping_liquid_rate_m3_h"] * (right - left) / 60) if state == "on" else 0.0
            daily_energy[date] += energy
            daily_liquid[date] += liquid
            power_intervals.append((left, right, power, startup))
        elapsed = (elapsed + duration) if state == previous else duration
        previous = state
        t = end
    if elapsed < well[f"min_{previous}_minutes"]:
        violations.add(f"minimum_{previous}_dwell")
    for date in dates:
        if daily_starts[date] > well["max_starts_per_calendar_day"]:
            violations.add("daily_start_limit")
        if daily_liquid[date] + 1e-10 < well["minimum_liquid_m3_per_calendar_day"]:
            violations.add("minimum_daily_liquid")
    trace, scenario_results = [], []
    for scenario_index, parameter in enumerate(scenarios):
        depth = float(well["initial_depth_m"])
        points = [{"minute": 0, "depth_m": depth}]
        scenario_violations = set()
        if not minimum_depth <= depth <= maximum_depth:
            scenario_violations.add("model_extrapolation")
        if well["pump_intake_depth_m"] - depth < well["minimum_submergence_m"]:
            scenario_violations.add("minimum_submergence")
        t = 0
        for segment in segments:
            delta = segment["duration_minutes"]
            tau = parameter["tau_minutes"]
            recovered = -math.expm1(-delta / tau)
            q = well["pumping_liquid_rate_m3_h"] / 60 if segment["state"] == "on" else 0.0
            depth = parameter["equilibrium_depth_m"] + (depth - parameter["equilibrium_depth_m"]) * (1 - recovered) + q * tau / well["annulus_storage_area_m2"] * recovered
            if not math.isfinite(depth):
                raise StudyError("numeric_overflow", "analytic depth exceeds finite range")
            t += delta
            points.append({"minute": t, "depth_m": depth})
            if not minimum_depth <= depth <= maximum_depth:
                scenario_violations.add("model_extrapolation")
            if well["pump_intake_depth_m"] - depth < well["minimum_submergence_m"]:
                scenario_violations.add("minimum_submergence")
        if depth > well["terminal_max_depth_m"]:
            scenario_violations.add("terminal_depth_limit")
        violations.update(scenario_violations)
        trace.append(points)
        scenario_results.append({"scenario_index": scenario_index, "terminal_depth_m": depth,
                                 "violations": sorted(scenario_violations)})
    return {"well_id": name, "candidate_id": candidate["candidate_id"],
            "segments": candidate["segments"], "daily_liquid_m3": daily_liquid,
            "daily_energy_kwh": daily_energy, "daily_starts": daily_starts,
            "power_intervals": power_intervals, "start_events": start_events,
            "depth_trace": trace, "scenario_results": scenario_results,
            "violations": violations}


def _combine(parts, dates, site_limit, max_starts):
    violations = set().union(*(part["violations"] for part in parts))
    boundaries = sorted({point for part in parts for left, right, _, _ in part["power_intervals"]
                         for point in (left, right)})
    peak = 0.0
    positions = [0] * len(parts)
    for t in boundaries[:-1]:
        power = 0.0
        startup_count = 0
        for index, part in enumerate(parts):
            intervals = part["power_intervals"]
            while positions[index] < len(intervals) and intervals[positions[index]][1] <= t:
                positions[index] += 1
            if positions[index] == len(intervals) or intervals[positions[index]][0] > t:
                _fail("incomplete power intervals")
            active = intervals[positions[index]]
            power += active[2]
            startup_count += active[3]
        peak = max(peak, power)
        if power > site_limit + 1e-9:
            violations.add("site_power_limit")
        if startup_count > max_starts:
            violations.add("simultaneous_starts")
    for t in {event for part in parts for event in part["start_events"]}:
        if sum(t in part["start_events"] for part in parts) > max_starts:
            violations.add("simultaneous_starts")
    daily_liquid = {date: math.fsum(part["daily_liquid_m3"][date] for part in parts) for date in dates}
    daily_energy = {date: math.fsum(part["daily_energy_kwh"][date] for part in parts) for date in dates}
    daily_starts = {date: sum(part["daily_starts"][date] for part in parts) for date in dates}
    identifiers = [f"{part['well_id']}:{part['candidate_id']}" for part in parts]
    return {"candidate_id": "|".join(identifiers),
            "schedule_by_well": {part["well_id"]: {"candidate_id": part["candidate_id"],
                                                    "segments": part["segments"]} for part in parts},
            "daily_liquid_m3": daily_liquid, "daily_energy_kwh": daily_energy,
            "daily_starts": daily_starts, "peak_power_kw": peak,
            "depth_trace": {part["well_id"]: part["depth_trace"] for part in parts},
            "scenario_results": {part["well_id"]: part["scenario_results"] for part in parts},
            "violations": sorted(violations),
            "total_energy_kwh": math.fsum(daily_energy.values()),
            "total_starts": sum(daily_starts.values())}


def optimize_schedule(payload):
    fields(payload, ("schema", "horizon_start", "horizon_end", "production_day_timezone",
                     "site_power_limit_kw", "max_simultaneous_starts", "wells"))
    if payload["schema"] != "energy.schedule/1" or payload["production_day_timezone"] != "Asia/Shanghai":
        _fail("schema or production_day_timezone")
    start, end = _time(payload["horizon_start"], "horizon_start").astimezone(LOCAL), _time(payload["horizon_end"], "horizon_end").astimezone(LOCAL)
    if any((value.hour, value.minute, value.second, value.microsecond) != (0, 0, 0, 0) for value in (start, end)):
        _fail("local midnight boundary")
    minutes = int((end - start).total_seconds() / 60)
    if not 1440 <= minutes <= 10080 or minutes % 1440:
        _fail("horizon duration")
    site_limit = _number(payload["site_power_limit_kw"], "site_power_limit_kw", 0, 1e6, lower_open=True)
    max_starts = _integer(payload["max_simultaneous_starts"], "max_simultaneous_starts", 1, 4)
    wells = payload["wells"]
    if type(wells) is not list or not 1 <= len(wells) <= 4:
        _fail("wells")
    names = [_well(well, minutes) for well in wells]
    if len(set(names)) != len(names):
        _fail("duplicate well_id")
    combinations = math.prod(len(well["candidates"]) for well in wells)
    boundary_cost = sum(max(len(candidate["segments"]) + 1 for candidate in well["candidates"]) for well in wells)
    if combinations > 4096 or combinations * boundary_cost * 3 > 1_000_000:
        raise StudyError("resource_limit", "supplied candidate search exceeds bounded budget")
    dates = [(start + dt.timedelta(days=day)).date().isoformat() for day in range(minutes // 1440)]
    precomputed = [[_candidate(well, candidate, dates) for candidate in well["candidates"]]
                   for well in wells]
    feasible, rejected, counts = [], [], {}
    for parts in itertools.product(*precomputed):
        candidate = _combine(parts, dates, site_limit, max_starts)
        if candidate["violations"]:
            for code in candidate["violations"]:
                counts[code] = counts.get(code, 0) + 1
            if len(rejected) < 10:
                rejected.append(candidate)
        else:
            feasible.append(candidate)
    feasible.sort(key=lambda value: (value["total_energy_kwh"], value["peak_power_kw"],
                                     value["total_starts"], value["candidate_id"]))
    return {"status": "feasible" if feasible else "no_feasible_candidate",
            "algorithm_version": VERSION, "input_digest": digest(payload),
            "search_space": "supplied_candidates", "search_complete": True,
            "evaluated_combinations": combinations,
            "selected_candidate_id": feasible[0]["candidate_id"] if feasible else None,
            "ranked_candidates": feasible[:20],
            "no_feasible_evidence": {"rejection_counts": counts, "rejected_candidates": rejected},
            "actual_energy_saving_kwh": None, "shadow_only": True,
            "dispatch_allowed": False, "field_validation": False,
            "warnings": ["declared_parameters_not_independently_verified"],
            "assumptions": ["Only supplied finite candidates are searched; no continuous-space optimum is claimed.",
                            "Depth uses an empirical first-order storage balance within measured support.",
                            "Declared power, rate, geometry, and recovery inputs are not independently verified."]}
