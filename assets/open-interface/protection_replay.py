"""Pure shadow protection replay; its events never actuate a device."""

from datetime import datetime, timedelta, timezone
from copy import deepcopy
import math

from protection_contracts import StudyError, digest


_RANGES = {
    "current_max_A": (0, 100000), "voltage_min_V": (0, 100000),
    "voltage_max_V": (0, 100000), "current_deviation_ratio": (0, 1),
    "voltage_deviation_ratio": (0, 1), "temperature_C": (-100, 500),
    "speed_rpm": (0, 120000), "frequency_hz": (0, 1000),
}


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise StudyError("invalid_field", "missing or unknown protection field")
    return value


def _text(value, name):
    if type(value) is not str or not value.strip() or len(value) > 240:
        raise StudyError("invalid_field", f"invalid {name}")
    return value


def _integer(value, name, low, high):
    if type(value) is not int or value < low or value > high:
        raise StudyError("invalid_field", f"invalid {name}")
    return value


def _number(value, name, low, high, optional=False):
    if optional and value is None:
        return None
    if type(value) not in (int, float) or value < low or value > high:
        raise StudyError("invalid_field", f"invalid {name}")
    value = float(value)
    if not math.isfinite(value):
        raise StudyError("invalid_field", f"invalid {name}")
    return value


def _bool(value, name):
    if type(value) is not bool:
        raise StudyError("invalid_field", f"invalid {name}")
    return value


