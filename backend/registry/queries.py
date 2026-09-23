"""Read-only registry views and historical resolution in one snapshot per request."""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import re

import psycopg
from psycopg.rows import dict_row

from .directory import public_record
from .errors import RegistryError
from .resolution import assess
from .values import MAX_VERSION, TRANSPORTS, choice, fields, identifier, invalid, text, uuid_value, version


_PAGING = {"page", "page_size", "revision", "received_before"}
_CONTEXT = {"revision", "received_before"}
_ALLOWED = {
    "wells": _PAGING | {"area_code", "well_code", "status"},
    "devices": _PAGING | {"gateway_id", "device_id", "status", "transport", "usage"},
    "device": _CONTEXT | {"id"},
    "state_history": _PAGING | {"id"}, "bindings": _PAGING | {"id"},
    "point_maps": _PAGING | {"id"}, "map_assignments": _PAGING | {"id"},
    "link_status": _CONTEXT | {"id"},
    "audit": _PAGING | {"object_id", "actor_id", "action", "from_time", "to_time"},
    "resolved_telemetry": _PAGING | {"gateway_id", "device_id", "well_id", "from_time", "to_time", "query_total"},
}
_DEVICE_RESOURCES = {"device", "state_history", "bindings", "point_maps", "map_assignments", "link_status"}
_HISTORY = {"state_history": ("device_state_periods", "*", "valid_from DESC,id DESC"),
            "bindings": ("well_bindings", "*", "valid_from DESC,id DESC"),
            "map_assignments": ("point_map_assignments", "*", "valid_from DESC,id DESC"),
            "point_maps": ("point_map_versions", "device_id,version,parent_version,transport,canonical_rule,content,sha256,"
                           "status,created_at,created_event_id,reviewed_at,review_event_id", "version DESC")}
_RESOLVED_FROM = """public.telemetry_samples s
 LEFT JOIN device_registry.devices current_d ON current_d.gateway_id=s.gateway_id AND current_d.device_id=s.device_id
 LEFT JOIN device_registry.devices d ON d.gateway_id=s.gateway_id AND d.device_id=s.device_id AND d.created_at<=s.measured_at
 LEFT JOIN device_registry.device_state_periods st ON st.device_id=d.id AND tstzrange(st.valid_from,st.valid_to,'[)') @> s.measured_at
 LEFT JOIN device_registry.well_bindings b ON b.device_id=d.id AND tstzrange(b.valid_from,b.valid_to,'[)') @> s.measured_at
 LEFT JOIN device_registry.wells w ON w.id=b.well_id
 LEFT JOIN device_registry.point_map_assignments a ON a.device_id=d.id AND tstzrange(a.valid_from,a.valid_to,'[)') @> s.measured_at
 LEFT JOIN device_registry.point_map_versions m ON m.device_id=a.device_id AND m.version=a.map_version"""


def _integer(value, label, maximum):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,5}", value):
        result = int(value)
    else:
        raise invalid(label, "须为正整数")
    if not 1 <= result <= maximum:
        raise invalid(label, "超出分页范围")
    return result


def _timestamp(value, label):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("missing timezone")
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError) as error:
        raise invalid(label, "须为带时区的有效时间") from error


def _check_window(start, end):
    if start > end or end - start > timedelta(days=31):
        raise invalid("from_time/to_time", "时间不能倒置且区间不能超过 31 天")


