"""Standalone HTTP adapter for /api/production-studies, wired by review runtime."""
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

from backend.auth import AuthError
from backend.session_cookies import cookie_name, read_session_cookie

from .contracts import MAX_JSON_BYTES, StudyError


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("non-finite JSON number")


class StudyApi:
    def __init__(self, handler):
        self.handler = handler
        self.service = handler.server.studies

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        body = payload if type(payload) is bytes else json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                                              separators=(",", ":")).encode("utf-8")
        handler = self.handler
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)

    def _error(self, error):
        self._send(error.status, {"ok": False, "error": {"code": error.code, "message": error.message}})

    def _token(self):
        token = read_session_cookie(self.handler.headers, cookie_name(self.handler.server))
        if token is None:
            raise StudyError("unauthenticated", "Authentication required", 401)
        return token

    def _origin(self):
        values = self.handler.headers.get_all("Origin", [])
        if len(values) > 1:
            raise StudyError("forbidden_origin", "Origin not allowed", 403)
        if values:
            scheme = "https" if getattr(self.handler.server, "tls_enabled", False) else "http"
            host, port = self.handler.server.server_address[:2]
            accepted = {f"{scheme}://{host}:{port}"}
            if host == "127.0.0.1":
                accepted.add(f"{scheme}://localhost:{port}")
            if values[0] not in accepted:
                raise StudyError("forbidden_origin", "Origin not allowed", 403)

    def _body(self):
        headers = self.handler.headers
        if headers.get_all("Transfer-Encoding"):
            raise StudyError("invalid_body", "Transfer-Encoding is not supported", 400)
        lengths = headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise StudyError("invalid_body", "single Content-Length is required", 400)
        size = int(lengths[0])
        if size > MAX_JSON_BYTES:
            raise StudyError("too_large", "JSON exceeds 8 MiB", 413)
        types = headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].lower().split(";", 1)[0].strip() != "application/json":
            raise StudyError("invalid_body", "Content-Type must be application/json", 400)
        if ";" in types[0] and types[0].lower().split(";", 1)[1].strip() != "charset=utf-8":
            raise StudyError("invalid_body", "only UTF-8 JSON is supported", 400)
        self.handler.connection.settimeout(10)
        try:
            raw = self.handler.rfile.read(size)
        except TimeoutError:
            raise StudyError("invalid_body", "request body timed out", 400) from None
        if len(raw) != size:
            raise StudyError("invalid_body", "incomplete request body", 400)
        try:
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise StudyError("invalid_json", "malformed or ambiguous JSON", 400) from None
        if type(body) is not dict:
            raise StudyError("invalid_json", "JSON body must be an object", 400)
        return body

    @staticmethod
    def _page(query):
        try:
            parsed = parse_qs(query, strict_parsing=True, keep_blank_values=True)
        except ValueError:
            raise StudyError("invalid_page", "invalid query", 400) from None
        if not set(parsed) <= {"kind", "limit", "offset"} or any(len(v) != 1 for v in parsed.values()):
            raise StudyError("invalid_page", "invalid list query", 400)
        try:
            limit = int(parsed.get("limit", ["50"])[0])
            offset = int(parsed.get("offset", ["0"])[0])
        except ValueError:
            raise StudyError("invalid_page", "invalid pagination", 400) from None
        return parsed.get("kind", [None])[0], limit, offset

    def dispatch(self):
        path = urlsplit(self.handler.path)
        prefix = "/api/production-studies"
        if path.path != prefix and not path.path.startswith(prefix + "/"):
            return False
        try:
            method = self.handler.command
            if method not in ("GET", "POST"):
                raise StudyError("method_not_allowed", "Method not allowed", 405)
            if method == "POST":
                self._origin()
            token = self._token()
            tail = path.path[len(prefix):].strip("/")
            parts = tail.split("/") if tail else []
            if method == "GET" and not parts:
                kind, limit, offset = self._page(path.query)
                self._send(200, {"ok": True, "data": self.service.list(token, kind, limit, offset)})
            elif method == "POST" and not parts and not path.query:
                self._send(201, {"ok": True, "data": self.service.create(token, self._body())})
            elif method == "GET" and len(parts) == 2 and parts[0] == "requests" and not path.query:
                self._send(200, {"ok": True, "data": self.service.request(token, parts[1])})
            elif method == "GET" and len(parts) == 1 and not path.query:
                self._send(200, {"ok": True, "data": self.service.get(token, parts[0])})
            elif method == "POST" and len(parts) == 2 and parts[1] == "reviews" and not path.query:
                self._send(200, {"ok": True, "data": self.service.review(token, parts[0], self._body())})
            elif method == "POST" and len(parts) == 2 and parts[0] == "selections" and not path.query:
                self._send(200, {"ok": True, "data": self.service.select(token, parts[1], self._body())})
            elif method == "GET" and len(parts) == 2 and parts[1] == "export" and not path.query:
                data = self.service.export(token, parts[0])
                self._send(200, data, "application/json; charset=utf-8")
            else:
                raise StudyError("not_found", "Study route not found", 404)
        except StudyError as error:
            self._error(error)
        except AuthError:
            self._error(StudyError("unauthenticated", "Authentication required", 401))
        except sqlite3.Error:
            self._error(StudyError("storage_unavailable", "Study storage unavailable", 503))
        return True
