"""Deterministic, server-owned virtual DSP+MCU terminals."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import math
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
                if self.scenario.name == "packet_loss" and (self.sequence + index) % 3 == 0: continue
                frames.append(encode_frame(self.profile.device_id, self.sequence, payload, timestamp=now))
            return frames

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
    """Bounded hand-off queue for Task 2; it does not persist live frames itself."""
    def __init__(self, database, *, cadence_seconds: float = 60.0, queue_size: int = 512):
        self.database, self.cadence_seconds, self.frames = database, cadence_seconds, deque(maxlen=queue_size)
        rows = database.query_all("SELECT id, well_id, serial_number, device_type, firmware_version FROM devices ORDER BY id")
        self.terminals = {row["id"]: VirtualTerminal(profile_for(row["id"], row)) for row in rows}
        self.latest_registers, self._stop, self._thread, self._lock = {}, threading.Event(), None, threading.RLock()
    @property
    def scenario_lock(self):
        """Shared mutation lock used by service requests and the runner."""
        return self._lock
    def get(self, device_id: str) -> VirtualTerminal | None: return self.terminals.get(device_id)
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive(): return
            self._stop.clear(); self._thread = threading.Thread(target=self._run, name="virtual-terminal-runner", daemon=False); self._thread.start()
    def stop(self):
        with self._lock:
            self._stop.set(); thread = self._thread
        if thread and thread is not threading.current_thread(): thread.join(timeout=5)
    def _run(self):
        while not self._stop.is_set():
            now = datetime.now(SHANGHAI_TIMEZONE).replace(second=0, microsecond=0)
            try:
                with self._lock:
                    for device_id, terminal in self.terminals.items():
                        packets = terminal.tick(now)
                        self.frames.extend(packets)
                        if packets: self.latest_registers[device_id] = terminal._registers(now)
            except Exception as error:
                try:
                    with self.database.transaction(None, "simulator.runner_error", "simulator", None, source_type="system") as conn:
                        conn.execute("INSERT INTO system_events(event_type, detail, status, source_type) VALUES('simulator_runner_error', ?, 'open', 'system')", (str(error),))
                except Exception: pass
            self._stop.wait(self.cadence_seconds)
