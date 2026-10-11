"""Deployment-owned readiness wrapper around the unmodified worker contracts."""
import argparse
import json
from pathlib import Path
import signal
import threading
import time
from uuid import uuid4


def _event(kind, component):
    print(json.dumps({'event': kind, 'component': component}), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('component', choices=('wave_assembler', 'inversion'))
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args(argv)
    stop = threading.Event()
    for kind in (signal.SIGINT, signal.SIGTERM):
        signal.signal(kind, lambda *_: stop.set())
    try:
        if args.component == 'wave_assembler':
            from backend.waveforms.config import load_waveform_config
            from backend.waveforms.consumer import run_assemble
            config = load_waveform_config(args.config, component='assemble')
            if config is None:
                raise ValueError('assembler disabled')
            ready = False
            def observe(event, **_details):
                nonlocal ready
                if not ready and event in {'idle', 'completed', 'lease_lost'}:
                    ready = True
                    _event('review_worker_ready', args.component)
            run_assemble(config, stop, on_event=observe)
        else:
            from backend.inversion.config import load_inversion_config
            from backend.inversion.contracts import canonical_bytes
            from backend.inversion.runtime import worker_services, worker_tick
            from backend.inversion.worker import WorkerRuntime
            config = load_inversion_config(args.config)
            if config is None:
                raise ValueError('inversion disabled')
            encoded = canonical_bytes({'config_path': str(config.path)})
            services = worker_services(encoded)
            runtime = WorkerRuntime(worker_services, encoded, config.temp_root, uuid4(), enabled=True)
            ready = False
            while not stop.is_set():
                worked = worker_tick(runtime, services.runs)
                if not ready:
                    ready = True
                    _event('review_worker_ready', args.component)
                if not worked:
                    stop.wait(1)
        return 0
    except Exception:
        _event('review_worker_failed', args.component)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
