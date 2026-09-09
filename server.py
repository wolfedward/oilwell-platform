import argparse
import http.client
import json
import secrets
import sys
import webbrowser
from datetime import datetime, timedelta, timezone

from backend.config import Config
from backend.database import Database
from backend.http_server import create_server
from backend.seed import seed_database


RUNTIME_FILE = Config.LOG_PATH / "runtime.json"
RUNTIME_LOG = Config.LOG_PATH / "runtime.log"
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))


def parse_arguments():
    parser = argparse.ArgumentParser(description="Run the offline platform.")
    parser.add_argument("--host", default=Config.HOST)
    parser.add_argument("--port", type=int, default=Config.DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument("--stop", action="store_true")
    return parser.parse_args()


def stop_server():
    try:
        runtime = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
        host = runtime["host"]
        port = int(runtime["port"])
        token = runtime["shutdown_token"]
        connection = http.client.HTTPConnection(_local_host(host), port, timeout=2)
        connection.request("POST", "/internal/shutdown", headers={"X-Platform-Shutdown-Token": token})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _append_runtime_log("stop request failed")
        return 1
    if response.status == 200 and payload.get("ok") and payload.get("data", {}).get("stopping"):
        return 0
    _append_runtime_log("stop request rejected")
    return 1


def _local_host(host):
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def _append_runtime_log(event):
    Config.LOG_PATH.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(SHANGHAI_TIMEZONE).isoformat()
    with RUNTIME_LOG.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} {event}\n")


def run_server(host, port, open_browser):
    Config.LOG_PATH.mkdir(parents=True, exist_ok=True)
    database = Database(Config.DATABASE_PATH)
    database.migrate()
    seed_database(database)
    server = create_server(host, port)
    server.shutdown_token = secrets.token_urlsafe(32)
    runtime = {
        "host": server.server_address[0],
        "port": server.server_port,
        "shutdown_token": server.shutdown_token,
    }
    RUNTIME_FILE.write_text(json.dumps(runtime), encoding="utf-8")
    _append_runtime_log(f"started host={runtime['host']} port={runtime['port']}")
    if open_browser:
        webbrowser.open(f"http://{host}:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        RUNTIME_FILE.unlink(missing_ok=True)
        _append_runtime_log("stopped")


def main():
    arguments = parse_arguments()
    if arguments.stop:
        return stop_server()
    run_server(arguments.host, arguments.port, arguments.open_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
