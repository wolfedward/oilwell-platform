"""Append-only effective-time relationships; never edit prior ownership or control hardware."""
from datetime import datetime
from uuid import UUID

from psycopg import sql

from .contracts import Change, Command
from .directory import lock_device, public_record
from .errors import RegistryError
from .maps import lock_map
from .transactions import bump, ensure_later
from .values import text, version


PERIOD_TABLES = ("device_state_periods", "well_bindings", "point_map_assignments")
_TRANSITIONS = {("pending", "active"), ("pending", "retired"), ("active", "suspended"),
                ("active", "retired"), ("suspended", "active"), ("suspended", "retired")}


def lock_target_well(cur, well_id: UUID) -> dict:
    cur.execute("SELECT id,status FROM device_registry.wells WHERE id=%s FOR UPDATE", (well_id,))
    row = cur.fetchone()
    if row is None:
        raise RegistryError("registry.not_found", "目标生产井不存在", 404)
    if row["status"] != "active":
        raise RegistryError("registry.well_archived", "目标井已归档，不能绑定设备", 409)
    return row


def lock_periods(cur, device_id: UUID) -> dict[str, list]:
    periods = {}
    for table in PERIOD_TABLES:
        cur.execute(sql.SQL("SELECT * FROM device_registry.{} WHERE device_id=%s AND valid_to IS NULL "
                            "ORDER BY valid_from,id FOR UPDATE").format(sql.Identifier(table)), (device_id,))
        periods[table] = cur.fetchall()
    return periods


def close_periods(cur, table: str, device_id: UUID, instant: datetime) -> None:
    if table not in PERIOD_TABLES:
        raise ValueError("unsupported period table")
    cur.execute(sql.SQL("SELECT valid_from FROM device_registry.{} WHERE device_id=%s AND valid_to IS NULL FOR UPDATE")
                .format(sql.Identifier(table)), (device_id,))
    ensure_later(instant, [row["valid_from"] for row in cur.fetchall()])
    cur.execute(sql.SQL("UPDATE device_registry.{} SET valid_to=%s WHERE device_id=%s AND valid_to IS NULL")
                .format(sql.Identifier(table)), (instant, device_id))


def change_state(cur, device: dict, status: str, instant: datetime, event_id: UUID) -> None:
    close_periods(cur, "device_state_periods", device["id"], instant)
    cur.execute("INSERT INTO device_registry.device_state_periods(device_id,status,valid_from,audit_event_id) VALUES (%s,%s,%s,%s)",
                (device["id"], status, instant, event_id))
    # The command's final device UPDATE persists these together with one version
    # increment. This helper never independently increments or commits.
    device["status"] = status
    if status == "active":
        device["usage_locked"] = True


def _require_approved(snapshot: dict, device: dict) -> None:
    if snapshot["status"] != "approved":
        raise RegistryError("registry.map_not_approved", "须先批准该点表版本", 409)
    if snapshot["transport"] != device["transport"]:
        raise RegistryError("registry.transport_conflict", "点表接入方式与设备登记不一致", 409)


def _require_binding(binding: dict | None) -> None:
    if binding is None:
        raise RegistryError("registry.binding_required", "须先绑定当前生产井", 409)


def _public_periods(periods: dict) -> dict:
    return {table: [public_record(row) for row in rows] for table, rows in periods.items()}


def _after_periods(cur, device_id: UUID, previous: dict) -> dict:
    periods = {}
    for table in PERIOD_TABLES:
        # Only touched/previously open and currently open rows, never the entire
        # growing history. Closed IDs remain visible in this event's after data.
        cur.execute(sql.SQL("SELECT * FROM device_registry.{} WHERE device_id=%s AND (valid_to IS NULL OR id=ANY(%s)) "
                            "ORDER BY valid_from,id").format(sql.Identifier(table)),
                    (device_id, [row["id"] for row in previous[table]]))
        periods[table] = cur.fetchall()
    return _public_periods(periods)


