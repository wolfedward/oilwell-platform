"""Deterministic, server-owned virtual DSP+MCU terminals."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import queue
import threading
import time

from backend.protocol import encode_frame


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
SCENARIOS = frozenset({"normal", "supply_shortage", "pump_leak", "empty_pump", "sensor_drift", "packet_loss", "offline"})
STATES = frozenset({"booting", "online", "running", "stopped", "fault", "local_control", "offline"})
_TRANSITIONS = {
    "booting": {"online", "offline", "fault"}, "online": {"running", "stopped", "local_control", "offline", "fault"},
    "running": {"stopped", "local_control", "offline", "fault"}, "stopped": {"running", "local_control", "offline", "fault"},
    "local_control": {"online", "offline", "fault"}, "fault": {"offline", "booting"}, "offline": {"booting", "online"},
}


class SimulatorValidationError(ValueError): pass


@dataclass(frozen=True)
class TerminalProfile:
    device_id: str
    well_id: str | None = None
    serial_number: str | None = None
    device_type: str = "DSP_MCU_CONTROLLER"
    firmware_version: str = "2.4.1"


@dataclass(frozen=True)
class ScenarioState:
    name: str
    duration_minutes: int
    expires_at: datetime | None
    terminal_state: str


def profile_for(device_id: str, row=None) -> TerminalProfile:
    if row is None: return TerminalProfile(device_id, serial_number=f"SIM-{device_id}")
    return TerminalProfile(row["id"], row["well_id"], row["serial_number"], row["device_type"], row["firmware_version"])


class VirtualTerminal:
    """A terminal's register values are pure functions of profile, seed, scenario and tick time."""
    def __init__(self, profile: TerminalProfile, seed: int = 202606):
        self.profile, self.seed, self.sequence, self.state = profile, seed, 0, "online"
        self.scenario = ScenarioState("normal", 0, None, "online")
        self._last_tick: datetime | None = None
        self._frame_history: dict[int, bytes] = {}
        self._history_size = 4096
        self._lock = threading.RLock()

    def transition(self, state: str) -> str:
        with self._lock:
            if state not in STATES or state not in _TRANSITIONS[self.state]: raise SimulatorValidationError("illegal terminal state transition")
            self.state = state
            return state

    def preview_scenario(self, name: str, duration_minutes: int, *, now: datetime | None = None) -> ScenarioState:
        """Validate and plan a scenario without mutating terminal memory."""
        if name not in SCENARIOS or not isinstance(duration_minutes, int) or isinstance(duration_minutes, bool) or not 1 <= duration_minutes <= 1440:
            raise SimulatorValidationError("scenario and duration_minutes must be valid")
        with self._lock:
            now = self._aware(now) if now is not None else self._last_tick
            # With no supplied/previous simulation clock, arm the duration on
            # the next tick. This makes an externally configured scenario
            # reproducible when a caller supplies the virtual clock only to
            # ``tick``.
            terminal_state = "offline" if name == "offline" else ("online" if self.state == "offline" else self.state)
            return ScenarioState(name, duration_minutes, None if now is None else now + timedelta(minutes=duration_minutes), terminal_state)

    def plan_scenario(self, name: str, duration_minutes: int, *, now: datetime | None = None) -> ScenarioState:
        """Named alias for callers that treat preview as a mutation plan."""
        return self.preview_scenario(name, duration_minutes, now=now)

    def commit_planned_scenario(self, planned: ScenarioState) -> ScenarioState:
        """Apply an already validated plan; this path performs no fallible validation."""
        with self._lock:
            self.scenario = planned
            self.state = planned.terminal_state
            return planned

    def restore_scenario(self, planned: ScenarioState) -> ScenarioState:
        """Restore a database-validated plan without recalculating its expiry."""
        return self.commit_planned_scenario(planned)

    def plan_expiration(self, now: datetime) -> ScenarioState | None:
        """Return the terminal state after expiry without mutating memory."""
        now = self._aware(now)
        with self._lock:
            if self.scenario.expires_at is None or now < self.scenario.expires_at:
                return None
            terminal_state = "online" if self.state == "offline" else self.state
            return ScenarioState("normal", 0, None, terminal_state)

    def apply_scenario(self, name: str, duration_minutes: int, *, now: datetime | None = None) -> ScenarioState:
        return self.commit_planned_scenario(self.preview_scenario(name, duration_minutes, now=now))

    def tick(self, now: datetime) -> list[bytes]:
        now = self._aware(now)
        with self._lock:
            if self._last_tick and now <= self._last_tick: return []
            self._last_tick = now
            if self.scenario.name != "normal" and self.scenario.expires_at is None:
                self.scenario = ScenarioState(self.scenario.name, self.scenario.duration_minutes, now + timedelta(minutes=self.scenario.duration_minutes), self.scenario.terminal_state)
            if self.scenario.expires_at and now >= self.scenario.expires_at:
                if self.state == "offline": self.state = "online"
                self.scenario = ScenarioState("normal", 0, None, self.state)
            if self.scenario.name == "offline" or self.state == "offline":
                self.state = "offline"; return []
            registers = self._registers(now)
            payloads = [
                {"kind": "heartbeat", "status": self.state, "connection_mode": "virtual", "hardware_connected": False},
                {"kind": "telemetry", "status": self.state, "telemetry": registers, "connection_mode": "virtual", "hardware_connected": False},
                {"kind": "electric_card", "status": self.state, "electric_samples": self._electric_samples(registers), "mechanism": {"stroke_m": registers["stroke"], "strokes_per_minute": registers["strokes_per_minute"]}, "connection_mode": "virtual", "hardware_connected": False},
            ]
            frames = []
            for index, payload in enumerate(payloads):
                self.sequence += 1
                encoded = encode_frame(self.profile.device_id, self.sequence, payload, timestamp=now)
                self._frame_history[self.sequence] = encoded
                while len(self._frame_history) > self._history_size:
                    self._frame_history.pop(next(iter(self._frame_history)))
                if self.scenario.name == "packet_loss" and (self.sequence + index) % 3 == 0: continue
                frames.append(encoded)
            return frames

    def replay_frames(self, start_sequence: int, end_sequence: int) -> dict[int, bytes]:
        if (
            not isinstance(start_sequence, int) or isinstance(start_sequence, bool)
            or not isinstance(end_sequence, int) or isinstance(end_sequence, bool)
            or start_sequence < 1 or end_sequence < start_sequence or end_sequence - start_sequence > 1000
        ):
            raise SimulatorValidationError("replay sequence range is invalid")
        with self._lock:
            return {
                sequence: self._frame_history[sequence]
                for sequence in range(start_sequence, end_sequence + 1)
                if sequence in self._frame_history
            }

    def _registers(self, now: datetime) -> dict:
        stable = int.from_bytes(hashlib.sha256(f"{self.profile.device_id}|{self.seed}".encode()).digest()[:4], "big")
        phase = (now.timestamp() / 60 + stable % 97) / 11
        fillage = .86 + .04 * math.sin(phase); power = 5.4 + .6 * math.sin(phase); stroke = 2.6 + .08 * math.cos(phase); strokes = 4.2 + .2 * math.sin(phase / 2)
        if self.scenario.name == "supply_shortage": fillage -= .28; power *= .78
        elif self.scenario.name == "pump_leak": fillage -= .16; stroke *= .88; strokes *= 1.12
        elif self.scenario.name == "empty_pump": fillage = .22; power *= .63
        elif self.scenario.name == "sensor_drift": fillage += .07; power += .45
        return {"current": round(power * 3.8, 3), "voltage": round(380 + 4 * math.cos(phase), 3), "active_power": round(power, 3), "power_factor": round(.88 + .03 * math.sin(phase), 3), "frequency": round(50 + .05 * math.cos(phase), 3), "stroke": round(stroke, 3), "strokes_per_minute": round(strokes, 3), "fillage": round(max(.05, min(.99, fillage)), 3)}

    @staticmethod
    def _electric_samples(registers: dict) -> list[dict]:
        return [{"phase": index, "current": round(registers["current"] * (.86 + .14 * math.sin(index / 12 * math.tau)), 3), "voltage": registers["voltage"], "power": round(registers["active_power"] * (.86 + .14 * math.sin(index / 12 * math.tau)), 3)} for index in range(12)]
    @staticmethod
    def _aware(now: datetime) -> datetime:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(hours=8): raise SimulatorValidationError("tick requires aware +08:00 datetime")
        return now


