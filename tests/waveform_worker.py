"""Main-guarded owned test worker with explicit stop over its private stdin."""
import argparse
import json
import sys
import threading
from backend.waveforms.config import load_waveform_config
from backend.waveforms.consumer import check_component, run_receive, run_assemble
from backend.waveforms.errors import WaveformError

_output_lock = threading.Lock()


def emit(event, **details):
    with _output_lock:
        print(json.dumps({"event": event, **details}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("component")
    parser.add_argument("--config", required=True)
    parser.add_argument("--fault", default="")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stop = threading.Event()
    release = threading.Event()
    def commands():
        for line in sys.stdin:
            if line.strip() == "stop":
                stop.set()
                release.set()
                return
            if line.strip() == "release": release.set()
        stop.set()
    try:
        config = load_waveform_config(args.config, component=args.component)
        if args.check:
            check_component(config)
            emit("checked")
            return 0
        if args.fault == "pause_deliveries":
            from backend.waveforms import consumer
            create, handle = consumer.create_client, consumer.WaveformConsumer.handle_message
            generation, sequence = 0, 0
            def create_traced(gateway):
                nonlocal generation
                client = create(gateway)
                generation += 1
                client.test_generation = generation
                original_ack = client.ack
                def ack(mid, qos):
                    result = original_ack(mid, qos)
                    emit("ack", mid=mid, client_generation=client.test_generation)
                    return result
                client.ack = ack
                return client
            def paused(self, session, message):
                nonlocal sequence
                sequence += 1
                release.clear()
                emit("delivery_paused", sequence=sequence, mid=message.mid,
                     client_generation=session.client.test_generation)
                if not release.wait(30):
                    raise WaveformError("waveform.test_timeout", "Owned test delivery wait timed out")
                return handle(self, session, message)
            consumer.create_client, consumer.WaveformConsumer.handle_message = create_traced, paused
        elif args.fault == "query_in_ingest":
            from backend.waveforms.repository import WaveformRepository
            original = WaveformRepository._store
            def slow(self, cursor, observation, candidate):
                emit("query_started")
                cursor.execute("SELECT pg_sleep(30)")
                return original(self, cursor, observation, candidate)
            WaveformRepository._store = slow
        elif args.fault == "pause_assembly":
            from backend.waveforms.assembly import Assembler
            original = Assembler._read_fact
            paused_once = False
            def paused_read(self, connection, fact_id):
                nonlocal paused_once
                result = original(self, connection, fact_id)
                if not paused_once:
                    paused_once = True
                    emit("assembly_paused")
                    stop.wait(20)
                return result
            Assembler._read_fact = paused_read
        elif args.fault == "concurrent_renewals":
            from contextlib import contextmanager
            from dataclasses import replace
            from time import monotonic
            from backend.waveforms.assembly import Assembler
            from backend.waveforms.consumer import DatabaseConnections
            original_claim, original_factory = Assembler.claim, DatabaseConnections.factory
            barrier, depth = threading.Barrier(2), threading.local()
            def claim(self, *args, **kwargs):
                lease = original_claim(self, *args, **kwargs)
                return replace(lease, _deadline=monotonic() + 5) if lease else None
            def factory(self, role):
                original_connect = original_factory(self, role)
                @contextmanager
                def connect():
                    depth.value = getattr(depth, "value", 0) + 1
                    try:
                        with original_connect() as connection:
                            if depth.value == 2:
                                barrier.wait(timeout=5)
                                emit("nested_renewal_connection", active=self.active_count)
                            yield connection
                    finally: depth.value -= 1
                return connect
            Assembler.claim, DatabaseConnections.factory = claim, factory
        threading.Thread(target=commands, daemon=True).start()
        runner = run_receive if args.component == "receive" else run_assemble
        runner(config, stop, on_event=emit)
        emit("stopped", remaining_threads=[thread.name for thread in threading.enumerate()
                                          if thread.name.startswith("waveform-")])
        return 0
    except WaveformError as error:
        emit("unavailable", code=error.code)
        return 2
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
