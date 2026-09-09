import json
import mimetypes
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from backend.config import Config


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))


class PlatformRequestHandler(BaseHTTPRequestHandler):
    """Serve the platform health API."""

    server_version = "OfflinePlatform/2.0"

    def do_GET(self):
        if urlsplit(self.path).path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "data": {
                        "service": "ok",
                        "database": "pending",
                        "simulator": "pending",
                        "version": Config.VERSION,
                        "time": datetime.now(SHANGHAI_TIMEZONE).isoformat(),
                    },
                },
            )
            return
        self._serve_static(urlsplit(self.path).path)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, request_path):
        relative_path = unquote(request_path).lstrip("/") or "index.html"
        root = Config.PROJECT_ROOT.resolve()
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
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

    def log_message(self, format, *args):
        return


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    """Create a threaded HTTP server without starting its event loop."""
    return ThreadingHTTPServer((host, port), PlatformRequestHandler)