def _time(value, name):
    if type(value) is not str or len(value) > 64:
        raise StudyError("invalid_field", f"invalid {name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise StudyError("invalid_field", f"invalid {name}") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise StudyError("invalid_field", f"{name} requires UTC timezone")
    return parsed.astimezone(timezone.utc)


def _validate_settings(settings):
    _fields(settings, ("version", "basis_reference", "approved_by", "effective_from", "effective_until", "maximum_frame_gap_ms", "reset_safe_ms", "rules"))
    for key in ("version", "basis_reference", "approved_by"):
        _text(settings[key], key)
    start = _time(settings["effective_from"], "effective_from")
    end = _time(settings["effective_until"], "effective_until")
    if not start < end or end - start > timedelta(days=366):
        raise StudyError("invalid_field", "invalid settings effective interval")
    _integer(settings["maximum_frame_gap_ms"], "maximum_frame_gap_ms", 1, 60000)
    _integer(settings["reset_safe_ms"], "reset_safe_ms", 0, 3600000)
    rules = settings["rules"]
    if type(rules) is not list or not 1 <= len(rules) <= 16:
        raise StudyError("invalid_field", "rules count must be 1..16")
    ids = set()
    for rule in rules:
        _fields(rule, ("id", "metric", "direction", "pickup", "dropout", "delay_ms"))
        name = _text(rule["id"], "rule id")
        if name in ids:
            raise StudyError("invalid_field", "duplicate rule id")
        ids.add(name)
        metric = rule["metric"]
        if type(metric) is not str or metric not in _RANGES:
            raise StudyError("invalid_field", "invalid rule metric")
        if type(rule["direction"]) is not str or rule["direction"] not in ("high", "low"):
            raise StudyError("invalid_field", "invalid rule direction")
        low, high = _RANGES[metric]
        pickup = _number(rule["pickup"], "pickup", low, high)
        dropout = _number(rule["dropout"], "dropout", low, high)
        if (rule["direction"] == "high" and pickup <= dropout) or (rule["direction"] == "low" and pickup >= dropout):
            raise StudyError("invalid_field", "pickup/dropout hysteresis order invalid")
        _integer(rule["delay_ms"], "delay_ms", 0, 3600000)
    return start, end


def _measurements(value):
    _fields(value, ("currents_A", "voltages_V", "temperature_C", "speed_rpm", "frequency_hz"))
    for name, bounds in (("currents_A", (0, 100000)), ("voltages_V", (0, 100000))):
        triple = value[name]
        if triple is not None:
            if type(triple) is not list or len(triple) != 3:
                raise StudyError("invalid_field", f"{name} must be three phases or null")
            for item in triple:
                _number(item, name, *bounds)
    for name, bounds in (("temperature_C", (-100, 500)), ("speed_rpm", (0, 120000)), ("frequency_hz", (0, 1000))):
        _number(value[name], name, *bounds, optional=True)


def _metric(name, measurements):
    if name.startswith("current_"):
        phases = measurements["currents_A"]
    elif name.startswith("voltage_"):
        phases = measurements["voltages_V"]
    else:
        return measurements[name]
    if phases is None:
        return None
    if name.endswith("deviation_ratio"):
        mean = sum(phases) / 3
        return max(abs(value - mean) for value in phases) / mean if mean else None
    return max(phases) if "max" in name else min(phases)


class ProtectionReplay:
    """Stateful pure replay adapter. Persist `final_state` at the integration boundary."""

    def __init__(self, settings):
        self.start, self.end = _validate_settings(settings)
        self.settings = deepcopy(settings)
        self.settings_digest = digest(self.settings)
        self.rules = {rule["id"]: rule for rule in self.settings["rules"]}
        self.state = {name: {"state": "normal", "since": None} for name in self.rules}
        self.hardware_inhibit = False
        self.safe_since = None
        self.cursor_at = None
        self.cursor_sequence = None
        self.cursor_boot = None
        self.continuous = False
        self.events = []
        self.frame_index = 0
        self.last_qualification = "unconfigured"

    def snapshot(self):
        """Serializable continuity checkpoint; external adapter owns durable integrity."""
        return {
            "schema": "protection_replay_state/1", "settings_digest": self.settings_digest,
            "frame_index": self.frame_index,
            "cursor_at": self.cursor_at.isoformat() if self.cursor_at is not None else None,
            "cursor_sequence": self.cursor_sequence, "cursor_boot": self.cursor_boot,
            "rules": {name: entry["state"] for name, entry in self.state.items()},
            "hardware_inhibit": self.hardware_inhibit,
        }

    def restore(self, snapshot):
        """Restore latches only; pickup and safe-time evidence must start afresh."""
        _fields(snapshot, ("schema", "settings_digest", "frame_index", "cursor_at", "cursor_sequence", "cursor_boot", "rules", "hardware_inhibit"))
        if (type(snapshot["schema"]) is not str or snapshot["schema"] != "protection_replay_state/1"
                or type(snapshot["settings_digest"]) is not str or snapshot["settings_digest"] != self.settings_digest):
            raise StudyError("invalid_state", "snapshot schema or settings digest mismatch")
        _integer(snapshot["frame_index"], "frame_index", 0, 2**53 - 1)
        _bool(snapshot["hardware_inhibit"], "hardware_inhibit")
        cursor_items = (snapshot["cursor_at"], snapshot["cursor_sequence"], snapshot["cursor_boot"])
        if all(item is None for item in cursor_items):
            cursor_at = cursor_sequence = cursor_boot = None
        elif all(item is not None for item in cursor_items):
            cursor_at = _time(snapshot["cursor_at"], "cursor_at")
            cursor_sequence = _integer(snapshot["cursor_sequence"], "cursor_sequence", 0, 2**53 - 1)
            cursor_boot = _text(snapshot["cursor_boot"], "cursor_boot")
        else:
            raise StudyError("invalid_state", "partial snapshot cursor")
        states = snapshot["rules"]
        if type(states) is not dict or set(states) != set(self.rules):
            raise StudyError("invalid_state", "snapshot rule set mismatch")
        for state in states.values():
            if type(state) is not str or state not in ("normal", "pickup", "would_trip_latched"):
                raise StudyError("invalid_state", "invalid snapshot rule state")
        if (snapshot["frame_index"] == 0) != (cursor_at is None):
            raise StudyError("invalid_state", "snapshot cursor/count mismatch")
        if snapshot["frame_index"] == 0 and (snapshot["hardware_inhibit"] or any(state != "normal" for state in states.values())):
            raise StudyError("invalid_state", "empty snapshot cannot contain a latch")
        self.frame_index = snapshot["frame_index"]
        self.cursor_at, self.cursor_sequence, self.cursor_boot = cursor_at, cursor_sequence, cursor_boot
        self.hardware_inhibit = snapshot["hardware_inhibit"]
        self.state = {name: {"state": "would_trip_latched" if state == "would_trip_latched" else "normal", "since": None}
                      for name, state in states.items()}
        self.safe_since = None
        self.continuous = False
        self.last_qualification = "stale"
        self.events = []

    def step(self, frame):
        _fields(frame, ("at", "sequence", "boot_id", "quality", "configuration_valid", "hardware_trip", "reset_request", "measurements"))
        at = _time(frame["at"], "frame.at")
        sequence = _integer(frame["sequence"], "sequence", 0, 2**53 - 1)
        boot = _text(frame["boot_id"], "boot_id")
        if type(frame["quality"]) is not str or frame["quality"] not in ("valid", "invalid", "unknown"):
            raise StudyError("invalid_field", "invalid quality")
        _bool(frame["configuration_valid"], "configuration_valid")
        _bool(frame["hardware_trip"], "hardware_trip")
        if type(frame["reset_request"]) is not str or frame["reset_request"] not in ("none", "local"):
            raise StudyError("invalid_field", "invalid reset_request")
        measurements = frame["measurements"]
        _measurements(measurements)
        values = {name: _metric(rule["metric"], measurements) for name, rule in self.rules.items()}
        reasons = []
        ordered = self.cursor_at is None or (at > self.cursor_at and sequence > self.cursor_sequence)
        gap = self.cursor_at is not None and ordered and (at - self.cursor_at).total_seconds() * 1000 > self.settings["maximum_frame_gap_ms"]
        boot_changed = self.cursor_boot is not None and boot != self.cursor_boot
        if not ordered:
            qualification = "invalid"
            reasons.append("reordered_frame")
        elif not frame["configuration_valid"]:
            qualification = "config_revoked"
            reasons.append("config_revoked")
        elif at < self.start or at >= self.end:
            qualification = "unconfigured"
            reasons.append("settings_outside_effective_interval")
        elif frame["quality"] != "valid" or any(value is None for value in values.values()):
            qualification = "invalid"
            reasons.append("invalid_quality_or_required_measurement")
        else:
            qualification = "valid"
        if gap:
            reasons.append("frame_gap")
        if boot_changed:
            reasons.append("boot_change")
        if qualification == "valid" and (gap or boot_changed):
            qualification = "stale"
        contiguous = qualification == "valid" and self.continuous and not gap and not boot_changed
        if frame["hardware_trip"]:
            if not self.hardware_inhibit:
                self.events.append({"frame_index": self.frame_index, "kind": "hardware_inhibit_latched"})
            self.hardware_inhibit = True
        processable = qualification in ("valid", "stale")
        safe = processable
        for name, rule in self.rules.items():
            entry = self.state[name]
            if not processable:
                if entry["state"] == "pickup":
                    entry.update(state="normal", since=None)
                safe = False
                continue
            value = values[name]
            high = rule["direction"] == "high"
            pickup = value >= rule["pickup"] if high else value <= rule["pickup"]
            dropout_safe = value <= rule["dropout"] if high else value >= rule["dropout"]
            safe = safe and dropout_safe
            if entry["state"] == "would_trip_latched":
                continue
            if not contiguous and entry["state"] == "pickup":
                entry.update(state="normal", since=None)
            if entry["state"] == "normal":
                if pickup:
                    entry.update(state="pickup", since=at)
            elif dropout_safe:
                entry.update(state="normal", since=None)
            if entry["state"] == "pickup" and (at - entry["since"]).total_seconds() * 1000 >= rule["delay_ms"]:
                entry.update(state="would_trip_latched", since=None)
                self.events.append({"frame_index": self.frame_index, "kind": "would_trip", "rule_id": name})
        if safe:
            if not contiguous or self.safe_since is None:
                self.safe_since = at
        else:
            self.safe_since = None
        reset_decision = "none"
        if frame["reset_request"] == "local":
            latched = self.hardware_inhibit or any(entry["state"] == "would_trip_latched" for entry in self.state.values())
            sufficient = self.safe_since is not None and (at - self.safe_since).total_seconds() * 1000 >= self.settings["reset_safe_ms"]
            if qualification == "valid" and not frame["hardware_trip"] and safe and sufficient and latched:
                for entry in self.state.values():
                    entry.update(state="normal", since=None)
                self.hardware_inhibit = False
                reset_decision = "accepted"
                self.events.append({"frame_index": self.frame_index, "kind": "local_reset_accepted"})
            else:
                reset_decision = "denied"
                reasons.append("reset_conditions_not_met")
                self.events.append({"frame_index": self.frame_index, "kind": "local_reset_denied"})
        if ordered:
            self.cursor_at, self.cursor_sequence, self.cursor_boot = at, sequence, boot
        self.continuous = processable and ordered
        self.last_qualification = qualification
        result = {
            "frame_index": self.frame_index, "at": frame["at"], "qualification": qualification,
            "rules": {name: {"state": entry["state"], "value": values[name]} for name, entry in self.state.items()},
            "inhibit": self.hardware_inhibit or qualification != "valid" or any(entry["state"] == "would_trip_latched" for entry in self.state.values()),
            "hardware_inhibit": self.hardware_inhibit, "reset_decision": reset_decision, "reasons": reasons,
        }
        self.frame_index += 1
        return result

    @property
    def final_state(self):
        return {"rules": {name: {"state": entry["state"]} for name, entry in self.state.items()},
                "hardware_inhibit": self.hardware_inhibit,
                "qualification": self.last_qualification}


def replay_protection(payload):
    _fields(payload, ("schema", "settings", "frames"))
    if payload["schema"] != "motor.protection/1":
        raise StudyError("unsupported_schema", "unsupported protection replay schema")
    frames = payload["frames"]
    if type(frames) is not list or not 1 <= len(frames) <= 8192:
        raise StudyError("invalid_field", "frames count must be 1..8192")
    engine = ProtectionReplay(payload["settings"])
    timeline = []
    times = []
    for frame in frames:
        row = engine.step(frame)
        timeline.append(row)
        times.append(_time(frame["at"], "frame.at"))
    if max(times) - min(times) > timedelta(hours=24):
        raise StudyError("invalid_field", "replay span exceeds 24 hours")
    final = engine.final_state
    final["qualification"] = timeline[-1]["qualification"]
    return {"status": "completed", "algorithm_version": "protection_replay/1",
            "settings_digest": engine.settings_digest, "events": engine.events,
            "timeline": timeline, "final_state": final, "control_eligible": False,
            "hardware_execution": False, "field_validation": False}
