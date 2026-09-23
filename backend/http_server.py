"""Threaded local HTTP API and static file server."""

import json
import mimetypes
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from backend.audit import AuditService
from backend.api import AcquisitionApi, AnalysisApi, CentralTelemetryApi, CommandApi, DeviceApi, ReportApi, StrategyApi, WellApi, WorkflowApi
from backend.auth import AuthError, AuthService, InvalidCredentials, PermissionDenied, SessionExpired, password_material
from backend.center.config import load_query_postgres_config
from backend.center.postgres import PostgresTelemetryQueryRepository
from backend.config import Config
from backend.database import Database
from backend.seed import seed_database
from backend.registry.api import RegistryApi
from backend.registry.runtime import build_registry_runtime

SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
MAX_JSON_BYTES = 64 * 1024


class RequestValidationError(ValueError):
    pass


class VersionConflict(RuntimeError):
    pass


class PlatformRequestHandler(BaseHTTPRequestHandler):
    server_version = "OfflinePlatform/2.0"

    def do_GET(self):
        if RegistryApi(self).get(self.path): return
        if CentralTelemetryApi(self).get(self.path): return
        if ReportApi(self).get(self.path): return
        if WorkflowApi(self).get(self.path): return
        if CommandApi(self).get(self.path): return
        if StrategyApi(self).get(self.path): return
        if AnalysisApi(self).get(self.path): return
        if AcquisitionApi(self).get(self.path): return
        if DeviceApi(self).get(self.path): return
        if WellApi(self).get(self.path): return
        path = urlsplit(self.path).path
        if path == "/api/health": return self._health()
        if path == "/api/audit": return self._audit_get()
        if path == "/api/acceptance/evidence": return self._acceptance_evidence()
        if path == "/api/auth/me": return self._auth_me()
        if path == "/api/users" or path.startswith("/api/users/"): return self._users_get(path)
        if path == "/api/roles" or path.startswith("/api/roles/"): return self._roles_get(path)
        if path.startswith("/api/"): return self._send_api_not_found()
        self._serve_static(path)

    def do_POST(self):
        if RegistryApi(self).post(self.path): return
        path = urlsplit(self.path).path
        if WorkflowApi(self).post(self.path): return
        if CommandApi(self).post(self.path): return
        if StrategyApi(self).post(self.path): return
        if AnalysisApi(self).post(self.path): return
        if AcquisitionApi(self).post(self.path): return
        if DeviceApi(self).post(self.path): return
        if path == "/api/auth/login": return self._login()
        if path == "/api/auth/logout": return self._logout()
        if path == "/api/users": return self._users_create()
        if path == "/api/roles": return self._roles_create()
        if path.startswith("/api/"): return self._send_api_not_found()
        if path != "/internal/shutdown":
            self.send_error(HTTPStatus.NOT_FOUND, "Route not found"); return
        if not self._has_shutdown_token():
            return self._send_json(HTTPStatus.FORBIDDEN, self._error("forbidden", "Invalid shutdown token"))
        self._send_json(HTTPStatus.OK, {"ok": True, "data": {"stopping": True}})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def do_PATCH(self):
        if RegistryApi(self).unsupported(self.path): return
        if StrategyApi(self).patch(self.path): return
        if DeviceApi(self).patch(self.path): return
        if WellApi(self).patch(self.path): return
        path = urlsplit(self.path).path
        if path.startswith("/api/users/"): return self._users_patch(path)
        if path.startswith("/api/roles/"): return self._roles_patch(path)
        if path.startswith("/api/"): return self._send_api_not_found()
        self.send_error(HTTPStatus.NOT_FOUND, "Route not found")

    def _unsupported_method(self):
        if RegistryApi(self).unsupported(self.path): return
        self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method")

    do_DELETE = _unsupported_method
    do_PUT = _unsupported_method
    do_OPTIONS = _unsupported_method
    do_HEAD = _unsupported_method

    def _health(self):
        try:
            status = "ok" if self.server.database.scalar("SELECT 1") == 1 else "unavailable"
        except (sqlite3.Error, OSError):
            return self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, self._error("database_unavailable", "Database is unavailable"))
        self._send_json(HTTPStatus.OK, {"ok": True, "data": {"service": "ok", "database": status,
            "simulator": "running" if getattr(self.server.simulator, "_thread", None) and self.server.simulator._thread.is_alive() else "ready", "version": Config.VERSION, "time": datetime.now(SHANGHAI_TIMEZONE).isoformat()}})

    def _audit_get(self):
        if self._require("audit.read") is None:
            return
        try:
            query = WellApi._query(
                urlsplit(self.path).query,
                {"action", "object_type", "actor_id", "from", "to", "page", "page_size"},
            )
            page = WellApi._integer(query.get("page", "1"), "page", 1, WellApi.MAX_PAGE)
            page_size = WellApi._integer(query.get("page_size", "20"), "page_size", 1, 100)
            clauses, values = [], []
            for key, column in (("action", "log.action"), ("object_type", "log.object_type")):
                if query.get(key):
                    clauses.append(f"{column}=?")
                    values.append(query[key])
            if query.get("actor_id"):
                clauses.append("log.actor_id=?")
                values.append(WellApi._integer(query["actor_id"], "actor_id", 1, 2**63 - 1))
            if query.get("from"):
                datetime.fromisoformat(query["from"])
                clauses.append("datetime(log.created_at)>=datetime(?)")
                values.append(query["from"])
            if query.get("to"):
                datetime.fromisoformat(query["to"])
                clauses.append("datetime(log.created_at)<=datetime(?)")
                values.append(query["to"])
            where = " AND ".join(clauses) if clauses else "1=1"
            total = self.server.database.scalar(
                f"SELECT COUNT(*) FROM audit_logs AS log WHERE {where}", values,
            )
            rows = self.server.database.query_all(
                f"""SELECT log.id,log.actor_id,user.display_name AS actor_name,log.action,
                            log.object_type,log.object_id,log.details_json,log.status,
                            log.source_type,log.created_at
                       FROM audit_logs AS log
                       LEFT JOIN users AS user ON user.id=log.actor_id
                      WHERE {where} ORDER BY log.id DESC LIMIT ? OFFSET ?""",
                [*values, page_size, (page - 1) * page_size],
            )
            items = []
            for row in rows:
                item = dict(row)
                try:
                    item["details"] = json.loads(item.pop("details_json"))
                except (TypeError, json.JSONDecodeError):
                    item["details"] = {}
                items.append(item)
        except (ValueError, RequestValidationError) as error:
            return self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                self._error("validation_error", str(error)),
            )
        self._send_json(HTTPStatus.OK, {"ok": True, "data": {
            "items": items, "total": total, "page": page, "page_size": page_size,
        }})

    def _acceptance_evidence(self):
        if self._require("reports.read") is None:
            return
        if urlsplit(self.path).query:
            return self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                self._error("validation_error", "acceptance evidence does not accept query parameters"),
            )
        db = self.server.database
        count = lambda sql, values=(): int(db.scalar(sql, values) or 0)
        latest = lambda sql, values=(): db.scalar(sql, values)
        requirements = [
            {
                "code": "Y1-DYNAGRAPH", "year": "第一年度",
                "title": "电功图到示功图转换与智能诊断", "route": "dynagraph-analysis",
                "evidence": [
                    {"label": "功图档案", "count": count("SELECT COUNT(*) FROM dynagraphs"), "latest_id": latest("SELECT id FROM dynagraphs ORDER BY captured_at DESC,id DESC LIMIT 1")},
                    {"label": "特征参数", "count": count("SELECT COUNT(*) FROM dynagraph_features"), "latest_id": latest("SELECT dynagraph_id FROM dynagraph_features ORDER BY id DESC LIMIT 1")},
                    {"label": "诊断结论", "count": count("SELECT COUNT(*) FROM diagnoses"), "latest_id": latest("SELECT id FROM diagnoses ORDER BY created_at DESC,id DESC LIMIT 1")},
                ],
            },
            {
                "code": "Y1-ACQUISITION", "year": "第一年度",
                "title": "可靠稳定的数字采集与数据处理", "route": "acquisition",
                "evidence": [
                    {"label": "原始报文", "count": count("SELECT COUNT(*) FROM raw_messages"), "latest_id": latest("SELECT id FROM raw_messages ORDER BY id DESC LIMIT 1")},
                    {"label": "遥测数据", "count": count("SELECT COUNT(*) FROM telemetry"), "latest_id": latest("SELECT id FROM telemetry ORDER BY measured_at DESC,id DESC LIMIT 1")},
                    {"label": "终端心跳", "count": count("SELECT COUNT(*) FROM device_heartbeats"), "latest_id": latest("SELECT id FROM device_heartbeats ORDER BY reported_at DESC,id DESC LIMIT 1")},
                ],
            },
            {
                "code": "Y1-TRANSMISSION", "year": "第一年度",
                "title": "信息传输、共享与质量闭环", "route": "data-quality",
                "evidence": [
                    {"label": "质量事件", "count": count("SELECT COUNT(*) FROM data_quality_events"), "latest_id": latest("SELECT id FROM data_quality_events ORDER BY id DESC LIMIT 1")},
                    {"label": "已修复事件", "count": count("SELECT COUNT(*) FROM data_quality_events WHERE status IN ('repaired','closed')"), "latest_id": latest("SELECT id FROM data_quality_events WHERE status IN ('repaired','closed') ORDER BY id DESC LIMIT 1")},
                ],
            },
            {
                "code": "Y1-DECISION", "year": "第一年度",
                "title": "可靠的智能决策支持", "route": "diagnosis-review",
                "evidence": [
                    {"label": "模型运行", "count": count("SELECT COUNT(*) FROM model_runs"), "latest_id": latest("SELECT id FROM model_runs ORDER BY created_at DESC,id DESC LIMIT 1")},
                    {"label": "人工复核", "count": count("SELECT COUNT(*) FROM diagnosis_reviews"), "latest_id": latest("SELECT diagnosis_id FROM diagnosis_reviews ORDER BY id DESC LIMIT 1")},
                ],
            },
            {
                "code": "Y2-ENERGY", "year": "第二年度",
                "title": "产油井自适应节能控制", "route": "strategy-center",
                "evidence": [
                    {"label": "渗流恢复试验", "count": count("SELECT COUNT(*) FROM recovery_tests WHERE status='completed'"), "latest_id": latest("SELECT id FROM recovery_tests WHERE status='completed' ORDER BY tested_at DESC,id DESC LIMIT 1")},
                    {"label": "生效策略", "count": count("SELECT COUNT(*) FROM energy_strategies WHERE status='effective'"), "latest_id": latest("SELECT id FROM energy_strategies WHERE status='effective' ORDER BY effective_at DESC,id DESC LIMIT 1")},
                    {"label": "成功控制", "count": count("SELECT COUNT(*) FROM device_commands WHERE status IN ('success','succeeded')"), "latest_id": latest("SELECT id FROM device_commands WHERE status IN ('success','succeeded') ORDER BY completed_at DESC,id DESC LIMIT 1")},
                ],
            },
            {
                "code": "Y2-DSP-MCU", "year": "第二年度",
                "title": "DSP+MCU 架构智能化抽油测控", "route": "devices",
                "evidence": [
                    {"label": "测控终端", "count": count("SELECT COUNT(*) FROM devices"), "latest_id": latest("SELECT id FROM devices ORDER BY id DESC LIMIT 1")},
                    {"label": "采集通道", "count": count("SELECT COUNT(*) FROM device_channels"), "latest_id": latest("SELECT id FROM device_channels ORDER BY id DESC LIMIT 1")},
                    {"label": "自检记录", "count": count("SELECT COUNT(*) FROM device_events WHERE event_type='self_check'"), "latest_id": latest("SELECT device_id FROM device_events WHERE event_type='self_check' ORDER BY id DESC LIMIT 1")},
                    {"label": "签名控制", "count": count("SELECT COUNT(*) FROM device_commands WHERE signature IS NOT NULL"), "latest_id": latest("SELECT id FROM device_commands WHERE signature IS NOT NULL ORDER BY completed_at DESC,id DESC LIMIT 1")},
                ],
            },
        ]
        with db.connection() as connection:
            database_status = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_status = "ok" if connection.execute("PRAGMA foreign_key_check").fetchone() is None else "failed"
        self._send_json(HTTPStatus.OK, {"ok": True, "data": {
            "generated_at": datetime.now(SHANGHAI_TIMEZONE).isoformat(),
            "requirements": requirements,
            "integrity": {"database": database_status, "foreign_keys": foreign_key_status},
        }})

    def _login(self):
        try:
            body = self._json_body()
            session = self.server.auth.login(body.get("username"), body.get("password"))
        except InvalidCredentials:
            return self._send_json(HTTPStatus.UNAUTHORIZED, self._error("invalid_credentials", "Invalid username or password"))
        except RequestValidationError as error:
            return self._send_json(HTTPStatus.BAD_REQUEST, self._error("invalid_request", str(error)))
        self._send_json(HTTPStatus.OK, {"ok": True, "data": {"token": session.token, "expires_at": session.expires_at}},
                        cookie=f"platform_session={session.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800")

    def _logout(self):
        try:
            self.server.auth.logout(self._token())
        except (AuthError, SessionExpired):
            return self._send_json(HTTPStatus.UNAUTHORIZED, self._error("unauthenticated", "Authentication required"))
        self._send_json(HTTPStatus.OK, {"ok": True, "data": {"logged_out": True}},
                        cookie="platform_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")

    def _auth_me(self):
        try:
            user = self.server.auth.me(self._token())
        except (AuthError, SessionExpired):
            return self._send_json(HTTPStatus.UNAUTHORIZED, self._error("unauthenticated", "Authentication required"))
        status = self.server.registry_runtime.status
        capability = {"enabled": status != "disabled", "status": status}
        self._send_json(HTTPStatus.OK, {"ok": True, "data": self._user_data(user) | {
            "capabilities": {"registry": capability}}})

    def _users_get(self, path):
        if self._require("users.read") is None: return
        try:
            identifier = self._path_optional_id(path, "/api/users/")
        except RequestValidationError as error:
            return self._send_json(HTTPStatus.BAD_REQUEST, self._error("validation_error", str(error)))
        if identifier is not None:
            row = self.server.database.query_one("SELECT * FROM users WHERE id = ?", (identifier,))
            if row is None: return self._send_json(HTTPStatus.NOT_FOUND, self._error("not_found", "User not found"))
            return self._send_json(HTTPStatus.OK, {"ok": True, "data": self._public_user(row)})
        rows = self.server.database.query_all("SELECT * FROM users ORDER BY username")
        self._send_json(HTTPStatus.OK, {"ok": True, "data": [self._public_user(row) for row in rows]})

    def _users_create(self):
        actor = self._require("users.manage")
        if actor is None: return
        try:
            body = self._json_body(); username = self._text(body, "username"); display_name = self._text(body, "display_name")
            password = self._password(body.get("password")); role_id = self._positive_int(body.get("role_id"), "role_id")
            status = body.get("status", "active")
            if status not in {"active", "disabled"}: raise RequestValidationError("invalid status")
            salt, digest, iterations = password_material(password)
            with self.server.database.transaction(actor.id, "user.create", "user", username, auto_audit=False) as conn:
                if conn.execute("SELECT 1 FROM roles WHERE id = ? AND status = 'active'", (role_id,)).fetchone() is None:
                    raise RequestValidationError("role_id is not active")
                cursor = conn.execute("""INSERT INTO users(role_id, username, display_name, status, password_salt, password_hash,
                    password_iterations, actor_id, source_type) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'manual')""",
                    (role_id, username, display_name, status, salt, digest, iterations, actor.id))
                created = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
                self.server.audit.record(actor.id, "user.create", "user", str(cursor.lastrowid),
                    {"before": {}, "after": self._public_user(created)}, conn)
        except RequestValidationError as error:
            return self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, self._error("validation_error", str(error)))
        except sqlite3.IntegrityError:
            return self._send_json(HTTPStatus.CONFLICT, self._error("conflict", "Username already exists"))
        self._send_json(HTTPStatus.CREATED, {"ok": True, "data": self._public_user(created)})

    def _users_patch(self, path):
        actor = self._require("users.manage")
        if actor is None: return
        try:
            target_id = self._path_id(path, "/api/users/"); body = self._json_body(); version = self._positive_int(body.get("version"), "version")
            with self.server.database.transaction(actor.id, "user.update", "user", str(target_id), auto_audit=False) as conn:
                before = conn.execute("SELECT * FROM users WHERE id = ?", (target_id,)).fetchone()
                if before is None: raise RequestValidationError("user not found")
                fields, values = [], []
                proposed_role_id, proposed_status = before["role_id"], before["status"]
                if "display_name" in body: fields.append("display_name = ?"); values.append(self._text(body, "display_name"))
                if "role_id" in body:
                    role_id = self._positive_int(body["role_id"], "role_id")
                    if conn.execute("SELECT 1 FROM roles WHERE id = ? AND status = 'active'", (role_id,)).fetchone() is None: raise RequestValidationError("role_id is not active")
                    fields.append("role_id = ?"); values.append(role_id); proposed_role_id = role_id
                if "status" in body:
                    if body["status"] not in {"active", "disabled"}: raise RequestValidationError("invalid status")
                    fields.append("status = ?"); values.append(body["status"]); proposed_status = body["status"]
                if self._would_remove_last_admin(conn, before, proposed_role_id, proposed_status):
                    raise RequestValidationError("cannot remove the last active administrator")
                reset = "password" in body
                if reset:
                    salt, digest, iterations = password_material(self._password(body["password"]))
                    fields += ["password_salt = ?", "password_hash = ?", "password_iterations = ?"]; values += [salt, digest, iterations]
                if not fields: raise RequestValidationError("no mutable fields supplied")
                cursor = conn.execute("UPDATE users SET " + ", ".join(fields) + ", version = version + 1 WHERE id = ? AND version = ?", [*values, target_id, version])
                if cursor.rowcount != 1: raise VersionConflict()
                after = conn.execute("SELECT * FROM users WHERE id = ?", (target_id,)).fetchone()
                safe_after = self._public_user(after)
                if reset: safe_after["password_reset"] = True
                self.server.audit.record(actor.id, "user.update", "user", str(target_id), {"before": self._public_user(before), "after": safe_after}, conn)
        except VersionConflict:
            return self._send_json(HTTPStatus.CONFLICT, self._error("version_conflict", "Record version is stale"))
        except RequestValidationError as error:
            code = "not_found" if str(error) == "user not found" else "validation_error"
            status = HTTPStatus.NOT_FOUND if code == "not_found" else HTTPStatus.BAD_REQUEST if str(error) == "invalid resource id" else HTTPStatus.UNPROCESSABLE_ENTITY
            return self._send_json(status, self._error(code, str(error)))
        self._send_json(HTTPStatus.OK, {"ok": True, "data": self._public_user(after)})

    def _roles_get(self, path):
        if self._require("roles.read") is None: return
        try:
            identifier = self._path_optional_id(path, "/api/roles/")
        except RequestValidationError as error:
            return self._send_json(HTTPStatus.BAD_REQUEST, self._error("validation_error", str(error)))
        if identifier is not None:
            row = self.server.database.query_one("SELECT * FROM roles WHERE id = ?", (identifier,))
            if row is None: return self._send_json(HTTPStatus.NOT_FOUND, self._error("not_found", "Role not found"))
            return self._send_json(HTTPStatus.OK, {"ok": True, "data": self._public_role(row)})
        self._send_json(HTTPStatus.OK, {"ok": True, "data": [self._public_role(row) for row in self.server.database.query_all("SELECT * FROM roles ORDER BY code")]})

    def _roles_create(self):
        actor = self._require("roles.manage")
        if actor is None: return
        try:
            body = self._json_body(); code, name = self._text(body, "code"), self._text(body, "name"); permissions = self._permissions(body.get("permissions", []))
            with self.server.database.transaction(actor.id, "role.create", "role", code, auto_audit=False) as conn:
                cursor = conn.execute("INSERT INTO roles(code, name, actor_id) VALUES(?, ?, ?)", (code, name, actor.id))
                self._set_permissions(conn, cursor.lastrowid, permissions); after = conn.execute("SELECT * FROM roles WHERE id = ?", (cursor.lastrowid,)).fetchone()
                self.server.audit.record(actor.id, "role.create", "role", str(cursor.lastrowid), {"before": {}, "after": self._public_role(after, conn)}, conn)
        except RequestValidationError as error:
            return self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, self._error("validation_error", str(error)))
        except sqlite3.IntegrityError:
            return self._send_json(HTTPStatus.CONFLICT, self._error("conflict", "Role code already exists"))
        self._send_json(HTTPStatus.CREATED, {"ok": True, "data": self._public_role(after)})

    def _roles_patch(self, path):
        actor = self._require("roles.manage")
        if actor is None: return
        try:
            role_id = self._path_id(path, "/api/roles/"); body = self._json_body(); version = self._positive_int(body.get("version"), "version")
            with self.server.database.transaction(actor.id, "role.update", "role", str(role_id), auto_audit=False) as conn:
                before = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
                if before is None: raise RequestValidationError("role not found")
                safe_before = self._public_role(before, conn)
                fields, values = [], []
                if "name" in body: fields.append("name = ?"); values.append(self._text(body, "name"))
                if "status" in body:
                    if body["status"] not in {"active", "disabled"}: raise RequestValidationError("invalid status")
                    if body["status"] == "disabled" and before["code"] == "administrator": raise RequestValidationError("administrator role cannot be disabled")
                    fields.append("status = ?"); values.append(body["status"])
                permissions = self._permissions(body["permissions"]) if "permissions" in body else None
                if before["code"] == "administrator" and permissions is not None and not {"users.manage", "roles.manage"}.issubset(permissions): raise RequestValidationError("administrator must retain users.manage and roles.manage")
                if not fields and permissions is None: raise RequestValidationError("no mutable fields supplied")
                sql = "UPDATE roles SET " + (", ".join(fields) + ", " if fields else "") + "version = version + 1 WHERE id = ? AND version = ?"
                if conn.execute(sql, [*values, role_id, version]).rowcount != 1: raise VersionConflict()
                if permissions is not None: self._set_permissions(conn, role_id, permissions)
                after = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
                self.server.audit.record(actor.id, "role.update", "role", str(role_id), {"before": safe_before, "after": self._public_role(after, conn)}, conn)
        except VersionConflict:
            return self._send_json(HTTPStatus.CONFLICT, self._error("version_conflict", "Record version is stale"))
        except RequestValidationError as error:
            code = "not_found" if str(error) == "role not found" else "validation_error"
            status = HTTPStatus.NOT_FOUND if code == "not_found" else HTTPStatus.BAD_REQUEST if str(error) == "invalid resource id" else HTTPStatus.UNPROCESSABLE_ENTITY
            return self._send_json(status, self._error(code, str(error)))
        self._send_json(HTTPStatus.OK, {"ok": True, "data": self._public_role(after)})

    def _require(self, permission):
        try: return self.server.auth.require(self._token(), permission)
        except PermissionDenied: self._send_json(HTTPStatus.FORBIDDEN, self._error("forbidden", "Permission denied"))
        except (AuthError, SessionExpired): self._send_json(HTTPStatus.UNAUTHORIZED, self._error("unauthenticated", "Authentication required"))
        return None

    def _token(self):
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer ") and authorization[7:].strip(): return authorization[7:].strip()
        for item in self.headers.get("Cookie", "").split(";"):
            name, mark, value = item.strip().partition("=")
            if mark and name == "platform_session": return value
        raise AuthError("authentication required")

    def _json_body(self):
        try: length = int(self.headers.get("Content-Length", ""))
        except ValueError: raise RequestValidationError("Content-Length is required") from None
        if length < 0 or length > MAX_JSON_BYTES: raise RequestValidationError("request body is too large")
        if "application/json" not in self.headers.get("Content-Type", "").lower(): raise RequestValidationError("Content-Type must be application/json")
        try: body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError): raise RequestValidationError("malformed JSON") from None
        if not isinstance(body, dict): raise RequestValidationError("JSON body must be an object")
        return body

    @staticmethod
    def _text(body, key):
        value = body.get(key)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120: raise RequestValidationError(f"{key} is required")
        return value.strip()
    @staticmethod
    def _positive_int(value, key):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1: raise RequestValidationError(f"{key} must be a positive integer")
        return value
    @staticmethod
    def _password(value):
        if not isinstance(value, str) or not 10 <= len(value) <= 256: raise RequestValidationError("password must be 10-256 characters")
        return value
    def _path_id(self, path, prefix):
        value = path.removeprefix(prefix)
        if not value.isdecimal() or int(value) < 1: raise RequestValidationError("invalid resource id")
        return int(value)
    def _path_optional_id(self, path, prefix):
        return None if path == prefix[:-1] else self._path_id(path, prefix)
    def _permissions(self, value):
        if not isinstance(value, list) or any(not isinstance(code, str) or not code for code in value): raise RequestValidationError("permissions must be a list of codes")
        codes = sorted(set(value))
        if codes:
            known = set(self.server.database.column("SELECT code FROM permissions WHERE code IN (" + ",".join("?" for _ in codes) + ")", codes))
            if known != set(codes): raise RequestValidationError("unknown permission")
        return codes
    @staticmethod
    def _set_permissions(conn, role_id, codes):
        conn.execute("DELETE FROM role_permissions WHERE role_id = ?", (role_id,))
        for code in codes: conn.execute("INSERT INTO role_permissions(role_id, permission_id, source_type) SELECT ?, id, 'manual' FROM permissions WHERE code = ?", (role_id, code))
    @staticmethod
    def _active_admin_count(conn):
        return conn.execute("SELECT COUNT(*) FROM users u JOIN roles r ON r.id=u.role_id WHERE r.code='administrator' AND r.status='active' AND u.status='active'").fetchone()[0]
    def _would_remove_last_admin(self, conn, row, proposed_role_id, proposed_status):
        current_role = conn.execute("SELECT code FROM roles WHERE id=?", (row["role_id"],)).fetchone()
        proposed_role = conn.execute("SELECT code FROM roles WHERE id=?", (proposed_role_id,)).fetchone()
        is_active_admin = row["status"] == "active" and current_role and current_role["code"] == "administrator"
        remains_active_admin = proposed_status == "active" and proposed_role and proposed_role["code"] == "administrator"
        return bool(is_active_admin and not remains_active_admin and self._active_admin_count(conn) <= 1)
    def _public_user(self, row):
        role = self.server.database.query_one("SELECT code FROM roles WHERE id=?", (row["role_id"],))
        return {"id": row["id"], "username": row["username"], "display_name": row["display_name"], "role_id": row["role_id"], "role_code": role["code"] if role else None, "status": row["status"], "version": row["version"]}
    @staticmethod
    def _user_data(user):
        return {"id": user.id, "username": user.username, "display_name": user.display_name, "role_id": user.role_id, "role_code": user.role_code, "permissions": sorted(user.permissions), "version": user.version}
    def _public_role(self, row, conn=None):
        sql = "SELECT p.code FROM permissions p JOIN role_permissions rp ON rp.permission_id=p.id WHERE rp.role_id=? ORDER BY p.code"
        permissions = [item[0] for item in conn.execute(sql, (row["id"],)).fetchall()] if conn else self.server.database.column(sql, (row["id"],))
        return {"id": row["id"], "code": row["code"], "name": row["name"], "status": row["status"], "version": row["version"], "permissions": permissions}
    @staticmethod
    def _error(code, message): return {"ok": False, "error": {"code": code, "message": message}}
    def _send_json(self, status, payload, cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8"); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body)))
        if cookie: self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)
    def _send_api_not_found(self): self._send_json(HTTPStatus.NOT_FOUND, self._error("not_found", "API route not found"))
    def _has_shutdown_token(self):
        return self.client_address[0] in {"127.0.0.1", "::1"} and bool(getattr(self.server, "shutdown_token", None) and self.headers.get("X-Platform-Shutdown-Token") and secrets.compare_digest(self.server.shutdown_token, self.headers["X-Platform-Shutdown-Token"]))
    def _serve_static(self, request_path):
        relative = unquote(request_path).lstrip("/") or "index.html"
        if not self._is_allowed_static_path(relative): self.send_error(HTTPStatus.NOT_FOUND, "File not found"); return
        root = Config.PROJECT_ROOT.resolve(); candidate = (root / relative).resolve(); allowed = (root / "assets").resolve() if relative.startswith("assets/") else (root / "frontend").resolve() if relative.startswith("frontend/") else root
        try: candidate.relative_to(allowed)
        except ValueError: self.send_error(HTTPStatus.NOT_FOUND, "File not found"); return
        if not candidate.is_file(): self.send_error(HTTPStatus.NOT_FOUND, "File not found"); return
        body = candidate.read_bytes(); self.send_response(HTTPStatus.OK); self.send_header("Content-Type", mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"); self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers(); self.wfile.write(body)
    @staticmethod
    def _is_allowed_static_path(relative): return relative in {"index.html", "app.js", "styles.css"} or relative.startswith(("assets/", "frontend/"))
    def log_message(self, format, *args): return


class PlatformHTTPServer(ThreadingHTTPServer):
    """Do not let a second platform process share a recently used address."""

    allow_reuse_address = False
    daemon_threads = True
    def serve_forever(self, *args, **kwargs):
        self.acquisition_worker.start()
        try:
            self.workflow_worker.start()
            self.simulator.start()
        except BaseException:
            self.workflow_worker.stop()
            self.acquisition_worker.stop()
            raise
        return super().serve_forever(*args, **kwargs)
    def server_close(self):
        try:
            simulator = getattr(self, "simulator", None)
            if simulator: simulator.stop()
            workflow_worker = getattr(self, "workflow_worker", None)
            if workflow_worker: workflow_worker.stop()
            acquisition_worker = getattr(self, "acquisition_worker", None)
            if acquisition_worker: acquisition_worker.stop()
        finally:
            # The listening socket must close even if a lifecycle component
            # reports that it could not meet its shutdown bound.
            super().server_close()


def create_server(
    host: str,
    port: int,
    database_path=None,
    *,
    central_repository=None,
    center_config_path=None,
    registry_runtime=None,
) -> ThreadingHTTPServer:
    if central_repository is not None and center_config_path is not None:
        raise ValueError("central_repository and center_config_path are mutually exclusive")
    if center_config_path is not None:
        central_repository = PostgresTelemetryQueryRepository(
            connect_kwargs=load_query_postgres_config(center_config_path),
        )
    database = Database(database_path or Config.DATABASE_PATH); database.migrate(); seed_database(database)
    server = PlatformHTTPServer((host, port), PlatformRequestHandler); server.database = database; server.auth = AuthService(database); server.audit = AuditService(database)
    server.central_telemetry = central_repository
    server.registry_runtime = registry_runtime if registry_runtime is not None else build_registry_runtime(center_config_path)
    from backend.simulator import SimulatorRegistry
    from backend.services.acquisition import AcquisitionService, AcquisitionWorker
    from backend.services.workflows import WorkflowService, WorkflowWorker
    server.simulator = SimulatorRegistry(database)
    server.acquisition = AcquisitionService(database, server.simulator)
    server.acquisition_worker = AcquisitionWorker(server.acquisition, server.simulator)
    server.workflow_worker = WorkflowWorker(WorkflowService(database, server.audit))
    server.simulator.acquisition_worker = server.acquisition_worker
    return server
