import argparse
import os
import signal
import sys
import webbrowser

from backend.config import Config
from backend.http_server import create_server


PID_FILE = Config.LOG_PATH / "platform.pid"


def parse_arguments():
    parser = argparse.ArgumentParser(description="Run the offline platform.")
    parser.add_argument("--host", default=Config.HOST)
    parser.add_argument("--port", type=int, default=Config.DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument("--stop", action="store_true")
    return parser.parse_args()


def stop_server():
    if not PID_FILE.exists():
        return 0
    try:
        process_id = int(PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(process_id, signal.SIGTERM)
    except (OSError, ValueError):
        return 1
    PID_FILE.unlink(missing_ok=True)
    return 0


def run_server(host, port, open_browser):
    Config.LOG_PATH.mkdir(parents=True, exist_ok=True)
    server = create_server(host, port)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    if open_browser:
        webbrowser.open(f"http://{host}:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        PID_FILE.unlink(missing_ok=True)


def main():
    arguments = parse_arguments()
    if arguments.stop:
        return stop_server()
    run_server(arguments.host, arguments.port, arguments.open_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
