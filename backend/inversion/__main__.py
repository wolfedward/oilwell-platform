"""Explicit independent worker; never imported to start it by HTTP."""
import argparse
from pathlib import Path
import time
from uuid import uuid4


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('worker',))
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args(argv)
    from .config import load_inversion_config
    from .contracts import canonical_bytes
    from .runtime import worker_services, worker_tick
    from .worker import WorkerRuntime
    try:
        config = load_inversion_config(args.config)
        if config is None: return 0
        encoded = canonical_bytes({'config_path': str(config.path)})
        services = worker_services(encoded)
        runtime = WorkerRuntime(worker_services, encoded, config.temp_root, uuid4(), enabled=True)
        while True:
            if not worker_tick(runtime, services.runs): time.sleep(1)
    except KeyboardInterrupt: return 0
    except Exception:
        # Never print exception repr, connection settings, or private paths.
        print('Inversion worker unavailable; no durable success assumed.')
        return 1


if __name__ == '__main__': raise SystemExit(main())