def apply_lifecycle(cur, command: Command, instant: datetime, event_id: UUID) -> Change:
    body = command.body
    device = lock_device(cur, command.target, body["expected_version"])
    periods = lock_periods(cur, command.target)
    binding = next(iter(periods["well_bindings"]), None)
    assignment = next(iter(periods["point_map_assignments"]), None)
    before = {"device": public_record(device), "periods": _public_periods(periods)}
    closing = set()
    next_state = device["status"]

    if command.action == "device.bind":
        target_well = lock_target_well(cur, UUID(body["well_id"]))
        if binding is not None and binding["well_id"] == target_well["id"]:
            raise RegistryError("registry.no_change", "设备已绑定到该井", 409)
        closing.update(("well_bindings", "point_map_assignments"))
        if next_state == "active":
            next_state = "suspended"
    elif command.action == "device.unbind":
        _require_binding(binding)
        closing.update(("well_bindings", "point_map_assignments"))
        if next_state == "active":
            next_state = "suspended"
    elif command.action == "map.assign":
        _require_binding(binding)
        snapshot = lock_map(cur, command.target, body["map_version"])
        _require_approved(snapshot, device)
        if assignment is not None and assignment["map_version"] == snapshot["version"]:
            raise RegistryError("registry.no_change", "当前期望点表已是此版本", 409)
        closing.add("point_map_assignments")
        if next_state == "active":
            next_state = "suspended"
    elif command.action == "device.state":
        next_state = body["status"]
        if (device["status"], next_state) not in _TRANSITIONS:
            raise RegistryError("registry.state_conflict", "当前状态不允许此转移，请刷新后核对", 409)
        if next_state == "active":
            _require_binding(binding)
            if assignment is None:
                raise RegistryError("registry.assignment_required", "须先关联已批准的期望点表", 409)
            # Approved content/review cannot be changed; head+device locks also
            # protect its current assignment. No new reference lock after T.
            cur.execute("SELECT status,transport FROM device_registry.point_map_versions WHERE device_id=%s AND version=%s",
                        (command.target, assignment["map_version"]))
            _require_approved(cur.fetchone(), device)
            text(body.get("check_note"), "check_note", minimum=2, maximum=1000)
        if next_state == "retired":
            closing.update(("well_bindings", "point_map_assignments"))
    else:
        raise RegistryError("registry.invalid_action", "不支持的状态管理操作", 422)

    if next_state != device["status"]:
        closing.add("device_state_periods")
    # Validate ALL affected starts before the first write, not table by table
    # after another history row has already been closed.
    ensure_later(instant, [row["valid_from"] for table in closing for row in periods[table]])
    for table in PERIOD_TABLES:
        if table in closing and table != "device_state_periods":
            close_periods(cur, table, command.target, instant)
    if command.action == "device.bind":
        cur.execute("INSERT INTO device_registry.well_bindings(device_id,well_id,valid_from,audit_event_id) VALUES (%s,%s,%s,%s)",
                    (command.target, body["well_id"], instant, event_id))
    elif command.action == "map.assign":
        cur.execute("INSERT INTO device_registry.point_map_assignments(device_id,map_version,valid_from,audit_event_id) VALUES (%s,%s,%s,%s)",
                    (command.target, version(body["map_version"]), instant, event_id))
    if next_state != device["status"]:
        change_state(cur, device, next_state, instant, event_id)
    if command.action == "device.state" and "check_note" in body:
        device["check_note"] = body["check_note"]
    cur.execute("UPDATE device_registry.devices SET status=%s,usage_locked=%s,check_note=%s,version=%s,updated_at=%s "
                "WHERE id=%s RETURNING *",
                (device["status"], device["usage_locked"], device["check_note"], bump(device["version"]), instant, command.target))
    entity = public_record(cur.fetchone())
    after = {"device": entity, "periods": _after_periods(cur, command.target, periods)}
    return Change("device", command.target, before, after, entity)
