"""Authenticated device inventory, scenario and safe-channel configuration service."""

from datetime import datetime, timedelta, timezone
import json
import re

from backend.simulator import SimulatorValidationError


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class DeviceNotFound(RuntimeError): pass
class DeviceValidationError(ValueError): pass
class ChannelVersionConflict(RuntimeError): pass


class DevicesService:
    def __init__(self, database, audit, registry): self.database, self.audit, self.registry = database, audit, registry
    def list_devices(self, filters):
        clauses, values = ["1=1"], []
        for key, column in (("unit_id", "g.unit_id"), ("status", "d.status")):
            if filters.get(key): clauses.append(f"{column} = ?"); values.append(filters[key])
        if filters.get("well_id"): clauses.append("d.well_id = ?"); values.append(filters["well_id"])
        if filters.get("keyword"):
            clauses.append("(d.id LIKE ? OR d.serial_number LIKE ? OR w.code LIKE ? OR w.name LIKE ?)"); values.extend([f"%{filters['keyword']}%"] * 4)
        where = " AND ".join(clauses); page, size = filters["page"], filters["page_size"]
        total = self.database.scalar("SELECT COUNT(*) FROM devices d LEFT JOIN wells w ON w.id=d.well_id LEFT JOIN well_groups g ON g.id=w.group_id WHERE " + where, values)
        rows = self.database.query_all("SELECT d.*, w.code AS well_code, w.name AS well_name, g.unit_id FROM devices d LEFT JOIN wells w ON w.id=d.well_id LEFT JOIN well_groups g ON g.id=w.group_id WHERE " + where + " ORDER BY d.id LIMIT ? OFFSET ?", [*values, size, (page - 1) * size])
        return {"items": [self._device(row) for row in rows], "page": page, "page_size": size, "total": total}
    def detail(self, device_id):
        row = self._row(device_id)
        data = self._device(row)
        data["channels"] = [self._channel(item) for item in self.database.query_all("SELECT * FROM device_channels WHERE device_id=? ORDER BY channel_code", (device_id,))]
        data["scenario"] = self._scenario(device_id)
        return data
    def registers(self, device_id):
        self._row(device_id)
        terminal = self.registry.get(device_id)
        return {"device_id": device_id, "registers": self.registry.latest_registers.get(device_id, {}), "state": terminal.state if terminal else "offline", "connection_mode": "virtual", "hardware_connected": False}
    def apply_scenario(self, actor_id, device_id, name, duration_minutes):
        self._row(device_id)
        terminal = self.registry.get(device_id)
        if terminal is None: raise DeviceNotFound()
        now = datetime.now(SHANGHAI_TIMEZONE)
        with self.registry.scenario_lock:
            # Capture before and build a pure plan before changing terminal memory.
            before = self._scenario(device_id)
            try:
                planned = terminal.preview_scenario(name, duration_minutes, now=now)
            except SimulatorValidationError as error: raise DeviceValidationError(str(error)) from None
            after = {"name": planned.name, "duration_minutes": planned.duration_minutes, "expires_at": planned.expires_at.isoformat() if planned.expires_at else None}
            with self.database.transaction(actor_id, "device.scenario", "device", device_id, source_type="manual", auto_audit=False) as conn:
                conn.execute("UPDATE devices SET status=?, actor_id=? WHERE id=?", (planned.terminal_state, actor_id, device_id))
                conn.execute("INSERT INTO device_scenarios(device_id, name, expires_at, actor_id, source_type) VALUES(?, ?, ?, ?, 'virtual_device') ON CONFLICT(device_id) DO UPDATE SET name=excluded.name, expires_at=excluded.expires_at, actor_id=excluded.actor_id, source_type='virtual_device'", (device_id, planned.name, after["expires_at"], actor_id))
                conn.execute("INSERT INTO device_events(device_id, event_type, detail, status, actor_id, source_type) VALUES(?, 'scenario_applied', ?, 'recorded', ?, 'virtual_device')", (device_id, json.dumps({"before": before, "after": after}, sort_keys=True), actor_id))
                self.audit.record(actor_id, "device.scenario", "device", device_id, {"before": before, "after": after, "reason": "operator scenario request"}, conn, source_type="manual")
            # The commit path only assigns this prevalidated plan; no new
            # validation or fallible work can create a DB/memory split here.
            terminal.commit_planned_scenario(planned)
        return after
    def update_channel(self, actor_id, device_id, channel_id, body):
        if not DEVICE_ID.fullmatch(device_id) or not DEVICE_ID.fullmatch(channel_id): raise DeviceValidationError("invalid device or channel id")
        version = body.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1: raise DeviceValidationError("version must be a positive integer")
        allowed = {"enabled", "status", "sample_interval_ms", "scale", "offset", "version"}
        if not set(body).issubset(allowed) or set(body) == {"version"}: raise DeviceValidationError("no valid channel fields supplied")
        fields, values = [], []
        if "enabled" in body:
            if not isinstance(body["enabled"], bool): raise DeviceValidationError("enabled must be boolean")
            fields.append("enabled=?"); values.append(int(body["enabled"]))
        if "status" in body:
            if body["status"] not in {"active", "disabled"}: raise DeviceValidationError("invalid channel status")
            fields.append("status=?"); values.append(body["status"])
        if "sample_interval_ms" in body:
            value = body["sample_interval_ms"]
            if not isinstance(value, int) or isinstance(value, bool) or not 100 <= value <= 3_600_000: raise DeviceValidationError("sample_interval_ms is out of range")
            fields.append("sample_interval_ms=?"); values.append(value)
        for key in ("scale", "offset"):
            if key in body:
                value = body[key]
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not -1000 <= value <= 1000: raise DeviceValidationError(f"{key} is out of range")
                fields.append(f"{key}=?"); values.append(float(value))
        with self.database.transaction(actor_id, "device.channel.configure", "device_channel", channel_id, source_type="manual", auto_audit=False) as conn:
            before = conn.execute("SELECT * FROM device_channels WHERE id=? AND device_id=?", (channel_id, device_id)).fetchone()
            if before is None: raise DeviceNotFound()
            cursor = conn.execute("UPDATE device_channels SET " + ", ".join(fields) + ", actor_id=?, version=version+1 WHERE id=? AND device_id=? AND version=?", [*values, actor_id, channel_id, device_id, version])
            if cursor.rowcount != 1: raise ChannelVersionConflict()
            after = conn.execute("SELECT * FROM device_channels WHERE id=?", (channel_id,)).fetchone()
            self.audit.record(actor_id, "device.channel.configure", "device_channel", channel_id, {"before": self._channel(before), "after": self._channel(after), "reason": "operator configuration"}, conn, source_type="manual")
        return self._channel(after)
    def _row(self, device_id):
        if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id): raise DeviceValidationError("invalid device id")
        row = self.database.query_one("SELECT d.*, w.code AS well_code, w.name AS well_name, g.unit_id FROM devices d LEFT JOIN wells w ON w.id=d.well_id LEFT JOIN well_groups g ON g.id=w.group_id WHERE d.id=?", (device_id,))
        if row is None: raise DeviceNotFound()
        return row
    def _scenario(self, device_id):
        terminal = self.registry.get(device_id)
        if terminal is not None:
            state = terminal.scenario
            return {"name": state.name, "expires_at": state.expires_at.isoformat() if state.expires_at else None}
        row = self.database.query_one("SELECT name, expires_at FROM device_scenarios WHERE device_id=?", (device_id,))
        return {"name": row["name"], "expires_at": row["expires_at"]} if row else {"name": "normal", "expires_at": None}
    @staticmethod
    def _device(row): return {"id": row["id"], "well_id": row["well_id"], "well_code": row["well_code"], "well_name": row["well_name"], "unit_id": row["unit_id"], "serial_number": row["serial_number"], "device_type": row["device_type"], "firmware_version": row["firmware_version"], "status": row["status"], "connection_mode": "virtual", "hardware_connected": False}
    @staticmethod
    def _channel(row): return {"id": row["id"], "device_id": row["device_id"], "channel_code": row["channel_code"], "name": row["name"], "status": row["status"], "enabled": bool(row["enabled"]), "sample_interval_ms": row["sample_interval_ms"], "scale": row["scale"], "offset": row["offset"], "version": row["version"]}
