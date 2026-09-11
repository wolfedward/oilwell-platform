"""Deterministic, server-owned virtual DSP+MCU terminals."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import time

from backend.protocol import FrameError, decode_frame, encode_frame
from backend.algorithms.dynagraph_profiles import build_profile_card, profile_condition


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
SCENARIOS = frozenset({"normal", "supply_shortage", "pump_leak", "empty_pump", "sensor_drift", "packet_loss", "offline"})
STATES = frozenset({"booting", "online", "running", "stopped", "fault", "local_control", "offline"})
_TRANSITIONS = {
    "booting": {"online", "offline", "fault"}, "online": {"running", "stopped", "local_control", "offline", "fault"},
    "running": {"stopped", "local_control", "offline", "fault"}, "stopped": {"running", "local_control", "offline", "fault"},
    "local_control": {"online", "offline", "fault"}, "fault": {"offline", "booting"}, "offline": {"booting", "online"},
}
COMMAND_KEYS = {
    "schema_version", "command_id", "device_id", "strategy_id",
    "strategy_version_id", "command_type", "on_minutes", "off_minutes",
    "issued_at", "expires_at", "nonce", "idempotency_key", "reason", "signature",
}
COMMAND_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class SimulatorValidationError(ValueError): pass
class TerminalUnavailable(SimulatorValidationError): pass


def _canonical_command(payload: dict) -> bytes:
    if not isinstance(payload, dict):
        raise SimulatorValidationError("control command must be an object")
    try:
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise SimulatorValidationError("control command is not canonical JSON data") from None


def _validate_signing_key(signing_key: bytes) -> bytes:
    if not isinstance(signing_key, bytes) or len(signing_key) < 32:
        raise SimulatorValidationError("command signing key is invalid")
    return signing_key


def load_command_signing_key(database_path: Path, explicit_key: bytes | None = None) -> bytes:
    """Load one private key per installation, creating it atomically when absent."""
    if explicit_key is not None:
        return _validate_signing_key(explicit_key)
    configured = os.environ.get("PLATFORM_COMMAND_SIGNING_KEY")
    if configured is not None:
        return _validate_signing_key(configured.encode("utf-8"))
    key_path = Path(str(database_path) + ".command-key")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = key_path.with_name(
        f"{key_path.name}.{secrets.token_hex(12)}.tmp"
    )
    descriptor = os.open(
        temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(secrets.token_hex(32).encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, key_path)
        except FileExistsError:
            pass
    finally:
        temporary_path.unlink(missing_ok=True)
    try:
        encoded = key_path.read_text(encoding="ascii").strip()
        signing_key = bytes.fromhex(encoded)
    except (OSError, UnicodeError, ValueError):
        raise SimulatorValidationError("installation command signing key is unreadable") from None
    return _validate_signing_key(signing_key)


def sign_control_command(payload: dict, signing_key: bytes) -> str:
    """Return the terminal-verifiable HMAC-SHA256 for an unsigned command."""
    signing_key = _validate_signing_key(signing_key)
    if "signature" in payload:
        raise SimulatorValidationError("sign only the unsigned command body")
    return hmac.new(signing_key, _canonical_command(payload), hashlib.sha256).hexdigest()


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
        self.control_cycle: dict | None = None
        self._executed_commands: dict[str, dict] = {}
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

    def preview_control_command(
        self, envelope: dict, now: datetime, signing_key: bytes,
    ) -> dict:
        """Validate a signed command without changing terminal memory."""
        now = self._aware(now)
        if not isinstance(envelope, dict) or set(envelope) != COMMAND_KEYS:
            raise SimulatorValidationError("control command fields are incomplete")
        if self.state not in {"online", "running", "stopped"}:
            raise TerminalUnavailable("terminal is unavailable for remote control")
        unsigned = {key: value for key, value in envelope.items() if key != "signature"}
        signature = envelope["signature"]
        if not isinstance(signature, str) or not hmac.compare_digest(
            signature, sign_control_command(unsigned, signing_key),
        ):
            raise SimulatorValidationError("control command signature is invalid")
        for key in ("command_id", "device_id", "strategy_id", "strategy_version_id"):
            if not isinstance(envelope[key], str) or not COMMAND_ID_PATTERN.fullmatch(envelope[key]):
                raise SimulatorValidationError(f"{key} is invalid")
        if envelope["device_id"] != self.profile.device_id:
            raise SimulatorValidationError("control command targets another terminal")
        if envelope["schema_version"] != 1:
            raise SimulatorValidationError("control command schema version is unsupported")
        if envelope["command_type"] not in {"set_intermittent_cycle", "rollback_cycle"}:
            raise SimulatorValidationError("control command type is unsupported")
        if not isinstance(envelope["nonce"], str) or not NONCE_PATTERN.fullmatch(envelope["nonce"]):
            raise SimulatorValidationError("control command nonce is invalid")
        if not isinstance(envelope["idempotency_key"], str) or not envelope["idempotency_key"]:
            raise SimulatorValidationError("control command idempotency key is invalid")
        if envelope["reason"] is not None and (
            not isinstance(envelope["reason"], str) or not 5 <= len(envelope["reason"]) <= 500
        ):
            raise SimulatorValidationError("control command reason is invalid")
        try:
            issued = self._aware(datetime.fromisoformat(envelope["issued_at"]))
            expires = self._aware(datetime.fromisoformat(envelope["expires_at"]))
        except (TypeError, ValueError):
            raise SimulatorValidationError("control command timestamps are invalid") from None
        if expires - issued != timedelta(minutes=15) or now < issued - timedelta(minutes=1):
            raise SimulatorValidationError("control command validity window is invalid")
        if now >= expires:
            raise SimulatorValidationError("control command has expired")
        on_minutes, off_minutes = envelope["on_minutes"], envelope["off_minutes"]
        if (
            isinstance(on_minutes, bool) or not isinstance(on_minutes, int)
            or not 20 <= on_minutes <= 240 or on_minutes % 10
        ):
            raise SimulatorValidationError("on_minutes violates terminal safety limits")
        if (
            isinstance(off_minutes, bool) or not isinstance(off_minutes, int)
            or not 15 <= off_minutes <= 360 or off_minutes % 15
        ):
            raise SimulatorValidationError("off_minutes violates terminal safety limits")
        return {
            "command_id": envelope["command_id"],
            "idempotency_key": envelope["idempotency_key"],
            "strategy_id": envelope["strategy_id"],
            "strategy_version_id": envelope["strategy_version_id"],
            "command_type": envelope["command_type"],
            "on_minutes": on_minutes,
            "off_minutes": off_minutes,
            "applied_at": now.isoformat(),
        }

    def commit_control_command(self, planned: dict) -> dict:
        """Commit a prevalidated command; repeated IDs return the original result."""
        with self._lock:
            command_id = planned["command_id"]
            if command_id in self._executed_commands:
                return dict(self._executed_commands[command_id])
            result = dict(planned)
            self.control_cycle = result
            self._executed_commands[command_id] = result
            self.state = "running"
            return dict(result)

    def snapshot_control_state(self) -> dict:
        """Capture mutable control state for compensation around database writes."""
        with self._lock:
            return {
                "state": self.state,
                "control_cycle": None if self.control_cycle is None else dict(self.control_cycle),
                "executed_commands": {
                    key: dict(value) for key, value in self._executed_commands.items()
                },
            }

    def restore_control_state(self, snapshot: dict) -> None:
        """Restore a snapshot when the surrounding persistence operation fails."""
        with self._lock:
            self.state = snapshot["state"]
            cycle = snapshot["control_cycle"]
            self.control_cycle = None if cycle is None else dict(cycle)
            self._executed_commands = {
                key: dict(value) for key, value in snapshot["executed_commands"].items()
            }

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
            condition = profile_condition(self.profile.device_id)
            if self.scenario.name in {"supply_shortage", "pump_leak", "empty_pump"} or (
                self.scenario.name == "normal" and self.scenario.duration_minutes > 0
            ):
                condition = self.scenario.name
            card = build_profile_card(condition, {
                "stroke_m": registers["stroke"],
                "strokes_per_minute": registers["strokes_per_minute"],
            }, variation=math.sin(now.timestamp() / 660.0 + self.seed))
            if self.scenario.name == "sensor_drift":
                # Apply the instrument bias after generating the physical cycle.
                for sample in card["electric_samples"]:
                    sample["power"] = round(sample["power"] + .45, 6)
                    sample["current"] = round(
                        abs(sample["power"]) * 1000.0 / (math.sqrt(3.0) * sample["voltage"] * .88), 6,
                    )
            payloads = [
                {"kind": "heartbeat", "status": self.state, "connection_mode": "virtual", "hardware_connected": False},
                {"kind": "telemetry", "status": self.state, "telemetry": registers, "connection_mode": "virtual", "hardware_connected": False},
                {"kind": card["kind"], "electric_samples": card["electric_samples"],
                 "mechanism": {"stroke_m": registers["stroke"], "strokes_per_minute": registers["strokes_per_minute"]},
                 "status": self.state, "connection_mode": "virtual", "hardware_connected": False},
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
    def _aware(now: datetime) -> datetime:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(hours=8): raise SimulatorValidationError("tick requires aware +08:00 datetime")
        return now


class SimulatorRegistry:
    """Own terminal state, durable scenario transitions, and bounded live-frame hand-off."""
    def __init__(self, database, *, cadence_seconds: float = 60.0, queue_size: int = 512, clock=None, signing_key=None):
        if not isinstance(queue_size, int) or isinstance(queue_size, bool) or queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        self.database, self.cadence_seconds = database, cadence_seconds
        self.command_signing_key = load_command_signing_key(database.path, signing_key)
        self._clock = clock or (lambda: datetime.now(SHANGHAI_TIMEZONE))
        self._queue, self._queue_size, self._queue_lock, self._frame_sink = deque(), queue_size, threading.RLock(), None
        self._delivery_failures = deque()
        self._delivered, self._dropped = 0, 0
        rows = database.query_all("SELECT id, well_id, serial_number, device_type, firmware_version, status FROM devices ORDER BY id")
        self.terminals = {row["id"]: VirtualTerminal(profile_for(row["id"], row)) for row in rows}
        for cursor in database.query_all("SELECT device_id,last_sequence FROM acquisition_cursors"):
            terminal = self.terminals.get(cursor["device_id"])
            if terminal is not None and isinstance(cursor["last_sequence"], int) and cursor["last_sequence"] >= 0:
                terminal.sequence = cursor["last_sequence"]
        self.latest_registers, self._stop, self._thread, self._lock = {}, threading.Event(), None, threading.RLock()
        self._restore_persisted_scenarios(rows)
        self._restore_persisted_control_cycles()
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
        with self._queue_lock:
            return {
                "queued": len(self._queue), "capacity": self._queue_size,
                "delivered": self._delivered, "dropped": self._dropped,
                "delivery_failures_pending": len(self._delivery_failures),
            }
    def drain_delivery_failures(self):
        with self._queue_lock:
            notices = list(self._delivery_failures)
            self._delivery_failures.clear()
            return notices
    def summarize_delivery_failure(self, packets, reason, *, detected_at=None):
        packet_list = list(packets)
        device_ranges = {}
        unparsed = 0
        for packet in packet_list:
            try:
                frame = decode_frame(packet)
            except FrameError:
                unparsed += 1
                continue
            entry = device_ranges.setdefault(
                frame.device_id,
                {"device_id": frame.device_id, "first_sequence": frame.sequence, "last_sequence": frame.sequence, "count": 0},
            )
            entry["first_sequence"] = min(entry["first_sequence"], frame.sequence)
            entry["last_sequence"] = max(entry["last_sequence"], frame.sequence)
            entry["count"] += 1
        observed = detected_at or self.now()
        return {
            "reason": reason,
            "dropped_frames": len(packet_list),
            "unparsed_frames": unparsed,
            "devices": [device_ranges[key] for key in sorted(device_ranges)],
            "first_detected_at": observed.isoformat(),
            "last_detected_at": observed.isoformat(),
        }
    @staticmethod
    def _merge_delivery_failure(first, second):
        device_ranges = {}
        for item in [*first["devices"], *second["devices"]]:
            if item["device_id"] not in device_ranges:
                device_ranges[item["device_id"]] = dict(item)
                continue
            entry = device_ranges[item["device_id"]]
            entry["first_sequence"] = min(entry["first_sequence"], item["first_sequence"])
            entry["last_sequence"] = max(entry["last_sequence"], item["last_sequence"])
            entry["count"] += item["count"]
        return {
            "reason": "+".join(sorted(set(first["reason"].split("+") + second["reason"].split("+")))),
            "dropped_frames": first["dropped_frames"] + second["dropped_frames"],
            "unparsed_frames": first["unparsed_frames"] + second["unparsed_frames"],
            "devices": [device_ranges[key] for key in sorted(device_ranges)],
            "first_detected_at": first["first_detected_at"],
            "last_detected_at": second["last_detected_at"],
        }
    def _queue_delivery_failure(self, notice):
        if notice["dropped_frames"] < 1:
            return
        if len(self._delivery_failures) >= self._queue_size:
            notice = self._merge_delivery_failure(self._delivery_failures.popleft(), notice)
        self._delivery_failures.append(notice)
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
            dropped = []
            for packet in batch:
                if len(self._queue) == self._queue_size:
                    dropped.append(self._queue.popleft()); self._dropped += 1
                self._queue.append(packet)
            if dropped:
                self._queue_delivery_failure(self.summarize_delivery_failure(dropped, "fallback_overflow"))
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
    def _restore_persisted_control_cycles(self):
        latest = {}
        for row in self.database.query_all(
            """SELECT command.*,device.well_id
               FROM device_commands AS command
               JOIN devices AS device ON device.id=command.device_id
               WHERE command.status='success' AND command.signature IS NOT NULL
               ORDER BY command.device_id,command.completed_at,command.created_at,command.id"""
        ):
            latest[row["device_id"]] = row
        for device_id, row in latest.items():
            terminal = self.terminals.get(device_id)
            if terminal is None:
                raise SimulatorValidationError(
                    "successful command targets an unregistered terminal"
                )
            try:
                envelope = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                raise SimulatorValidationError(
                    "successful command payload is unreadable"
                ) from None
            expected = {
                "command_id": row["id"], "device_id": device_id,
                "strategy_id": row["strategy_id"],
                "strategy_version_id": row["strategy_version_id"],
                "command_type": row["command_type"], "signature": row["signature"],
                "idempotency_key": row["idempotency_key"],
                "issued_at": row["issued_at"], "expires_at": row["expires_at"],
            }
            if not isinstance(envelope, dict) or any(
                envelope.get(key) != value for key, value in expected.items()
            ):
                raise SimulatorValidationError(
                    "successful command does not match its persisted record"
                )
            version = self.database.query_one(
                "SELECT strategy_id,configuration,status,submitted_at "
                "FROM strategy_versions WHERE id=?",
                (row["strategy_version_id"],),
            )
            strategy = self.database.query_one(
                "SELECT parent_strategy_id FROM energy_strategies WHERE id=?",
                (row["strategy_id"],),
            )
            expected_owner = row["strategy_id"]
            if row["command_type"] == "rollback_cycle":
                expected_owner = None if strategy is None else strategy["parent_strategy_id"]
            try:
                configuration = json.loads(version["configuration"])
                selected = configuration["selected_candidate"]
            except (TypeError, json.JSONDecodeError, KeyError):
                raise SimulatorValidationError(
                    "successful command strategy configuration is unreadable"
                ) from None
            if (
                version is None or strategy is None or version["submitted_at"] is None
                or version["status"] == "draft"
                or version["strategy_id"] != expected_owner
                or envelope.get("on_minutes") != selected.get("on_minutes")
                or envelope.get("off_minutes") != selected.get("off_minutes")
            ):
                raise SimulatorValidationError(
                    "successful command is not bound to its immutable strategy version"
                )
            try:
                completed = VirtualTerminal._aware(
                    datetime.fromisoformat(row["completed_at"])
                )
                validator = VirtualTerminal(terminal.profile)
                planned = validator.preview_control_command(
                    envelope, completed, self.command_signing_key,
                )
            except (TypeError, ValueError, SimulatorValidationError) as error:
                raise SimulatorValidationError(
                    "successful command cannot be restored safely"
                ) from error
            state = terminal.state
            terminal.commit_control_command(planned)
            if state not in {"online", "running", "stopped"}:
                terminal.state = state
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
