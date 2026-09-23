"""Authenticated registry boundary. No direct SQLite catalog or raw telemetry writes."""
import logging
import re
from urllib.parse import parse_qsl, urlsplit

from .contracts import Actor, MAX_JSON_BYTES, parse_json, validate_command
from .errors import RegistryError
from .permissions import required_permissions
from .runtime import RegistryRuntime
from .values import uuid_value, version


PREFIX = "/api/registry"
_PAGING = {"page", "page_size", "revision", "received_before"}
_CONTEXT = {"revision", "received_before"}
_FILTERS = {
    "wells": _PAGING | {"area_code", "well_code", "status"},
    "devices": _PAGING | {"gateway_id", "device_id", "status", "transport", "usage"},
    "device": _CONTEXT, "link_status": _CONTEXT,
    "state_history": _PAGING, "bindings": _PAGING, "point_maps": _PAGING, "map_assignments": _PAGING,
    "resolved_telemetry": _PAGING | {"gateway_id", "device_id", "well_id", "from_time", "to_time", "query_total"},
    "audit": _PAGING | {"object_id", "actor_id", "action", "from_time", "to_time"},
}
_HISTORY = {"state-history": "state_history", "bindings": "bindings", "point-maps": "point_maps",
            "point-map-assignments": "map_assignments", "link-status": "link_status"}
_DEVICE_WRITES = {"changes": "device.change", "state": "device.state", "bindings": "device.bind",
                  "unbind": "device.unbind", "point-maps": "map.create", "point-map-assignment": "map.assign"}


def _not_found():
    return RegistryError("registry.not_found", "此台账接口不存在或不支持该方法", 404)


def _query(source, resource):
    try:
        if re.search(r"%(?![0-9a-fA-F]{2})", source):
            raise ValueError()
        pairs = parse_qsl(source, keep_blank_values=True, strict_parsing=True, encoding="utf-8", errors="strict", max_num_fields=20)
        if len({key for key, _ in pairs}) != len(pairs) or any(key not in _FILTERS[resource] for key, _ in pairs):
            raise ValueError()
        return dict(pairs)
    except (ValueError, UnicodeError):
        raise RegistryError("registry.invalid_query", "查询参数重复、未知或编码无效") from None


def _route(method, path):
    parts = path[len(PREFIX):].split("/")[1:]
    target = None
    if method == "GET":
        if len(parts) == 1 and parts[0] in ("wells", "devices", "audit", "resolved-telemetry"):
            return parts[0].replace("-", "_"), None, None, path
        if len(parts) in (2, 3) and parts[0] == "devices":
            if len(parts) == 3 and parts[2] not in _HISTORY:
                raise _not_found()
            target = uuid_value(parts[1], "device_id")
            return "device" if len(parts) == 2 else _HISTORY[parts[2]], target, None, path
    elif method == "POST":
        if parts in (["wells"], ["devices"]):
            return "well.create" if parts[0] == "wells" else "device.create", None, None, path
        if len(parts) == 3 and (parts[0], parts[2]) == ("wells", "changes"):
            target = uuid_value(parts[1], "well_id")
            return "well.change", target, None, f"{PREFIX}/wells/{target}/changes"
        if len(parts) == 3 and parts[0] == "devices" and parts[2] in _DEVICE_WRITES:
            target = uuid_value(parts[1], "device_id")
            return _DEVICE_WRITES[parts[2]], target, None, f"{PREFIX}/devices/{target}/{parts[2]}"
        if len(parts) == 5 and parts[0] == "devices" and parts[2] == "point-maps" and parts[4] == "review":
            target = uuid_value(parts[1], "device_id")
            version(parts[3])
            return "map.review", target, parts[3], f"{PREFIX}/devices/{target}/point-maps/{parts[3]}/review"
    raise _not_found()


