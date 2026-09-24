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
from backend.ingest_reliability.errors import ReliabilityError


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
            confirmation_mode=config.confirmation_mode,
            queue_checked=False,
            network_checked=False,
        )
        return 0

    try:
        runtime = GatewayRuntime(config)
    except ReliabilityError as error:
        _write_event('gateway_startup_error', error_code=error.code)
        return 2
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    exit_code = 0
    try:
        while not stop.is_set():
            started = time.monotonic()
            result = runtime.run_cycle()
            body = asdict(result)
            body["poll_errors"] = [asdict(error) for error in result.poll_errors]
            mode = getattr(config, 'confirmation_mode', 'broker')
            body['confirmation_mode'] = mode
            body['confirmation_scope'] = 'center_application' if mode == 'application' else 'broker_only'
            if runtime.reliable_mode:
                if args.once:
                    body['drain_pass_completed'] = runtime.uplink.wait_once()
                snapshot = runtime.uplink.snapshot()
                body['messages_published'] += snapshot['broker_published_since_last_snapshot']
                body['uplink_error'] = snapshot['error_code'] or body['uplink_error']
                counts = snapshot['spool']
                body['queue_status'] = snapshot['queue_status']
            else:
                counts = runtime.spool.counts()
            # Exit code zero only means this bounded cycle ran. Quarantine,
            # dead records and unconfirmed data never count as business success.
            body['delivery_complete'] = not any(counts.get(state, 0) for state in
                                                ('pending', 'inflight', 'awaiting_receipt', 'quarantined', 'dead'))
            body['center_confirmation_enabled'] = mode == 'application'
            _write_event("gateway_cycle", **body, spool=counts)
            if args.once:
                break
            remaining = max(0.0, config.poll_interval_seconds - (time.monotonic() - started))
            stop.wait(remaining)
    except ReliabilityError as error:
        _write_event('gateway_runtime_error', error_code=error.code)
        exit_code = 3
    finally:
        try:
            runtime.close()
        except ReliabilityError as error:
            _write_event('gateway_shutdown_error', error_code=error.code)
            exit_code = 3
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
