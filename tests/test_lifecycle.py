import http.client
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from backend.lifecycle import InstanceLock, remove_runtime_if_owner, write_runtime


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def wait_for_runtime(path, process, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        if process.poll() is not None:
            raise AssertionError(process.stderr.read())
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def wait_for_replaced_runtime(path, process, prior, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runtime = wait_for_runtime(path, process, timeout=0.1)
        if runtime != prior:
            return runtime
        if process.poll() is not None:
            raise AssertionError(process.stderr.read())
    raise AssertionError("timed out waiting for stale runtime replacement")


def healthy(runtime):
    connection = http.client.HTTPConnection("127.0.0.1", runtime["port"], timeout=2)
    try:
        connection.request("GET", "/api/health")
        return connection.getresponse().status == 200
    finally:
        connection.close()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.log_path = root / "logs"
        self.database_path = root / "platform.db"
        self.environment = os.environ | {
            "PLATFORM_LOG_PATH": str(self.log_path),
            "PLATFORM_DATABASE_PATH": str(self.database_path),
            "PLATFORM_NO_BROWSER": "1",
        }
        self.processes = []
        self.addCleanup(self._stop_processes)

    def _start(self, *arguments):
        process = subprocess.Popen(
            [sys.executable, "server.py", *arguments], cwd=PROJECT_ROOT, env=self.environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.processes.append(process)
        return process

    def _stop_processes(self):
        subprocess.run([sys.executable, "server.py", "--stop"], cwd=PROJECT_ROOT, env=self.environment,
                       capture_output=True, text=True, check=False)
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

    def _runtime_log(self):
        log = self.log_path / "runtime.log"
        return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""

    def test_foreground_single_instance_preserves_owner_and_restarts_after_stop(self):
        first = self._start("--port", "0")
        runtime_path = self.log_path / "runtime.json"
        owned = wait_for_runtime(runtime_path, first)
        self.assertTrue(healthy(owned))
        self.assertIn("instance_id", owned)
        self.assertIn("pid", owned)

        duplicate = subprocess.run(
            [sys.executable, "server.py", "--port", "0"], cwd=PROJECT_ROOT, env=self.environment,
            capture_output=True, text=True, timeout=12, check=False,
        )
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("already running", (duplicate.stdout + duplicate.stderr).lower())
        self.assertEqual(json.loads(runtime_path.read_text(encoding="utf-8")), owned)
        self.assertTrue(healthy(owned))

        stopped = subprocess.run([sys.executable, "server.py", "--stop"], cwd=PROJECT_ROOT,
                                 env=self.environment, capture_output=True, text=True, check=False)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        first.wait(timeout=8)
        self.assertFalse(runtime_path.exists())

        replacement = self._start("--port", "0")
        replacement_runtime = wait_for_runtime(runtime_path, replacement)
        self.assertTrue(healthy(replacement_runtime))

    def test_stop_is_a_completion_barrier_before_an_immediate_launcher_restart(self):
        """A 0 stop result must mean its owner cleaned up and released the OS lock."""
        runtime_path = self.log_path / "runtime.json"
        for _ in range(3):
            process = self._start("--port", "0")
            wait_for_runtime(runtime_path, process)
            stopped = subprocess.run(
                [sys.executable, "server.py", "--stop"], cwd=PROJECT_ROOT, env=self.environment,
                capture_output=True, text=True, timeout=12, check=False,
            )
            self.assertEqual(stopped.returncode, 0, stopped.stderr + self._runtime_log())
            self.assertFalse(runtime_path.exists())
            probe = InstanceLock(self.log_path)
            probe.acquire()
            probe.release()
            launched = subprocess.run(
                [sys.executable, "server.py", "--launch", "--port", "0"], cwd=PROJECT_ROOT,
                env=self.environment, capture_output=True, text=True, timeout=20, check=False,
            )
            self.assertEqual(launched.returncode, 0, launched.stderr)
            stopped = subprocess.run(
                [sys.executable, "server.py", "--stop"], cwd=PROJECT_ROOT, env=self.environment,
                capture_output=True, text=True, timeout=12, check=False,
            )
            self.assertEqual(stopped.returncode, 0, stopped.stderr + self._runtime_log())
            self.assertFalse(runtime_path.exists())

    def test_stale_runtime_is_replaced_and_non_owner_cannot_remove_owner_runtime(self):
        self.log_path.mkdir(parents=True)
        runtime_path = self.log_path / "runtime.json"
        stale = {"instance_id": "stale", "shutdown_token": "stale", "host": "127.0.0.1", "port": 1, "pid": 1}
        write_runtime(runtime_path, stale)
        self.assertFalse(remove_runtime_if_owner(runtime_path, "other", "other"))
        self.assertEqual(json.loads(runtime_path.read_text(encoding="utf-8")), stale)

        process = self._start("--port", "0")
        runtime = wait_for_replaced_runtime(runtime_path, process, stale)
        self.assertNotEqual(runtime["instance_id"], "stale")
        self.assertTrue(healthy(runtime))
        self.assertFalse(remove_runtime_if_owner(runtime_path, "other", "other"))
        self.assertTrue(runtime_path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows batch behavior")
    def test_batch_launch_and_stop_work_from_an_unrelated_working_directory(self):
        from server import STOP_COMPLETION_TIMEOUT_SECONDS
        with tempfile.TemporaryDirectory() as other_cwd:
            for _ in range(2):
                started = subprocess.run(
                    ["cmd", "/c", str(PROJECT_ROOT / "start-platform.cmd"), "--port", "0"], cwd=other_cwd, env=self.environment,
                    capture_output=True, text=True, timeout=20, check=False,
                )
                self.assertEqual(started.returncode, 0, started.stderr + self._runtime_log())
                runtime = json.loads((self.log_path / "runtime.json").read_text(encoding="utf-8"))
                self.assertTrue(healthy(runtime))
                stopped = subprocess.run(
                    ["cmd", "/c", str(PROJECT_ROOT / "stop-platform.cmd")], cwd=other_cwd, env=self.environment,
                    # Functional completion follows the existing server budget,
                    # plus bounded CLI startup margin; this is not a 15s SLA.
                    capture_output=True, text=True, timeout=STOP_COMPLETION_TIMEOUT_SECONDS + 5, check=False,
                )
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertFalse((self.log_path / "runtime.json").exists())

    def test_launcher_returns_failure_when_child_cannot_bind_requested_port(self):
        holder = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        thread = threading.Thread(target=holder.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(holder.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(holder.shutdown)
        result = subprocess.run(
            [sys.executable, "server.py", "--launch", "--port", str(holder.server_port)], cwd=PROJECT_ROOT,
            env=self.environment, capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed", (result.stdout + result.stderr).lower())
