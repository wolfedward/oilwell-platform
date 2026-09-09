import json
import mimetypes
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from backend.config import Config
from backend.database import Database


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))


class PlatformRequestHandler(BaseHTTPRequestHandler):
    """Serve the platform health API."""

    server_version = "OfflinePlatform/2.0"

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/health":
            try:
                database_status = "ok" if self.server.database.scalar("SELECT 1") == 1 else "unavailable"
            except sqlite3.Error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "ok": False,
                        "error": {
                            "code": "database_unavailable",
                            "message": "Database is unavailable",
                        },
                    },
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "data": {
                        "service": "ok",
                        "database": database_status,
                        "simulator": "pending",
                        "version": Config.VERSION,
                        "time": datetime.now(SHANGHAI_TIMEZONE).isoformat(),
                    },
                },
            )
            return
        if path.startswith("/api/"):
            self._send_api_not_found()
            return
        self._serve_static(path)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            self._send_api_not_found()
            return
        if path != "/internal/shutdown":
            self.send_error(HTTPStatus.NOT_FOUND, "Route not found")
            return
        if not self._has_shutdown_token():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error": {"code": "forbidden", "message": "Invalid shutdown token"}},
            )
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "data": {"stopping": True}})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_api_not_found(self):
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "error": {"code": "not_found", "message": "API route not found"}},
        )

    def _has_shutdown_token(self):
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            return False
        expected = getattr(self.server, "shutdown_token", None)
        received = self.headers.get("X-Platform-Shutdown-Token")
        return bool(expected and received and secrets.compare_digest(expected, received))

    def _serve_static(self, request_path):
        relative_path = unquote(request_path).lstrip("/") or "index.html"
        if not self._is_allowed_static_path(relative_path):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        root = Config.PROJECT_ROOT.resolve()
        candidate = (root / relative_path).resolve()
        allowed_root = root
        if relative_path.startswith("assets/"):
            allowed_root = (root / "assets").resolve()
        elif relative_path.startswith("frontend/"):
            allowed_root = (root / "frontend").resolve()
        try:
            candidate.relative_to(allowed_root)
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _is_allowed_static_path(relative_path):
        return relative_path in {"index.html", "app.js", "styles.css"} or relative_path.startswith(
            ("assets/", "frontend/")
        )

    def log_message(self, format, *args):
        return


def create_server(host: str, port: int, database_path=None) -> ThreadingHTTPServer:
    """Create a threaded HTTP server without starting its event loop."""
    database = Database(database_path or Config.DATABASE_PATH)
    database.migrate()
    server = ThreadingHTTPServer((host, port), PlatformRequestHandler)
    server.database = database
    return server
