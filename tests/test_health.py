import http.client
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest

from backend.http_server import create_server


def start_test_server():
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def get_json(port, path):
    response, body = get_response(port, path)
    return json.loads(body.decode("utf-8"))


def get_response(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response, response.read()
    finally:
        connection.close()


class HealthTests(unittest.TestCase):
    def test_health_returns_version_and_timezone(self):
        server, thread = start_test_server()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        payload = get_json(server.server_port, "/api/health")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["version"], "2.0.0")
        self.assertTrue(payload["data"]["time"].endswith("+08:00"))

    def test_root_serves_existing_html_asset(self):
        server, thread = start_test_server()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        response, body = get_response(server.server_port, "/")

        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/html"))
        self.assertGreater(len(body), 0)

    def test_server_help_exposes_runtime_options(self):
        project_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "server.py", "--help"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--host", result.stdout)
        self.assertIn("--port", result.stdout)
        self.assertIn("--open", result.stdout)
        self.assertIn("--stop", result.stdout)
