"""Command-line entry point for the Windows/Linux edge gateway process."""

import argparse
from dataclasses import asdict
import json
import signal
import sys
import threading
import time

from .config import GatewayConfigurationError, load_gateway_config
from .runtime import GatewayRuntime


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description="Oil-well Modbus to MQTT edge gateway")
    parser.add_argument("--config", required=True, help="path to gateway.local.toml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate configuration without connecting")
    mode.add_argument("--once", action="store_true", help="run one acquisition/uplink cycle")
    return parser.parse_args(argv)


def _write_event(event: str, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def main(argv=None):
    args = _arguments(argv)
    try:
        config = load_gateway_config(args.config)
    except GatewayConfigurationError as error:
        _write_event("configuration_error", error=str(error))
        return 2
    if args.check:
        _write_event(
            "configuration_valid",
            gateway_id=config.gateway_id,
            modbus_devices=len(config.devices),
            mqtt_host=config.mqtt.host,
            mqtt_port=config.mqtt.port,
        )
        return 0

    runtime = GatewayRuntime(config)
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    try:
        while not stop.is_set():
            started = time.monotonic()
            result = runtime.run_cycle()
            body = asdict(result)
            body["poll_errors"] = [asdict(error) for error in result.poll_errors]
            _write_event("gateway_cycle", **body, spool=runtime.spool.counts())
            if args.once:
                break
            remaining = max(0.0, config.poll_interval_seconds - (time.monotonic() - started))
            stop.wait(remaining)
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
