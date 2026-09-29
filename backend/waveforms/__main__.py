"""Explicit waveform components; module imports never connect or start threads."""
import argparse
import json
import signal
import threading
from .config import load_waveform_config
from .consumer import check_component, run_receive, run_assemble
from .errors import WaveformError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Isolated waveform components")
    parser.add_argument("--config", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("component", choices=("receive", "assemble", "publish"))
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = {}
    try:
        config = load_waveform_config(args.config, component=args.component)
        if config is None:
            print(json.dumps({"status": "disabled"}), flush=True)
            return 0
        if args.check:
            check_component(config)
            print(json.dumps({"status": "checked", "component": args.component}), flush=True)
            return 0
        if args.component == "publish":
            raise WaveformError("waveform.component_unavailable", "Receipt publisher is not implemented yet")
        for kind in (signal.SIGINT, signal.SIGTERM):
            previous[kind] = signal.signal(kind, lambda *_args: stop.set())
        runner = run_receive if args.component == "receive" else run_assemble
        runner(config, stop)
        return 0
    except (WaveformError, ImportError) as error:
        code = error.code if isinstance(error, WaveformError) else "waveform.dependency_unavailable"
        print(json.dumps({"status": "unavailable", "code": code}), flush=True)
        return 2
    except Exception:
        print(json.dumps({"status": "unavailable", "code": "waveform.unavailable"}), flush=True)
        return 2
    finally:
        stop.set()
        for kind, handler in previous.items():
            signal.signal(kind, handler)


if __name__ == "__main__":
    raise SystemExit(main())
