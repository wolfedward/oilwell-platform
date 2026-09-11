import argparse
import http.client
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone

from backend.config import Config
from backend.backup import backup_database, restore_database
from backend.database import Database
from backend.http_server import create_server
from backend.lifecycle import AlreadyRunningError, InstanceLock, remove_runtime_if_owner, write_runtime


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
LAUNCH_TIMEOUT_SECONDS = 15
# Server close may finish one bounded SQLite write from each worker before it
# releases the process lock.  Ten seconds was shorter than the combined worker
# contract on a busy acceptance laptop, causing a successful shutdown to be
# reported as failed even though cleanup completed moments later.
STOP_COMPLETION_TIMEOUT_SECONDS = 30


def parse_arguments():
    parser = argparse.ArgumentParser(description="Run the offline platform.")
    parser.add_argument("--host", default=Config.HOST)
    parser.add_argument("--port", type=int, default=Config.DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--backup", action="store_true", help="create and verify a database backup")
    parser.add_argument("--restore", type=Path, metavar="BACKUP", help="restore a verified backup while the service is stopped")
    parser.add_argument("--launch", action="store_true", help="start a hidden child and wait until it is healthy")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def _runtime_paths():
    return Config.LOG_PATH / "runtime.json", Config.LOG_PATH / "runtime.log"


def backup_server():
    result = backup_database(Database(Config.DATABASE_PATH), Config.PROJECT_ROOT / "backups")
    print(json.dumps({
        "ok": True, "path": str(result.path), "integrity": result.integrity,
        "size_bytes": result.size_bytes, "created_at": result.created_at,
    }, ensure_ascii=False))
    return 0


def restore_server(source):
    lock = InstanceLock(Config.LOG_PATH)
    try:
        lock.acquire()
    except AlreadyRunningError:
        print("请先停止平台，再执行恢复。", file=sys.stderr)
        return 1
    try:
        live = Database(Config.DATABASE_PATH)
        if Config.DATABASE_PATH.is_file():
            backup_database(live, Config.PROJECT_ROOT / "backups")
        restore_database(source, Config.DATABASE_PATH)
        check = Database(Config.DATABASE_PATH)
        check.migrate()
        print(json.dumps({
            "ok": True, "restored_from": str(Path(source).resolve()),
            "integrity": check.scalar("PRAGMA integrity_check"),
        }, ensure_ascii=False))
        return 0
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"恢复失败：{error}", file=sys.stderr)
        return 1
    finally:
        lock.release()


def stop_server():
    runtime_file, _ = _runtime_paths()
    try:
        runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
        owner = (runtime["instance_id"], runtime["shutdown_token"])
        connection = http.client.HTTPConnection(_local_host(runtime["host"]), int(runtime["port"]), timeout=2)
        connection.request("POST", "/internal/shutdown", headers={"X-Platform-Shutdown-Token": runtime["shutdown_token"]})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _append_runtime_log("stop request failed")
        return 1
    if response.status == 200 and payload.get("ok") and payload.get("data", {}).get("stopping"):
        return 0 if _wait_for_shutdown_completion(runtime_file, owner) else 1
    _append_runtime_log("stop request rejected")
    return 1


def _local_host(host):
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def _append_runtime_log(event):
    _, runtime_log = _runtime_paths()
    Config.LOG_PATH.mkdir(parents=True, exist_ok=True)
    with runtime_log.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{datetime.now(SHANGHAI_TIMEZONE).isoformat()} {event}\n")


def _health_is_ready(runtime):
    try:
        connection = http.client.HTTPConnection(_local_host(runtime["host"]), int(runtime["port"]), timeout=1)
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status == 200
    except (OSError, KeyError, TypeError, ValueError):
        return False