def _filters(resource, supplied):
    if not isinstance(resource, str) or resource not in _ALLOWED:
        raise RegistryError("registry.invalid_resource", "不支持的查询类型")
    fields(supplied, _ALLOWED[resource], {"id"} if resource in _DEVICE_RESOURCES else set(), "query")
    result = dict(supplied)
    for key in ("id", "well_id", "object_id"):
        if key in result:
            result[key] = uuid_value(result[key], key)
    for key in ("gateway_id", "device_id", "area_code", "well_code", "action"):
        if key in result:
            result[key] = identifier(result[key], key)
    if "actor_id" in result:
        result["actor_id"] = text(result["actor_id"], "actor_id", minimum=1, maximum=128)
    if "status" in result:
        result["status"] = choice(result["status"], "status", ("active", "archived") if resource == "wells"
                                   else ("pending", "active", "suspended", "retired"))
    if "transport" in result:
        result["transport"] = choice(result["transport"], "transport", TRANSPORTS)
    if "usage" in result:
        result["usage"] = choice(result["usage"], "usage", ("commissioning", "production"))
    for key in ("from_time", "to_time", "received_before"):
        if key in result:
            result[key] = _timestamp(result[key], key)
    if "from_time" in result and "to_time" in result:
        _check_window(result["from_time"], result["to_time"])
    if "revision" in result:
        version(result["revision"])
    if "page" in _ALLOWED[resource]:
        result["page"] = _integer(result.get("page", 1), "page", 100001)
        result["page_size"] = _integer(result.get("page_size", 20), "page_size", 200)
        result["offset"] = (result["page"] - 1) * result["page_size"]
        if result["offset"] > 100000:
            raise invalid("page", "偏移量不能超过 100000")
        if result["page"] > 1 and not {"revision", "received_before"} <= set(result):
            raise invalid("page", "翻页须保留原修订号和接收截止时间")
    if resource == "resolved_telemetry":
        pair = {"gateway_id", "device_id"} & set(result)
        if not ((pair == {"gateway_id", "device_id"} and "well_id" not in result) or (not pair and "well_id" in result)):
            raise invalid("query", "须指定完整网关和终端标识，或指定生产井，两者不能混用")
        if result["page"] > 1 and "query_total" not in result:
            raise invalid("query_total", "核查翻页须保留首屏的记录总数")
        if "query_total" in result:
            count = result["query_total"]
            if not isinstance(count, str) or not re.fullmatch(r"0|[1-9][0-9]{0,18}", count) or int(count) > MAX_VERSION:
                raise invalid("query_total", "须为非负十进制整数字符串")
    return result


def _window(filters, cutoff):
    end = filters.get("to_time", cutoff)
    try:
        start = filters.get("from_time") or end - timedelta(hours=24)
    except OverflowError as error:
        raise invalid("from_time", "超出支持的时间范围") from error
    _check_window(start, end)
    return start, end


