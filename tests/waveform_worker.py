"""Main-guarded owned test worker with explicit stop over its private stdin."""
import argparse
import json
import sys
import threading
from backend.waveforms.config import load_waveform_config
from backend.waveforms.consumer import check_component, run_receive, run_assemble
from backend.waveforms.publisher import run_publish
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
    connection_pools=[]
    def commands():
        for line in sys.stdin:
            if line.strip() == "stop":
                stop.set()
                release.set()
                return
            if line.strip() == "release": release.set()
        stop.set()
    try:
        if args.component=='resource_measure':
            from tests.waveform_resource_scenarios import run_resource_worker
            threading.Thread(target=commands,daemon=True).start()
            return run_resource_worker(args.config,stop,emit)
        if args.component=='http_measure':
            from tests.waveform_measurements import run_http_worker
            threading.Thread(target=commands,daemon=True).start()
            return run_http_worker(args.config,stop,emit,fault=args.fault)
        if args.component=='sender_measure':
            from tests.waveform_measurements import run_sender_worker
            threading.Thread(target=commands,daemon=True).start()
            return run_sender_worker(args.config,stop,emit)
        config = load_waveform_config(args.config, component=args.component)
        if args.check:
            check_component(config)
            emit("checked")
            return 0
        if args.fault in ('pause_before_commit','pause_after_commit'):
            from backend.waveforms.repository import WaveformRepository
            original_commit=WaveformRepository.commit_pending
            def paused_commit(self,connection,pending):
                result=None
                if args.fault=='pause_after_commit': result=original_commit(self,connection,pending)
                emit('transaction_paused',phase=args.fault)
                if not release.wait(30): raise WaveformError('waveform.test_timeout','Owned transaction pause timed out')
                return result if args.fault=='pause_after_commit' else original_commit(self,connection,pending)
            WaveformRepository.commit_pending=paused_commit
        elif args.fault=='clock_rollback':
            from datetime import datetime,timedelta,timezone
            from backend.waveforms.assembly import Assembler
            original_init=Assembler.__init__
            def past_clock(self,connect,*,clock,limits):
                original_init(self,connect,clock=lambda:datetime.now(timezone.utc)-timedelta(seconds=60),limits=limits)
            Assembler.__init__=past_clock
        elif args.fault=='drop_publish_ack':
            from backend.waveforms import publisher
            original_client=publisher.create_client
            def drop_ack(gateway):
                client=original_client(gateway); original_send=client.publish
                def send(*a,**kw):
                    client.on_publish=lambda *_:emit('publish_ack_dropped')
                    return original_send(*a,**kw)
                client.publish=send; return client
            publisher.create_client=drop_ack
        elif args.fault == "pause_deliveries":
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
        elif args.fault == "pause_publish_finish":
            from backend.waveforms.outbox import WaveOutbox
            original_finish = WaveOutbox.finish
            def paused_finish(self, lease, **kwargs):
                if kwargs['delivered']:
                    emit('publisher_finish_paused', receipt_id=str(lease.receipt_id))
                    if not release.wait(30):
                        raise WaveformError('waveform.test_timeout', 'Owned publisher pause timed out')
                return original_finish(self, lease, **kwargs)
            WaveOutbox.finish = paused_finish
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
            original_renew,original_run=Assembler.renew,Assembler.run
            barrier, depth = threading.Barrier(2), threading.local()
            def measured(phase,call,*args,**kwargs):
                began=monotonic(); worker=threading.current_thread().name
                lease=args[0] if args and hasattr(args[0],'token') else None
                emit('lease_activity_started',phase=phase,worker=worker,monotonic=began,
                     token=str(lease.token) if lease else None)
                result=call(*args,**kwargs)
                emit('lease_activity_complete',phase=phase,worker=worker,monotonic=monotonic(),
                     elapsed_seconds=monotonic()-began,succeeded=result is not None,
                     token=str(result.token) if hasattr(result,'token') else str(lease.token) if lease else None)
                return result
            def claim(self, *args, **kwargs):
                lease = measured('claim',lambda *a,**k:original_claim(self,*a,**k),*args,**kwargs)
                return replace(lease, _deadline=monotonic() + 5) if lease else None
            def renew(self,lease): return measured('renew',lambda value:original_renew(self,value),lease)
            def run(self,lease): return measured('processing',lambda value:original_run(self,value),lease)
            def factory(self, role):
                if self not in connection_pools: connection_pools.append(self)
                original_connect = original_factory(self, role)
                @contextmanager
                def connect():
                    depth.value = getattr(depth, "value", 0) + 1
                    try:
                        with original_connect() as connection:
                            if depth.value == 2:
                                barrier.wait(timeout=5)
                                emit("nested_renewal_connection", active=self.active_count,monotonic=monotonic(),worker=threading.current_thread().name)
                            yield connection
                    finally: depth.value -= 1
                return connect
            Assembler.claim, DatabaseConnections.factory = claim, factory
            Assembler.renew,Assembler.run=renew,run
        threading.Thread(target=commands, daemon=True).start()
        runner = {'receive': run_receive, 'assemble': run_assemble, 'publish': run_publish}[args.component]
        runner(config, stop, on_event=emit)
        emit("stopped", remaining_threads=[thread.name for thread in threading.enumerate()
                                          if thread.name.startswith("waveform-")],
             **({'connections':sum(pool.active_count for pool in connection_pools),'monotonic':__import__('time').monotonic()} if args.fault=='concurrent_renewals' else {}))
        return 0
    except WaveformError as error:
        emit("unavailable", code=error.code)
        return 2
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