class RegistryApi:
    def __init__(self, handler):
        self.handler = handler

    def get(self, path: str) -> bool:
        return self._dispatch("GET", path)

    def post(self, path: str) -> bool:
        return self._dispatch("POST", path)

    def unsupported(self, path: str) -> bool:
        return self._dispatch("unsupported", path)

    def _body(self):
        h = self.handler
        lengths = h.headers.get_all("Content-Length", [])
        types = h.headers.get_all("Content-Type", [])
        if h.headers.get_all("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            raise RegistryError("registry.invalid_framing", "请求须使用单一有效 Content-Length")
        length = int(lengths[0])
        if length > MAX_JSON_BYTES:
            raise RegistryError("registry.body_too_large", "请求不能超过 64 KiB")
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise RegistryError("registry.invalid_content_type", "请求必须使用 application/json")
        previous = h.connection.gettimeout()
        try:
            h.connection.settimeout(3)
            raw = h.rfile.read(length)
            if len(raw) != length:
                raise RegistryError("registry.incomplete_body", "请求正文未完整接收")
            return parse_json(raw)
        except (OSError, TimeoutError):
            raise RegistryError("registry.body_timeout", "请求正文读取超时或连接中断") from None
        finally:
            h.connection.settimeout(previous)

    def _origin(self, runtime):
        headers = self.handler.headers
        origins = headers.get_all("Origin", [])
        if origins:
            if len(origins) != 1 or origins[0] not in runtime.allowed_origins:
                raise RegistryError("registry.origin_denied", "不允许此站点修改生产台账", 403)
        else:
            authorization = headers.get("Authorization", "")
            if not authorization.startswith("Bearer ") or not authorization[7:].strip():
                raise RegistryError("registry.origin_required", "无来源头的客户端须使用 Bearer 认证", 403)

    @staticmethod
    def _permit(user, action, body):
        if any(not any(permission in user.permissions for permission in group)
               for group in required_permissions(action, body)):
            raise RegistryError("registry.forbidden", "当前账号没有此项台账操作权限", 403)

    def _dispatch(self, method, path):
        parsed = urlsplit(path)
        if parsed.path != PREFIX and not parsed.path.startswith(PREFIX + "/"):
            return False
        h = self.handler
        # Authentication and the base capability always precede any repository
        # call, including receipt lookup on a retry of a committed mutation.
        user = h._require("registry.read")
        if user is None:
            if method != "GET":
                h.close_connection = True
            return True
        try:
            action, target, map_version, route = _route(method, parsed.path)
            runtime = getattr(h.server, "registry_runtime", None) or RegistryRuntime("disabled", code="registry.disabled")
            if method == "POST":
                if parsed.query or parsed.fragment:
                    raise RegistryError("registry.invalid_query", "写入接口不接受查询参数")
                body = self._body()
                self._permit(user, action, body)
                if action == "map.review":
                    if "map_version" in body:
                        raise RegistryError("registry.invalid_field", "审核版号只能由接口路径指定")
                    body = body | {"map_version": map_version}
                actor = Actor("platform-local", str(user.id), user.username, user.display_name, user.role_code)
                command = validate_command(action, target, actor, body, method="POST", route=route)
                self._origin(runtime)
            else:
                self._permit(user, action, {})
                filters = _query(parsed.query, action)
                if target is not None:
                    filters["id"] = str(target)
            if runtime.status != "ready" or runtime.service is None or runtime.query is None:
                raise RegistryError(runtime.code or "registry.unavailable", "生产台账未配置或暂不可用，请联系管理员检查", 503)
            if method == "POST":
                result = runtime.service.execute(command)
                status = 201 if action in {"well.create", "device.create", "map.create"} and not result["replayed"] else 200
            else:
                result, status = runtime.query.read(action, filters), 200
            h._send_json(status, {"ok": True, "data": result})
        except RegistryError as error:
            if method != "GET":
                h.close_connection = True
            h._send_json(error.status, h._error(error.code, error.message))
        except Exception:
            if method != "GET":
                h.close_connection = True
            logging.getLogger(__name__).warning("Registry request failed: registry.unavailable")
            h._send_json(503, h._error("registry.unavailable", "台账服务暂不可用，请稍后重试"))
        return True
