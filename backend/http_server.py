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
from backend.api import AcquisitionApi, DeviceApi, WellApi
from backend.auth import AuthError, AuthService, InvalidCredentials, PermissionDenied, SessionExpired, password_material
from backend.config import Config
from backend.database import Database
from backend.seed import seed_database

SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
MAX_JSON_BYTES = 64 * 1024


class RequestValidationError(ValueError):
    pass


class VersionConflict(RuntimeError):
    pass


class PlatformRequestHandler(BaseHTTPRequestHandler):
    server_version = "OfflinePlatform/2.0"

    def do_GET(self):
        if AcquisitionApi(self).get(self.path): return
        if DeviceApi(self).get(self.path): return
        if WellApi(self).get(self.path): return
        path = urlsplit(self.path).path
        if path == "/api/health": return self._health()
        if path == "/api/auth/me": return self._auth_me()
        if path == "/api/users" or path.startswith("/api/users/"): return self._users_get(path)
        if path == "/api/roles" or path.startswith("/api/roles/"): return self._roles_get(path)
        if path.startswith("/api/"): return self._send_api_not_found()
        self._serve_static(path)

    def do_POST(self):
        path = urlsplit(self.path).path
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
        if DeviceApi(self).patch(self.path): return
        if WellApi(self).patch(self.path): return
        path = urlsplit(self.path).path
        if path.startswith("/api/users/"): return self._users_patch(path)
        if path.startswith("/api/roles/"): return self._roles_patch(path)
        if path.startswith("/api/"): return self._send_api_not_found()
        self.send_error(HTTPStatus.NOT_FOUND, "Route not found")

    def _health(self):
        try:
            status = "ok" if self.server.database.scalar("SELECT 1") == 1 else "unavailable"
        except (sqlite3.Error, OSError):
            return self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, self._error("database_unavailable", "Database is unavailable"))
        self._send_json(HTTPStatus.OK, {"ok": True, "data": {"service": "ok", "database": status,
            "simulator": "running" if getattr(self.server.simulator, "_thread", None) and self.server.simulator._thread.is_alive() else "ready", "version": Config.VERSION, "time": datetime.now(SHANGHAI_TIMEZONE).isoformat()}})

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
        self._send_json(HTTPStatus.OK, {"ok": True, "data": self._user_data(user)})

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
        self.end_headers(); self.wfile.write(body)
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
        body = candidate.read_bytes(); self.send_response(HTTPStatus.OK); self.send_header("Content-Type", mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
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
            self.simulator.start()
        except BaseException:
            self.acquisition_worker.stop()
            raise
        return super().serve_forever(*args, **kwargs)
    def server_close(self):
        simulator = getattr(self, "simulator", None)
        if simulator: simulator.stop()
        acquisition_worker = getattr(self, "acquisition_worker", None)
        if acquisition_worker: acquisition_worker.stop()
        return super().server_close()


def create_server(host: str, port: int, database_path=None) -> ThreadingHTTPServer:
    database = Database(database_path or Config.DATABASE_PATH); database.migrate(); seed_database(database)
    server = PlatformHTTPServer((host, port), PlatformRequestHandler); server.database = database; server.auth = AuthService(database); server.audit = AuditService(database)
    from backend.simulator import SimulatorRegistry
    from backend.services.acquisition import AcquisitionService, AcquisitionWorker
    server.simulator = SimulatorRegistry(database)
    server.acquisition = AcquisitionService(database, server.simulator)
    server.acquisition_worker = AcquisitionWorker(server.acquisition, server.simulator)
    server.simulator.acquisition_worker = server.acquisition_worker
    return server
