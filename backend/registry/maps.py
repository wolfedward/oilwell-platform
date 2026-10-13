"""Immutable point-map content, explicit lineage and one-time human review."""
from datetime import datetime
from uuid import UUID

from psycopg.types.json import Jsonb

from .contracts import Change, Command
from .directory import lock_device, public_record
from .errors import RegistryError
from .point_maps import canonical_point_map
from .transactions import bump
from .values import version


def lock_map(cur, device_id: UUID, map_version: str) -> dict:
    cur.execute("SELECT * FROM device_registry.point_map_versions WHERE device_id=%s AND version=%s FOR UPDATE",
                (device_id, version(map_version)))
    row = cur.fetchone()
    if row is None:
        raise RegistryError("registry.not_found", "该设备的指定点表版本不存在", 404)
    return row


def map_record(device: dict, snapshot: dict) -> dict:
    # Content lives in the immutable snapshot, not duplicated in audit/receipts.
    # Never serialize the bytea via JSONB and use it to recalculate the hash.
    return public_record({"id": device["id"], "version": device["version"],
                          "map_version": snapshot["version"], "parent_version": snapshot["parent_version"],
                          **{key: snapshot[key] for key in
                             ("transport", "canonical_rule", "sha256", "status", "created_at", "reviewed_at")}}) | {
                                 "device_version": str(device["version"])}


def apply_map(cur, command: Command, instant: datetime, event_id: UUID, *, require_independent_review=False) -> Change:
    body = command.body
    device = lock_device(cur, command.target, body["expected_version"])
    if command.action == "map.create":
        cur.execute("SELECT version FROM device_registry.point_map_versions "
                    "WHERE device_id=%s ORDER BY version DESC LIMIT 1", (command.target,))
        latest = cur.fetchone()
        expected_parent = str(latest["version"]) if latest else None
        if body["parent_version"] != expected_parent:
            raise RegistryError("registry.parent_conflict", "请基于最新点表创建新版本", 409)
        new_version = bump(latest["version"]) if latest else 1
        document, raw, digest = canonical_point_map(device["transport"], body["points"])
        cur.execute("INSERT INTO device_registry.point_map_versions "
                    "(device_id,version,parent_version,transport,canonical_rule,canonical_bytes,"
                    "content,sha256,created_at,created_event_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (command.target, new_version, latest["version"] if latest else None,
                     device["transport"], document["rule"], raw, Jsonb(document), digest, instant, event_id))
        before = {}
    else:
        snapshot = lock_map(cur, command.target, body["map_version"])
        if snapshot["status"] != "draft":
            raise RegistryError("registry.review_conflict", "此点表已审核，修改须新建版本", 409)
        if snapshot["sha256"] != body["sha256"]:
            raise RegistryError("registry.snapshot_conflict", "审核摘要与保存的点表不一致，请重新打开核对", 409)
        if require_independent_review:
            cur.execute("SELECT actor FROM device_registry.audit_events WHERE id=%s AND action='map.create'",
                        (snapshot["created_event_id"],))
            creator = cur.fetchone()
            identity = creator["actor"] if creator else {}
            if not identity.get("domain") or not identity.get("user_id"):
                raise RegistryError("registry.review_identity_unavailable", "无法核实点表登记身份，禁止审核", 409)
            if (identity["domain"], str(identity["user_id"])) == (command.actor.domain, command.actor.user_id):
                raise RegistryError("registry.independent_review_required", "点表必须由另一名有审核权限的人员独立复核", 403)
        before = map_record(device, snapshot)
        cur.execute("UPDATE device_registry.point_map_versions SET status=%s,reviewed_at=%s,review_event_id=%s "
                    "WHERE device_id=%s AND version=%s RETURNING *",
                    (body["decision"], instant, event_id, command.target, version(body["map_version"])))
    snapshot = cur.fetchone()
    cur.execute("UPDATE device_registry.devices SET version=%s,updated_at=%s WHERE id=%s RETURNING *",
                (bump(device["version"]), instant, command.target))
    after = map_record(cur.fetchone(), snapshot)
    return Change("point_map", command.target, before, after, after)