class SimulatorRegistry:
    """Own terminal state, durable scenario transitions, and bounded live-frame hand-off."""
    def __init__(self, database, *, cadence_seconds: float = 60.0, queue_size: int = 512, clock=None):
        if not isinstance(queue_size, int) or isinstance(queue_size, bool) or queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        self.database, self.cadence_seconds = database, cadence_seconds
        self._clock = clock or (lambda: datetime.now(SHANGHAI_TIMEZONE))
        self._queue, self._queue_size, self._queue_lock, self._frame_sink = deque(), queue_size, threading.RLock(), None
        self._delivered, self._dropped = 0, 0
        rows = database.query_all("SELECT id, well_id, serial_number, device_type, firmware_version, status FROM devices ORDER BY id")
        self.terminals = {row["id"]: VirtualTerminal(profile_for(row["id"], row)) for row in rows}
        for cursor in database.query_all("SELECT device_id,last_sequence FROM acquisition_cursors"):
            terminal = self.terminals.get(cursor["device_id"])
            if terminal is not None and isinstance(cursor["last_sequence"], int) and cursor["last_sequence"] >= 0:
                terminal.sequence = cursor["last_sequence"]
        self.latest_registers, self._stop, self._thread, self._lock = {}, threading.Event(), None, threading.RLock()
        self._restore_persisted_scenarios(rows)
    @property
    def scenario_lock(self):
        """Shared mutation lock used by service requests and the runner."""
        return self._lock
    def now(self): return VirtualTerminal._aware(self._clock())
    def get(self, device_id: str) -> VirtualTerminal | None: return self.terminals.get(device_id)
    def replay_frames(self, device_id: str, start_sequence: int, end_sequence: int) -> dict[int, bytes]:
        terminal = self.get(device_id)
        if terminal is None: raise SimulatorValidationError("device is not registered")
        return terminal.replay_frames(start_sequence, end_sequence)
    def set_frame_sink(self, sink):
        if sink is not None and (type(sink) is not queue.Queue or sink.maxsize <= 0):
            raise ValueError("frame sink must be a bounded standard-library queue.Queue or None")
        with self._queue_lock: self._frame_sink = sink
    def drain_frames(self, max_items=None):
        if max_items is not None and (not isinstance(max_items, int) or isinstance(max_items, bool) or max_items < 0):
            raise ValueError("max_items must be a non-negative integer or None")
        with self._queue_lock:
            count = len(self._queue) if max_items is None else min(max_items, len(self._queue))
            drained = [self._queue.popleft() for _ in range(count)]
            self._delivered += len(drained)
            return drained
    def frame_stats(self):
        with self._queue_lock: return {"queued": len(self._queue), "delivered": self._delivered, "dropped": self._dropped}
    def _publish_frames(self, packets):
        batch = list(packets)
        if not batch: return
        with self._queue_lock: sink = self._frame_sink
        if sink is not None:
            try:
                sink.put_nowait(tuple(batch))
            except queue.Full:
                pass
            else:
                with self._queue_lock: self._delivered += len(batch)
                return
        with self._queue_lock:
            for packet in batch:
                if len(self._queue) == self._queue_size:
                    self._queue.popleft(); self._dropped += 1
                self._queue.append(packet)
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive(): return
            self._stop.clear(); self._thread = threading.Thread(target=self._run, name="virtual-terminal-runner", daemon=False); self._thread.start()
    def stop(self):
        with self._lock:
            self._stop.set(); thread = self._thread
        if thread and thread is not threading.current_thread(): thread.join(timeout=5)
    def _scenario_from_row(self, row, now):
        name, duration, expires = row["scenario_name"], row["duration_minutes"], row["expires_at"]
        state = row["status"] if row["status"] in STATES else "online"
        if name is None:
            return ScenarioState("normal", 0, None, state), False, False
        if name == "normal" and duration in (None, 0) and expires is None:
            return ScenarioState("normal", 0, None, state), False, False
        try:
            expiry = datetime.fromisoformat(expires) if isinstance(expires, str) else None
            expiry = VirtualTerminal._aware(expiry) if expiry is not None else None
        except (TypeError, ValueError):
            expiry = None
        active = name in SCENARIOS and name != "normal" and expiry is not None and expiry > now and (name != "offline" or state == "offline")
        if active and duration in (None, 0):
            remaining = math.ceil((expiry - now).total_seconds() / 60)
            if 1 <= remaining <= 1440:
                return ScenarioState(name, remaining, expiry, state), False, True
        if active and isinstance(duration, int) and not isinstance(duration, bool) and 1 <= duration <= 1440:
            return ScenarioState(name, duration, expiry, state), False, False
        return ScenarioState("normal", 0, None, "online" if state == "offline" else state), True, False
    @staticmethod
    def _persistence_snapshot(value):
        """Keep raw SQL NULLs distinct from normalized terminal values for CAS."""
        if isinstance(value, ScenarioState):
            name = value.name
            duration = value.duration_minutes
            expires_at = value.expires_at.isoformat() if value.expires_at else None
        else:
            name = value["scenario_name"]
            duration = value["duration_minutes"]
            expires_at = value["expires_at"]
        return name, duration, expires_at, {
            "name": name,
            "duration_minutes": duration,
            "expires_at": expires_at,
        }
    def _reload_terminal(self, device_id, now):
        row = self._persisted_row(device_id)
        if row is None: return
        planned, _normalize, _migrate = self._scenario_from_row(row, now)
        self.terminals[device_id].restore_scenario(planned)
    def _persisted_row(self, device_id):
        return self.database.query_one(
            "SELECT d.status, s.name AS scenario_name, s.duration_minutes, s.expires_at FROM devices d LEFT JOIN device_scenarios s ON s.device_id=d.id WHERE d.id=?",
            (device_id,),
        )
    def _restore_persisted_scenarios(self, rows):
        now = self.now()
        for device in rows:
            combined = self._persisted_row(device["id"])
            if combined is None: continue
            planned, normalize, migrate_duration = self._scenario_from_row(combined, now)
            terminal = self.terminals[device["id"]]
            if normalize:
                if not self._persist_expiration(device["id"], combined, planned):
                    self._reload_terminal(device["id"], now); continue
            elif migrate_duration:
                if not self._persist_duration_migration(device["id"], combined, planned):
                    self._reload_terminal(device["id"], now); continue
            terminal.restore_scenario(planned)
    def _persist_duration_migration(self, device_id, before, after):
        expected_name, expected_duration, expected_expires, before_data = self._persistence_snapshot(before)
        after_data = {"name": after.name, "duration_minutes": after.duration_minutes, "expires_at": after.expires_at.isoformat() if after.expires_at else None}
        with self.database.transaction(None, "simulator.scenario_duration_migrated", "device", device_id, source_type="system", auto_audit=False) as conn:
            updated = conn.execute("UPDATE device_scenarios SET duration_minutes=? WHERE device_id=? AND name IS ? AND duration_minutes IS ? AND expires_at IS ?", (after.duration_minutes, device_id, expected_name, expected_duration, expected_expires)).rowcount
            if updated != 1: return False
            conn.execute("INSERT INTO audit_logs(actor_id, action, object_type, object_id, details_json, source_type) VALUES(NULL, 'simulator.scenario_duration_migrated', 'device', ?, ?, 'system')", (device_id, json.dumps({"before": before_data, "after": after_data}, sort_keys=True)))
            return True
    def _persist_expiration(self, device_id, before, after):
        expected_name, expected_duration, expected_expires, before_data = self._persistence_snapshot(before)
        after_data = {"name": after.name, "duration_minutes": after.duration_minutes, "expires_at": None}
        with self.database.transaction(None, "simulator.scenario_expired", "device", device_id, source_type="system", auto_audit=False) as conn:
            updated = conn.execute("UPDATE device_scenarios SET name='normal', duration_minutes=0, expires_at=NULL, source_type='virtual_device' WHERE device_id=? AND name IS ? AND duration_minutes IS ? AND expires_at IS ?", (device_id, expected_name, expected_duration, expected_expires)).rowcount
            if updated != 1: return False
            conn.execute("UPDATE devices SET status=? WHERE id=?", (after.terminal_state, device_id))
            conn.execute("INSERT INTO device_events(device_id, event_type, detail, status, source_type) VALUES(?, 'scenario_expired', ?, 'recorded', 'virtual_device')", (device_id, json.dumps({"before": before_data, "after": after_data}, sort_keys=True)))
            conn.execute("INSERT INTO audit_logs(actor_id, action, object_type, object_id, details_json, source_type) VALUES(NULL, 'simulator.scenario_expired', 'device', ?, ?, 'system')", (device_id, json.dumps({"before": before_data, "after": after_data}, sort_keys=True)))
            return True
    def run_once(self, now=None):
        now = self.now() if now is None else VirtualTerminal._aware(now)
        batch = []
        with self._lock:
            for device_id, terminal in self.terminals.items():
                expiration = terminal.plan_expiration(now)
                if expiration is not None:
                    try: owner = self._persist_expiration(device_id, terminal.scenario, expiration)
                    except Exception: continue
                    if not owner:
                        self._reload_terminal(device_id, now); continue
                    terminal.commit_planned_scenario(expiration)
                packets = terminal.tick(now)
                batch.extend(packets)
                if packets: self.latest_registers[device_id] = terminal._registers(now)
        self._publish_frames(batch)
    def _run(self):
        while not self._stop.is_set():
            now = self.now().replace(second=0, microsecond=0)
            try:
                self.run_once(now)
            except Exception as error:
                try:
                    with self.database.transaction(None, "simulator.runner_error", "simulator", None, source_type="system") as conn:
                        conn.execute("INSERT INTO system_events(event_type, detail, status, source_type) VALUES('simulator_runner_error', ?, 'open', 'system')", (str(error),))
                except Exception: pass
            self._stop.wait(self.cadence_seconds)