class RegistryQueries:
    def __init__(self, connect: Callable[[], psycopg.Connection], *, online_seconds: int,
                 delayed_seconds: int, clock_tolerance_seconds: int):
        self.connect = connect
        self.online_seconds = online_seconds
        self.delayed_seconds = delayed_seconds
        self.clock_tolerance_seconds = clock_tolerance_seconds

    def read(self, resource: str, filters: dict) -> dict:
        selected = _filters(resource, filters)  # Reject malformed filters before opening a connection.
        try:
            with self.connect() as conn:
                with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = '3s'")
                    cur.execute("SET LOCAL lock_timeout = '1s'")
                    cur.execute("SET LOCAL TIME ZONE 'UTC'")
                    cur.execute("SELECT revision,clock_timestamp() AS instant FROM device_registry.registry_head WHERE singleton=1")
                    head = cur.fetchone()
                    if head is None:
                        raise RegistryError("registry.unavailable", "台账修订记录缺失，请联系管理员", 503)
                    if "revision" in selected and selected["revision"] != str(head["revision"]):
                        raise RegistryError("registry.query_changed", "台账已变化，请刷新后重新查询", 409)
                    cutoff = selected.get("received_before", head["instant"])
                    if cutoff > head["instant"]:
                        raise invalid("received_before", "接收截止时间不能晚于数据库当前时间")
                    context = {"registry_revision": str(head["revision"]), "received_before": cutoff.isoformat(timespec="microseconds")}
                    result = self._read(cur, resource, selected, context, cutoff, head["instant"])
            return result
        except psycopg.Error as error:
            raise RegistryError("registry.unavailable", "生产台账查询暂时不可用，请稍后重试", 503) from error

    @staticmethod
    def _device(cur, device_id):
        cur.execute("SELECT * FROM device_registry.devices WHERE id=%s", (device_id,))
        device = cur.fetchone()
        if device is None:
            raise RegistryError("registry.not_found", "生产设备不存在", 404)
        return device

    @staticmethod
    def _list(cur, select, source, clauses, params, order, filters, context):
        # SQL fragments/columns come exclusively from fixed resource branches;
        # every client-provided value is passed separately as a parameter.
        where = " AND ".join(clauses) if clauses else "TRUE"
        cur.execute(f"SELECT count(*) AS total FROM {source} WHERE {where}", params)
        total = cur.fetchone()["total"]
        if "query_total" in filters and str(total) != filters["query_total"]:
            raise RegistryError("registry.query_changed", "接收数据集合已变化，请刷新后重新查询", 409)
        cur.execute(f"SELECT {select} FROM {source} WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s",
                    (*params, filters["page_size"], filters["offset"]))
        return {"items": [public_record(row) for row in cur.fetchall()], "total": total,
                "page": filters["page"], "page_size": filters["page_size"]} | context

    def _read(self, cur, resource, filters, context, cutoff, now):
        if resource in {"wells", "devices"}:
            keys = ("area_code", "well_code", "status") if resource == "wells" else ("gateway_id", "device_id", "status", "transport", "usage")
            present = [key for key in keys if key in filters]
            return self._list(cur, "*", "device_registry." + resource, [key + "=%s" for key in present],
                              [filters[key] for key in present], "created_at DESC,id DESC", filters, context)
        if resource in _DEVICE_RESOURCES:
            device = self._device(cur, filters["id"])
            if resource == "device":
                return public_record(device) | context
            if resource == "link_status":
                return self._link(cur, device, now) | context
            table, select, order = _HISTORY[resource]
            return self._list(cur, select, "device_registry." + table, ["device_id=%s"], [filters["id"]], order, filters, context)
        start, end = _window(filters, cutoff)
        if resource == "audit":
            clauses, params = ["event_at>=%s", "event_at<%s"], [start, end]
            for key, column in (("object_id", "object_id"), ("actor_id", "actor->>'user_id'"), ("action", "action")):
                if key in filters:
                    clauses.append(column + "=%s")
                    params.append(filters[key])
            return self._list(cur, "*", "device_registry.audit_events", clauses, params, "event_at DESC,id DESC", filters, context)
        return self._resolved(cur, filters, context, start, end, cutoff)

    def _link(self, cur, device, now):
        cur.execute("SELECT max(received_at) AS last_received_at FROM public.ingest_messages WHERE gateway_id=%s AND device_id=%s",
                    (device["gateway_id"], device["device_id"]))
        last = cur.fetchone()["last_received_at"]
        age = (now - last).total_seconds() if last is not None else None
        status = ("never_reported" if age is None else "clock_anomaly" if age < 0 else "online" if age <= self.online_seconds
                  else "delayed" if age <= self.delayed_seconds else "offline")
        return public_record({"id": device["id"], "gateway_id": device["gateway_id"], "device_id": device["device_id"],
                              "last_received_at": last, "age_seconds": age, "link_status": status}) | {
                                  "thresholds": {"online_seconds": self.online_seconds, "delayed_seconds": self.delayed_seconds}}

    def _resolved(self, cur, filters, context, start, end, cutoff):
        clauses = ["s.measured_at>=%s", "s.measured_at<%s", "s.received_at<=%s"]
        params = [start, end, cutoff]
        if "well_id" in filters:
            clauses.append("w.id=%s")
            params.append(filters["well_id"])
        else:
            clauses.extend(("s.gateway_id=%s", "s.device_id=%s"))
            params.extend((filters["gateway_id"], filters["device_id"]))
        select = ("s.*,d.id AS registry_device_id,current_d.status AS current_state,st.status AS sample_state,"
                  "b.id AS binding_id,w.id AS well_id,w.well_code,a.map_version AS expected_map_version,m.content")
        result = self._list(cur, select, _RESOLVED_FROM, clauses, params,
                            "s.measured_at DESC,s.message_id DESC,s.sample_index DESC", filters, context)
        resolved = []
        for sample in result["items"]:
            content = sample.pop("content")
            point = next((item for item in content["points"] if item["point_code"] == sample["point_code"]), None) if content else None
            measured, received = datetime.fromisoformat(sample["measured_at"]), datetime.fromisoformat(sample["received_at"])
            registered = sample["registry_device_id"] is not None
            clock_valid = ((measured - received).total_seconds() <= self.clock_tolerance_seconds and
                           (not registered or sample["sample_state"] is not None))
            sample["sequence"] = str(sample["sequence"])
            resolved.append(assess(sample, {"registered": registered, "bound": sample["binding_id"] is not None,
                                           "state": sample["sample_state"], "clock_valid": clock_valid,
                                           "map_version": sample["expected_map_version"], "point": point}))
        return result | {"items": resolved, "query_total": str(result["total"]),
                         "from_time": start.isoformat(timespec="microseconds"), "to_time": end.isoformat(timespec="microseconds")}
