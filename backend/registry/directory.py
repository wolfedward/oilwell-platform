"""Registered production identities, independent of received/virtual devices."""
from datetime import datetime
from uuid import UUID, uuid4

from .contracts import Change, Command
from .errors import RegistryError
from .transactions import bump
from .values import version


def public_record(row: dict) -> dict:
    """Serialize database records without losing BIGINT or UTC precision."""
    return {key: (str(value) if isinstance(value, UUID) or
                  key in {"version", "parent_version", "map_version"} and value is not None
                  else value.isoformat(timespec="microseconds") if isinstance(value, datetime)
                  else value)
            for key, value in row.items()}


def _check_version(row: dict, expected_version: str) -> None:
    if row["version"] != version(expected_version):
        raise RegistryError("registry.version_conflict", "数据已更新，请刷新后重试", 409)
    bump(row["version"])


def lock_device(cur, device_id: UUID, expected_version: str) -> dict:
    cur.execute("SELECT * FROM device_registry.devices WHERE id=%s FOR UPDATE", (device_id,))
    row = cur.fetchone()
    if row is None:
        raise RegistryError("registry.not_found", "生产设备不存在", 404)
    if row["status"] == "retired":
        raise RegistryError("registry.retired", "设备已退役，只能查看历史", 409)
    _check_version(row, expected_version)
    return row


def lock_well(cur, well_id: UUID, expected_version: str) -> dict:
    cur.execute("SELECT * FROM device_registry.wells WHERE id=%s FOR UPDATE", (well_id,))
    row = cur.fetchone()
    if row is None:
        raise RegistryError("registry.not_found", "生产井不存在", 404)
    _check_version(row, expected_version)
    return row


def _changes(row: dict, body: dict, allowed: tuple[str, ...]) -> dict:
    changes = {key: body[key] for key in allowed if key in body and body[key] != row[key]}
    if not changes:
        raise RegistryError("registry.no_change", "没有实际变更，请刷新后核对", 409)
    return changes


def apply_well(cur, command: Command, instant: datetime, event_id: UUID) -> Change:
    body = command.body
    if command.action == "well.create":
        before = {}
        cur.execute("INSERT INTO device_registry.wells (id,area_code,well_code,name,status,version,created_at,updated_at) "
                    "VALUES (%s,%s,%s,%s,'active',1,%s,%s) RETURNING *",
                    (uuid4(), body["area_code"], body["well_code"], body["name"], instant, instant))
    else:
        row = lock_well(cur, command.target, body["expected_version"])
        changes = _changes(row, body, ("name", "status"))
        if changes.get("status") == "archived":
            cur.execute("SELECT id FROM device_registry.well_bindings WHERE well_id=%s AND valid_to IS NULL LIMIT 1",
                        (command.target,))
            if cur.fetchone() is not None:
                raise RegistryError("registry.well_in_use", "该井仍有设备绑定，请先解绑或迁井", 409)
        before = public_record(row)
        changed = row | changes
        cur.execute("UPDATE device_registry.wells SET name=%s,status=%s,version=%s,updated_at=%s WHERE id=%s RETURNING *",
                    (changed["name"], changed["status"], bump(row["version"]), instant, command.target))
    after = public_record(cur.fetchone())
    return Change("well", UUID(after["id"]), before, after, after)


def apply_device(cur, command: Command, instant: datetime, event_id: UUID) -> Change:
    body = command.body
    if command.action == "device.create":
        before = {}
        cur.execute("INSERT INTO device_registry.devices (id,gateway_id,device_id,name,manufacturer,model,serial_number,"
                    "transport,usage,status,version,created_at,updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',1,%s,%s) RETURNING *",
                    (uuid4(), body["gateway_id"], body["device_id"], body["name"], body["manufacturer"], body["model"],
                     body["serial_number"], body["transport"], body["usage"], instant, instant))
        after = public_record(cur.fetchone())
        cur.execute("INSERT INTO device_registry.device_state_periods (device_id,status,valid_from,audit_event_id) "
                    "VALUES (%s,'pending',%s,%s)", (after["id"], instant, event_id))
    else:
        row = lock_device(cur, command.target, body["expected_version"])
        changes = _changes(row, body, ("name", "manufacturer", "model", "serial_number", "check_note", "usage"))
        if "usage" in changes and (row["status"] != "pending" or row["usage_locked"]):
            raise RegistryError("registry.usage_locked", "仅首次启用前的待核对设备可以修改用途", 409)
        before = public_record(row)
        changed = row | changes
        cur.execute("UPDATE device_registry.devices SET name=%s,manufacturer=%s,model=%s,serial_number=%s,"
                    "check_note=%s,usage=%s,version=%s,updated_at=%s WHERE id=%s RETURNING *",
                    (changed["name"], changed["manufacturer"], changed["model"], changed["serial_number"],
                     changed["check_note"], changed["usage"], bump(row["version"]), instant, command.target))
        after = public_record(cur.fetchone())
    return Change("device", UUID(after["id"]), before, after, after)