def _read_runtime(runtime_file):
    try:
        return json.loads(runtime_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _runtime_file_exists(runtime_file):
    try:
        runtime_file.stat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _lock_is_available():
    probe = InstanceLock(Config.LOG_PATH)
    try:
        probe.acquire()
    except AlreadyRunningError:
        return False
    probe.release()
    return True


def _wait_for_shutdown_completion(runtime_file, owner):
    """Wait for the acknowledged owner to remove state and release its OS lock."""
    deadline = time.monotonic() + STOP_COMPLETION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = _read_runtime(runtime_file)
        if current is not None:
            current_owner = (current.get("instance_id"), current.get("shutdown_token"))
            if current_owner != owner:
                _append_runtime_log("stop completion conflict: runtime ownership changed")
                return False
        elif not _runtime_file_exists(runtime_file) and _lock_is_available():
            _append_runtime_log("stop completed")
            return True
        time.sleep(0.05)
    current = _read_runtime(runtime_file)
    if current is not None and (current.get("instance_id"), current.get("shutdown_token")) != owner:
        _append_runtime_log("stop completion conflict: runtime ownership changed")
    else:
        _append_runtime_log("stop completion timed out waiting for owner cleanup and lock release")
    return False


def _open_when_ready(runtime):
    if os.environ.get("PLATFORM_NO_BROWSER") != "1":
        webbrowser.open(f"http://{_local_host(runtime['host'])}:{runtime['port']}/")


def launch_server(host, port, open_browser):
    """Start a detached child and return only after that child is healthy."""
    runtime_file, runtime_log = _runtime_paths()
    before = _read_runtime(runtime_file)
    Config.LOG_PATH.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), "--child", "--host", host, "--port", str(port)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with runtime_log.open("a", encoding="utf-8") as log_file:
        child = subprocess.Popen(command, cwd=Path(__file__).resolve().parent, stdout=log_file,
                                 stderr=subprocess.STDOUT, creationflags=flags)
    deadline = time.monotonic() + LAUNCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        runtime = _read_runtime(runtime_file)
        if runtime and runtime.get("pid") == child.pid and runtime != before and _health_is_ready(runtime):
            _append_runtime_log(f"launcher ready pid={child.pid}")
            if open_browser:
                _open_when_ready(runtime)
            return 0
        status = child.poll()
        if status is not None:
            message = f"launch failed: child exited {status}; see {runtime_log}"
            _append_runtime_log(message)
            print(message, file=sys.stderr)
            return 1
        time.sleep(0.05)
    child.terminate()
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        child.kill()
    message = f"launch failed: timed out waiting for health; see {runtime_log}"
    _append_runtime_log(message)
    print(message, file=sys.stderr)
    return 1


def run_server(host, port, open_browser=False):
    """Hold a platform-wide OS lock from migration through shutdown."""
    instance_id = secrets.token_urlsafe(32)
    shutdown_token = secrets.token_urlsafe(32)
    runtime_file, _ = _runtime_paths()
    lock = InstanceLock(Config.LOG_PATH)
    server = None
    try:
        lock.acquire()
    except AlreadyRunningError:
        _append_runtime_log("platform already running")
        print("platform already running", file=sys.stderr)
        return 1
    try:
        server = create_server(host, port, database_path=Config.DATABASE_PATH)
        server.shutdown_token = shutdown_token
        runtime = {"instance_id": instance_id, "pid": os.getpid(), "host": server.server_address[0],
                   "port": server.server_port, "shutdown_token": shutdown_token}
        write_runtime(runtime_file, runtime)
        _append_runtime_log(f"started instance_id={instance_id} pid={runtime['pid']} host={runtime['host']} port={runtime['port']}")
        if open_browser:
            _open_when_ready(runtime)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    except BaseException as error:
        _append_runtime_log(f"startup failed: {error}")
        print(f"platform startup failed: {error}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
        remove_runtime_if_owner(runtime_file, instance_id, shutdown_token)
        lock.release()
        _append_runtime_log("stopped")


def main():
    arguments = parse_arguments()
    if arguments.stop:
        return stop_server()
    if arguments.backup:
        return backup_server()
    if arguments.restore:
        return restore_server(arguments.restore)
    if arguments.launch:
        return launch_server(arguments.host, arguments.port, arguments.open_browser)
    return run_server(arguments.host, arguments.port, arguments.open_browser)


if __name__ == "__main__":
    sys.exit(main())
