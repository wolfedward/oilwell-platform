import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
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
    return request_response(port, "GET", path)


def request_response(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response, response.read()
    finally:
        connection.close()


def wait_for_file(path, process, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise AssertionError(process.stderr.read())
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {path}")


def stop_test_process(process):
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=3)
    if process.stderr is not None:
        process.stderr.close()


def remove_legacy_pid_if_owned(process):
    pid_file = Path(__file__).resolve().parent.parent / "logs" / "platform.pid"
    if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(process.pid):
        pid_file.unlink()


class UnrelatedHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


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

    def test_unknown_api_returns_json_not_found_envelope(self):
        server, thread = start_test_server()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        response, body = get_response(server.server_port, "/api/not-implemented")

        self.assertEqual(response.status, 404)
        self.assertTrue(response.getheader("Content-Type").startswith("application/json"))
        payload = json.loads(body.decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_unknown_api_post_returns_json_not_found_envelope(self):
        server, thread = start_test_server()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        response, body = request_response(server.server_port, "POST", "/api/not-implemented")

        self.assertEqual(response.status, 404)
        self.assertTrue(response.getheader("Content-Type").startswith("application/json"))
        payload = json.loads(body.decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_git_configuration_is_not_served_as_static_content(self):
        server, thread = start_test_server()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        response, body = get_response(server.server_port, "/.git/config")

        self.assertEqual(response.status, 404)
        self.assertNotIn(b"[core]", body)

    def test_static_prefix_cannot_escape_allowed_directory(self):
        server, thread = start_test_server()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        response, body = get_response(server.server_port, "/assets/../.git/config")

        self.assertEqual(response.status, 404)
        self.assertNotIn(b"[core]", body)

    def test_allowed_asset_is_served(self):
        server, thread = start_test_server()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

        response, body = get_response(server.server_port, "/assets/heneng-logo.png")

        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("image/png"))
        self.assertGreater(len(body), 0)

    def test_stale_stop_information_does_not_stop_unrelated_server(self):
        unrelated = ThreadingHTTPServer(("127.0.0.1", 0), UnrelatedHandler)
        thread = threading.Thread(target=unrelated.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(unrelated.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(unrelated.shutdown)

        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_path = Path(temporary_directory) / "runtime.json"
            runtime_path.write_text(
                json.dumps(
                    {
                        "host": "127.0.0.1",
                        "port": unrelated.server_port,
                        "shutdown_token": "stale-token",
                    }
                ),
                encoding="utf-8",
            )
            environment = os.environ | {"PLATFORM_LOG_PATH": temporary_directory}
            project_root = Path(__file__).resolve().parent.parent
            result = subprocess.run(
                [sys.executable, "server.py", "--stop"],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            response, _ = get_response(unrelated.server_port, "/")
            self.assertEqual(response.status, 200)

    def test_stop_uses_runtime_token_to_end_its_own_server(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment = os.environ | {"PLATFORM_LOG_PATH": temporary_directory}
            project_root = Path(__file__).resolve().parent.parent
            process = subprocess.Popen(
                [sys.executable, "server.py", "--port", "0"],
                cwd=project_root,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(remove_legacy_pid_if_owned, process)
            self.addCleanup(stop_test_process, process)
            runtime_path = Path(temporary_directory) / "runtime.json"
            wait_for_file(runtime_path, process)
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            self.assertTrue(runtime["shutdown_token"])

            result = subprocess.run(
                [sys.executable, "server.py", "--stop"],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            process.wait(timeout=3)
            evidence = (Path(temporary_directory) / "runtime.log").read_text(encoding="utf-8")
            self.assertIn("started", evidence)
            self.assertIn("stopped", evidence)
