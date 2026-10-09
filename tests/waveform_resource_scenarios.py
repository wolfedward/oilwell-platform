"""Bounded high-level resource workloads, separate from worker entrypoints."""
from pathlib import Path
import time
from tests.waveform_measurements import process_memory,current_process_memory,host_resources,safety_check


class MeasurementEvidence:
    """One small owned-phase journal; outer scope finalizes AFTER Stack cleanup."""
    def __init__(self,root,kind,metadata):
        import json,threading
        from uuid import uuid4
        self.json=json; self.lock=threading.Lock(); self.stack=None; self.cursors={}
        self.root=Path(root)/('measurement-'+uuid4().hex); self.root.mkdir(exist_ok=False)
        self.result=dict(kind=kind,status='partial',metadata=metadata,envelope_path=str(self.root/'result.json'),
            samples_path=str(self.root/'samples.jsonl'),phases_path=str(self.root/'phases.jsonl'))
        self.samples=(self.root/'samples.jsonl').open('x',encoding='utf-8')
        self.phases=(self.root/'phases.jsonl').open('x',encoding='utf-8'); self.began=time.monotonic()
        with (self.root/'metadata.json').open('x',encoding='utf-8') as output: json.dump(self.result,output,indent=2)

    def sample(self,value):
        with self.lock: self.samples.write(self.json.dumps(value)+'\n'); self.samples.flush()

    def events(self,component):
        events=self.stack.events(component)
        with self.lock:
            for event in events[self.cursors.get(component,0):]:
                self.phases.write(self.json.dumps(dict(component=component,**event))+'\n')
            self.phases.flush(); self.cursors[component]=len(events)
        self.result['events']=events
        return events

    def complete(self,value): self.result.update(value,status='passed'); return self.result
    def __enter__(self): return self
    def __exit__(self,kind,error,_traceback):
        import re
        first=error
        def attempt(action):
            nonlocal first
            try: action()
            except BaseException as failure:
                if first is None: first=failure
                else: first.add_note('Measurement retention failure: '+type(failure).__name__)
        attempt(self.samples.close); attempt(self.phases.close)
        if first is not None:
            self.result.update(status='partial',error_type=type(first).__name__,reason=str(first) if re.fullmatch(r'safety\.[a-z_]{1,80}',str(first)) else 'error.'+type(first).__name__)
            first.add_note('Measurement envelope: '+self.result['envelope_path'])
        processes=[]
        if self.stack:
            for item in [*self.stack.instances,*(self.stack.broker.instances if self.stack.broker else [])]:
                processes.append(dict(instance_id=item['instance_id'],component=item['component'],pid=item['pid'],exit_code=item['process'].poll()))
        self.result['owned_cleanup']=dict(complete=all(p['exit_code'] is not None for p in processes),processes=processes)
        self.result['total_elapsed_seconds']=time.monotonic()-self.began
        def save():
            with (self.root/'result.json').open('x',encoding='utf-8') as output: self.json.dump(self.result,output,indent=2)
        attempt(save)
        if first is not None and error is None: raise first


def storage_sample(fixture,root):
    return dict(database_bytes=fixture.scalar('SELECT pg_database_size(current_database())'),
        artifact_bytes=sum(p.stat().st_size for p in Path(root).rglob('*') if p.is_file()),
        logical_raw_bytes=fixture.scalar("SELECT used_raw_bytes FROM waveforms.capacity_metadata WHERE bucket='*'"))


def storage_check(before,after):
    if after['database_bytes']-before['database_bytes']>2*1024**3: raise RuntimeError('safety.database_growth')
    if after['artifact_bytes']>8*1024**3: raise RuntimeError('safety.artifact_growth')


def measure_leases(fixture,root,*,run_id,source_sha256):
    """Fresh bounded supplement; historical kill/reclaim is not stale-token proof."""
    import json,re
    from contextlib import contextmanager
    from dataclasses import asdict,replace
    from datetime import timedelta
    from uuid import uuid4
    from backend.waveforms.assembly import Assembler
    from backend.waveforms.config import CapacityLimits
    from backend.waveforms.repository import WaveformRepository
    from tests.waveform_support import FIXED_TIME,LIMITS
    from tests.test_waveform_repository import capture,observation
    if not re.fullmatch('[0-9a-f]{32}',run_id) or not re.fullmatch('[0-9a-f]{64}',source_sha256):
        raise ValueError('Explicit lease measurement identity required')
    root=Path(root); began=time.monotonic(); now=FIXED_TIME; trace=None; dual_started=False
    result=dict(schema='waveform.lease-measurement/1',run_id=run_id,source_sha256=source_sha256,
        status='partial',assertions={},timings=[],plans=[],identities={},
        limits=['66 actual jobs only; injected UTC expiry/rollback, local 5s renewal deadline and two-worker barrier; not natural five-minute endurance'])
    owned_connections=len(fixture.connections)
    def timed(name,operation):
        safety_check(host_resources(root)); start=time.monotonic()
        try: return operation()
        finally:
            elapsed=time.monotonic()-start; result['timings'].append(dict(operation=name,elapsed_seconds=elapsed))
            if elapsed>120: raise RuntimeError('safety.operation_deadline')
    def require(name,condition):
        result['assertions'][name]=condition is True
        if not condition: raise AssertionError('Lease measurement: '+name)
    @contextmanager
    def traced_connect():
        with fixture.connect('assembler') as connection:
            class Cursor:
                def __init__(self,actual): self.actual=actual
                def __getattr__(self,name): return getattr(self.actual,name)
                def execute(self,query,params=()):
                    if trace and isinstance(query,str) and query.startswith('SELECT capture_pk FROM waveforms.assembly_jobs WHERE intake_gateway_id=%s') and 'FOR UPDATE' not in query and 'ORDER BY capture_pk' not in query:
                        start=time.monotonic()
                        with connection.cursor() as explain:
                            explain.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+query,params)
                            plan=explain.fetchone()[0][0]
                        result['plans'].append(dict(cardinality=trace[0],state=trace[1],query=query,
                            params=[p.isoformat() if hasattr(p,'isoformat') else p for p in params],
                            client_seconds=time.monotonic()-start,plan=plan))
                    return self.actual.execute(query,params)
            class Connection:
                def __getattr__(self,name): return getattr(connection,name)
                def cursor(self): return Cursor(connection.cursor())
            yield Connection()
    def count_owned():
        return fixture.scalar("SELECT count(*) FROM waveforms.assembly_jobs WHERE lease_token IS NOT NULL",role='assembler')
    try:
        quota=replace(LIMITS.gateways['GW'],raw_bytes=33554432*70,incomplete_captures=70)
        limits=CapacityLimits(global_quota=replace(LIMITS.global_quota,raw_bytes=33554432*140,incomplete_captures=140),gateways={'GW':quota})
        result['fixture_capacity']=dict(global_quota=asdict(limits.global_quota),gateways={k:asdict(v) for k,v in limits.gateways.items()})
        with fixture.capacity_fixture(limits):
            repo=WaveformRepository(fixture.connect,limits=limits,prefix='oil',ingress_gateway_id='GW')
            def insert(number):
                with fixture.connect() as conn:
                    pending=[]
                    for _ in range(number):
                        for payload in capture(): pending.append(repo.store_in_transaction(conn.cursor(),observation(payload,transport='file')))
                    repo.commit_pending(conn,pending)
            assembler=Assembler(traced_connect,clock=lambda:now,limits=limits)
            timed('populate-1',lambda:insert(1)); trace=(1,'available')
            old=[timed('claim-1',lambda:assembler.claim(uuid4(),gateway_id='GW'))]
            require('one_claim',old[0] is not None)
            trace=None; timed('populate-64',lambda:insert(63)); trace=(64,'available')
            old.append(timed('claim-64-sample',lambda:assembler.claim(uuid4(),gateway_id='GW'))); trace=None
            for _ in range(62): old.append(timed('claim-to-ceiling',lambda:assembler.claim(uuid4(),gateway_id='GW')))
            require('owned_64',all(lease is not None for lease in old) and count_owned()==64)
            timed('populate-66',lambda:insert(2)); trace=(66,'full-live')
            require('full_unowned_refused',timed('full-claim',lambda:assembler.claim(uuid4(),gateway_id='GW')) is None)
            result['identities']['old']=[json.loads(json.dumps(asdict(lease),default=str)) for lease in old]
            now+=timedelta(seconds=31); trace=(66,'expired-still-owned')
            require('expired_owned_64',count_owned()==64)
            replacement=timed('expired-reclaim',lambda:assembler.claim(uuid4(),gateway_id='GW')); trace=None
            require('reclaimed_owned_only',replacement is not None and count_owned()==64 and replacement.capture_pk in {lease.capture_pk for lease in old})
            previous=next(lease for lease in old if lease.capture_pk==replacement.capture_pk)
            result['identities']['replacement']=json.loads(json.dumps(asdict(replacement),default=str))
            require('token_version_changed',replacement.token!=previous.token and replacement.version>previous.version and replacement.generation==previous.generation)
            before=fixture.scalar('SELECT count(*) FROM waveforms.completion',role='assembler')
            require('stale_renew_refused',timed('stale-renew',lambda:assembler.renew(previous)) is None)
            require('stale_run_refused',timed('stale-run',lambda:assembler.run(previous)) is None)
            require('stale_completion_unchanged',fixture.scalar('SELECT count(*) FROM waveforms.completion',role='assembler')==before==0)
            completion=timed('replacement-process',lambda:assembler.run(replacement))
            require('replacement_completed',completion is not None and completion.status=='complete' and fixture.scalar('SELECT count(*) FROM waveforms.completion',role='assembler')==1)
            now-=timedelta(seconds=1)
            require('rollback_refused',timed('clock-rollback',lambda:assembler.claim(uuid4(),gateway_id='GW')) is None)
            require('rollback_cleared_owned',count_owned()==0)
        dual_started=True
        dual=measure_lease_workers(fixture,root,run_id=run_id,source_sha256=source_sha256)
        result['dual_workers']=dual
        nested=[e for e in dual['events'] if e['event']=='nested_renewal_connection']
        require('two_workers_renewed',len(nested)==2 and len({e['worker'] for e in nested})==2 and dual['peak_connections']==4)
        require('two_workers_completed',dual['completed']==2)
        require('workers_clean',dual['stopped']['remaining_threads']==[] and dual['stopped']['connections']==0 and dual['owned_cleanup']['complete'])
        require('query_plans', {p['cardinality'] for p in result['plans']}=={1,64,66} and len(result['plans'])==8)
        result['status']='passed'
        return result
    except BaseException as error:
        result['error_type']=type(error).__name__
        raise
    finally:
        result['elapsed_seconds']=time.monotonic()-began
        processes=result.get('dual_workers',{}).get('owned_cleanup',{}).get('processes',[])
        result['cleanup']=dict(complete=(not dual_started or bool(processes)) and all(c.closed for c in fixture.connections[owned_connections:]) and all(p['exit_code'] is not None for p in processes),processes=processes)
        if not result['cleanup']['complete']: result['status']='partial'
        with (root/'lease-measurement.json').open('x',encoding='utf-8') as output: json.dump(result,output,default=str,indent=2)


def measure_lease_workers(fixture,root,*,run_id,source_sha256):
    """The same two-worker scenario, sampled using only its retained owners."""
    from dataclasses import replace
    from tests.waveform_measurements import OwnedSampler
    from tests.waveform_stack import Stack
    from tests.waveform_support import LIMITS
    from tests.test_waveform_repository import capture,observation
    from backend.waveforms.repository import WaveformRepository
    limits=replace(LIMITS,gateways={'A':LIMITS.gateways['GW'],'GW':LIMITS.gateways['GW']})
    with MeasurementEvidence(root,'lease_workers',dict(run_id=run_id,source_sha256=source_sha256,
            seconds=120,fault='injected local 5s lease deadline and two-worker renewal barrier')) as evidence:
        with fixture.capacity_fixture(limits),Stack(fixture,limits=limits,evidence_root=root,run_id=run_id) as stack:
            evidence.stack=stack
            for gateway in ('A','GW'):
                repo=WaveformRepository(fixture.connect,limits=limits,prefix='oil',ingress_gateway_id=gateway)
                for payload in capture(gateway): repo.ingest(observation(payload,gateway=gateway))
            began=time.monotonic(); process=stack.launch('assemble',fault='concurrent_renewals')
            def abort():
                if process.poll() is None: stack.kill_owned('assemble',reason='lease_resource_abort')
            sampler=OwnedSampler(root,lambda:[process,stack.broker.process],abort,began=began,sink=evidence.sample)
            def completed():
                sampler.check()
                return fixture.scalar('SELECT count(*) FROM waveforms.completion',role='assembler')==2
            try:
                with sampler:
                    stack.await_condition(completed,'two sampled lease workers')
                    sampler.check()
            except BaseException as error:
                if sampler.failure is not None: raise sampler.failure from error
                raise
            finally:
                evidence.result.update(continuous_samples=sampler.samples,sampler_joined=not sampler.thread.is_alive(),
                    launch_monotonic=began,elapsed_seconds=time.monotonic()-began)
                evidence.events('assemble')
            stack.stop('assemble'); stopped=stack.await_event('assemble','stopped'); events=evidence.events('assemble')
            nested=[e for e in events if e['event']=='nested_renewal_connection']
            if (len(nested)!=2 or len({e['worker'] for e in nested})!=2 or max(e['active'] for e in nested)!=4 or
                stopped['remaining_threads'] or stopped['connections'] or process.returncode!=0): raise AssertionError('Two lease worker invariants')
            evidence.result.update(events=events,peak_connections=4,completed=2,configured_connections=stack.sections['waveforms']['max_db_connections'],
                stopped=stopped,shutdown_monotonic=time.monotonic())
        return evidence.complete({})


class PreparationDeadline:
    """One absolute workstation clock shared by the entire recovery preparation."""
    def __init__(self,*,until=None):
        self.began=time.monotonic(); self.until=min(self.began+1800,until) if until is not None else self.began+1800

    def record(self,root,phase):
        import json
        now=time.monotonic()
        value=dict(phase=phase,began=self.began,deadline=self.until,monotonic=now,
            elapsed_seconds=now-self.began,remaining_seconds=self.until-now)
        with (Path(root)/'preparation-clock.jsonl').open('a',encoding='utf-8') as output:
            output.write(json.dumps(value)+'\n')
        return value

    def check(self,root,phase):
        value=self.record(root,phase)
        if value['remaining_seconds']<=0: raise RuntimeError('safety.preparation_deadline')
        if hasattr(self,'storage_guard'):
            self.storage_guard.check()
            value=self.record(root,phase)
            if value['remaining_seconds']<=0: raise RuntimeError('safety.preparation_deadline')
        return value


class ResourceWatchdog:
    """Only clocks, in-memory state and retained owned handles on this thread."""
    def __init__(self,*,until,outer=None,seconds=120,clock=time.monotonic):
        import threading,math
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0<seconds<=120: raise ValueError('Bounded operation clock required')
        self.clock,self.seconds=clock,seconds; self.outer=until if outer is None else outer; self.until=min(until,self.outer)
        self.lock=threading.RLock(); self.wake=threading.Event(); self.halt=threading.Event()
        self.operations={}; self.history={}; self.processes=[]; self.failure=None; self.closed=False
        self.thread=threading.Thread(target=self._run,name='task17-resource-watchdog')

    def _remember(self,error):
        if self.failure is None: self.failure=error
        elif error is not self.failure: self.failure.add_note('Later watchdog failure: '+type(error).__name__)
        self.wake.set()

    def _check(self):
        now=self.clock()
        if self.failure is None:
            if now>=self.until: self._remember(RuntimeError('safety.preparation_deadline'))
            elif any(now>=start+self.seconds for _,start in self.operations.values()): self._remember(RuntimeError('safety.operation_deadline'))
        if self.failure is not None: raise self.failure
        if self.closed: raise RuntimeError('safety.watchdog_closed')

    def check(self):
        with self.lock: self._check()

    def fail(self,error):
        with self.lock: self._remember(error)

    def register(self,process):
        with self.lock:
            if not any(p is process for p in self.processes): self.processes.append(process)
            try: self._check()
            except BaseException as error: failure=error
            else: return
        self._abort([process])  # A launch racing with timeout cannot escape ownership.
        raise failure

    def retire(self,process):
        with self.lock:
            if process.poll() is None: raise RuntimeError('safety.live_handle_removal')
            self.processes=[p for p in self.processes if p is not process]

    def live_handles(self):
        with self.lock: return tuple(p for p in self.processes if p.poll() is None)

    def start_operation(self,key,sequence,started):
        with self.lock:
            self._check(); old=self.history.get(key)
            if old==(sequence,started): return  # Replay cannot refresh its original clock.
            if (type(sequence) is not int or sequence!=(old[0]+1 if old else 0) or
                type(started) not in (int,float) or not started<=self.clock() or (old and started<old[1])):
                error=ValueError('Invalid resource operation transition'); self._remember(error); raise error
            self.operations[key]=(sequence,started); self.history[key]=(sequence,started); self._check()

    def end_operation(self,key):
        with self.lock:
            self._check(); self.operations.pop(key,None)

    def stage(self,until):
        with self.lock:
            self._check(); self.until=min(until,self.outer); self._check()

    def _abort(self,processes):
        # Signal every retained child first; share one five-second reap budget.
        import subprocess
        for process in processes:
            try:
                if process.poll() is None: process.kill()
            except BaseException as error:
                with self.lock: self._remember(error)
        limit=time.monotonic()+5
        for process in processes:
            try: process.wait(timeout=max(0,limit-time.monotonic()))
            except BaseException as error:
                with self.lock: self._remember(error)

    def _run(self):
        while not self.halt.is_set():
            with self.lock:
                try: self._check()
                except BaseException: doomed=list(self.processes)
                else: doomed=None
            if doomed is not None: self._abort(doomed); return
            self.wake.wait(.02); self.wake.clear()

    def start(self): self.thread.start(); return self

    def close(self):
        with self.lock:
            self.closed=True; doomed=list(self.processes) if self.failure is not None else []
        self.halt.set(); self.wake.set()
        if self.thread.ident is not None: self.thread.join(6)
        if self.thread.is_alive(): raise RuntimeError('safety.watchdog_residue')
        if doomed: self._abort(doomed)
        if self.live_handles():
            error=RuntimeError('safety.watchdog_live_handles'); self.fail(error); self._abort(list(self.processes)); raise error

    def __enter__(self): return self.start()
    def __exit__(self,kind,error,traceback):
        if error is not None: self.fail(error)
        try: self.close()
        except BaseException as cleanup:
            if error is None: raise self.failure or cleanup
            error.add_note('Watchdog cleanup failure: '+type(cleanup).__name__)


class RecoveryStorage:
    """One owned sampling child; parent never runs its DB/stat workload."""
    def __init__(self,fixture,root,*,deadline,outer,seconds=120,fault=None):
        self.fixture,self.root=fixture,Path(root); self.deadline=deadline
        self.watchdog=ResourceWatchdog(until=deadline.until,outer=outer,seconds=seconds,clock=lambda:time.monotonic())
        self.baseline=None; self.latest=None; self.pending=None; self.sequence=0; self.cursor=0
        self.stack=None; self.evidence=None; self.closed=False; self.fault=fault
        self.watchdog.start_operation('storage',0,time.monotonic())

    def accept(self,event):
        import json,math
        self.watchdog.check(); kind=event.get('event')
        if kind=='storage_started':
            sequence,started=event['sequence'],event['started']
            if self.pending is not None or type(sequence) is not int or sequence!=self.sequence+1: raise ValueError('Invalid storage sequence')
            self.watchdog.start_operation('storage',sequence,started); self.pending=(sequence,started)
        elif kind=='storage_sample':
            sequence,started,completed=event['sequence'],event['started'],event['completed']; sample=event['sample']
            if (type(sequence) is not int or type(started) not in (int,float) or self.pending!=(sequence,started) or type(completed) not in (int,float) or not math.isfinite(completed) or
                not started<=completed<=time.monotonic() or completed-started>self.watchdog.seconds or
                type(sample) is not dict or set(sample)!= {'database_bytes','artifact_bytes','logical_raw_bytes'} or
                any(type(n) is not int or n<0 for n in sample.values())): raise ValueError('Invalid storage sample')
            if self.baseline is None: self.baseline=dict(sample)
            with (self.root/'recovery-storage.jsonl').open('a',encoding='utf-8') as output:
                output.write(json.dumps(dict(event,baseline=self.baseline,cadence_seconds=5))+'\n')
            storage_check(self.baseline,sample)
            self.watchdog.end_operation('storage')
            self.watchdog.start_operation('storage-freshness',sequence-1,completed)
            self.sequence=sequence; self.latest=dict(event); self.pending=None
        elif kind not in ('resource_ready','stopped'): raise ValueError('Unexpected storage event')

    def fresh_after(self,stamp):
        return self.latest is not None and self.latest['started']>=stamp

    def __enter__(self):
        import json,sys
        from tests.waveform_stack import Stack
        self.watchdog.start()
        try:
            self.evidence=MeasurementEvidence(self.root,'storage',dict(cadence_seconds=5,operation_seconds=self.watchdog.seconds))
            self.stack=Stack(self.fixture,evidence_root=self.root,_created=self.watchdog.register)
            self.evidence.stack=self.stack; self.stack.__enter__()
            request=dict(kind='storage_monitor',artifact_root=str(self.root.resolve()),fault=self.fault)
            (self.stack.root/'resource-request.json').write_text(json.dumps(request),encoding='utf-8')
            self.process=self.stack.launch('resource_measure')
            self.check(force=True)
            return self
        except BaseException:
            self.__exit__(*sys.exc_info()); raise

    def check(self,*,force=False):
        if self.closed:
            if self.watchdog.failure is not None: raise self.watchdog.failure
            if force: raise RuntimeError('safety.storage_monitor_closed')
            return
        required=time.monotonic() if force else None
        while True:
            self.watchdog.check()
            if self.process.poll() is not None: raise RuntimeError('safety.storage_monitor_exited')
            events=self.stack.events('resource_measure')
            for event in events[self.cursor:]: self.accept(event); self.cursor+=1
            self.watchdog.check()
            sample_resource_job(self.stack,self.process,self.evidence,self.root,watchdog=self.watchdog)
            self.evidence.events('resource_measure')
            if self.latest is not None and (required is None or self.fresh_after(required)): return
            time.sleep(.05)

    def __exit__(self,kind,error,traceback):
        first=self.watchdog.failure or error
        def attempt(action):
            nonlocal first
            try: action()
            except BaseException as failure:
                if first is None: first=failure
                else: first.add_note('Storage cleanup failure: '+type(failure).__name__)
                self.watchdog.fail(first)
        if first is not None: self.watchdog.fail(first)
        else: attempt(lambda:self.watchdog.start_operation('storage-close',0,time.monotonic()))
        if self.stack is not None:
            if 'resource_measure' in self.stack.processes: attempt(lambda:self.stack.stop('resource_measure'))
            attempt(lambda:self.stack.__exit__(type(first) if first else None,first,traceback))
        if self.evidence is not None:
            if first is None: attempt(lambda:self.evidence.complete(dict(baseline=self.baseline,last_sample=self.latest)))
            attempt(lambda:self.evidence.__exit__(type(first) if first else None,first,traceback))
        attempt(self.watchdog.close)
        self.closed=True
        if first is None and self.watchdog.failure is not None: first=self.watchdog.failure
        if first is not None and first is not error: raise first from error

    def closure(self):
        return bool(self.closed and not self.watchdog.thread.is_alive() and self.watchdog.failure is None and
            self.evidence and self.evidence.result['owned_cleanup']['complete'])


class RecoveryBudget:
    """R88: two shared stages, additionally clamped by one caller/outer limit."""
    def __init__(self,*,until=None):
        self.began=time.monotonic(); self.outer=min(self.began+3600,until) if until is not None else self.began+3600
        self.preparation=PreparationDeadline(until=min(self.began+1800,self.outer)); self.download=None

    def start_download(self,root):
        if self.download is not None: raise ValueError('Download stage already started')
        self.prepared=self.preparation.check(root,'preparation-complete')
        self.download=PreparationDeadline(until=self.outer)
        if hasattr(self.preparation,'storage_guard'):
            self.download.storage_guard=self.preparation.storage_guard
            if hasattr(self.download.storage_guard,'watchdog'): self.download.storage_guard.watchdog.stage(self.download.until)
        self.download.check(root,'download-start')
        return self.download

    def finish(self,root):
        if self.download is None: raise ValueError('Download stage absent')
        if hasattr(self.download,'storage_guard'): self.download.storage_guard.check(force=True)
        return self.download.check(root,'download-complete')


def validate_recovery_measurement(value,*,run_id,source_sha256,volumes=(1,100,1000)):
    """Data-only fail-closed gate, also used before creating any backup baseline."""
    import math,re
    try:
        def number(n): return type(n) in (int,float) and math.isfinite(n)
        if (value['schema']!='waveform.recovery-downloads/2' or value['policy']!='R88-two-stage' or
            value['status']!='passed' or value['run_id']!=run_id or value['source_sha256']!=source_sha256 or
            not re.fullmatch('[0-9a-f]{32}',run_id) or not re.fullmatch('[0-9a-f]{64}',source_sha256) or
            value['volumes']!=list(volumes) or value['owned_cleanup'] is not True or value['monitor_cleanup'] is not True): raise ValueError()
        c=value['clock']
        if (not all(number(c[k]) for k in ('began','outer','preparation_deadline','prepared','download_started','download_deadline','completed')) or
            not c['began']<=c['prepared']<c['preparation_deadline']<=min(c['began']+1800,c['outer']) or
            not c['prepared']<=c['download_started']<=c['completed']<c['download_deadline']<=min(c['download_started']+1800,c['outer']) or
            c['outer']>c['began']+3600): raise ValueError()
        prepared=value['prepared']; downloads=value['downloads']; expected={}; versions=set(); paths=set()
        if type(prepared) is not list or len(prepared)!=1+len(volumes) or [p['members'] for p in prepared[1:]]!=list(volumes): raise ValueError()
        for index,version in enumerate(prepared):
            id=version['version_id']
            if (not id or id in versions or not version['export_id'] or type(version['actor']) is not dict or
                version['run_id']!=run_id or version['source_sha256']!=source_sha256): raise ValueError()
            versions.add(id); files=version['expected']
            if type(files) is not list or len(files)!=(3 if index==0 else version['members']+1) or files[0]['name']!='manifest.json': raise ValueError()
            for file in files:
                key=(id,file['name'])
                if (key in expected or Path(file['name']).name!=file['name'] or
                    not re.fullmatch('[0-9a-f]{64}',file['sha256']) or type(file['size']) is not int or file['size']<=0): raise ValueError()
                expected[key]=(version,file)
        if type(downloads) is not list or len(downloads)!=len(expected): raise ValueError()
        for file in downloads:
            version,original=expected.pop((file['version_id'],file['name']))
            if (any(file.get(k)!=v for k,v in original.items()) or file['export_id']!=version['export_id'] or
                not number(file['started']) or not number(file['completed']) or
                not c['download_started']<=file['started']<=file['completed']<=c['completed'] or
                file['completed']-file['started']>120 or file['relative_path'] in paths): raise ValueError()
            path=Path(file['relative_path'])
            if path.is_absolute() or '..' in path.parts: raise ValueError()
            paths.add(file['relative_path'])
        if expected: raise ValueError()
    except (KeyError,TypeError,ValueError,AttributeError) as error:
        raise ValueError('Incomplete or invalid two-stage recovery evidence') from error
    return value


def prepare_members(fixture,root,*,count=1000,progress=None,deadline=None):
    """Distinct real references through intake/assembly/quality, never direct rows."""
    from dataclasses import replace
    from uuid import uuid4
    from backend.waveforms.commands import Command,quality_request_hash
    from backend.waveforms.quality_service import QualityService
    from tests.waveform_pairing_support import CREATOR
    from tests.waveform_support import LIMITS
    from tests.waveform_measurements import build_capture,ingest_file
    if type(count) is not int or not 1<=count<=1000: raise ValueError('Population bound')
    deadline=deadline or PreparationDeadline(); deadline.check(root,'population-start')
    quality=QualityService(lambda:fixture.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:fixture.connect('reader'))
    began=time.monotonic(); members=[]
    monitor=getattr(deadline,'storage_guard',None)
    storage_before=None if monitor else storage_sample(fixture,root)
    for index in range(count):
        deadline.check(root,'population-'+str(index))
        safety_check(host_resources(root))
        path=Path(root)/('reference-'+str(index)+'.owpwave')
        build_capture(path,frames=4,channels=1,chunk_frames=4)
        frame,pk,_=ingest_file(fixture,path)
        deadline.check(root,'population-quality-'+str(index))
        key=dict(gateway_id=frame.key.gateway_id,device_id=frame.key.device_id,capture_id=str(frame.key.capture_id))
        selection=dict(purpose='raw_archive',profile=None,captures=[dict(key=key,manifest_sha256=frame.raw_sha256,origins=[])],channels=[],cycle=None,sync=None)
        command=Command(CREATOR,uuid4(),0,'Synthetic resource archive','0'*64)
        result=quality.record(frame.key,selection,replace(command,request_hash=quality_request_hash(frame.key,command,body=selection)))
        members.append(dict(kind='capture',reference_id=str(pk),quality_run_id=str(result.runs[0].id),split='unassigned'))
        if (index+1)%10==0:
            if monitor: monitor.check()
            else: storage_check(storage_before,storage_sample(fixture,root))
        if progress: progress(index+1,time.monotonic()-began)
    deadline.check(root,'population-complete')
    return members


def dataset_work(connect,members,root,emit,*,deadline=None,stage='complete'):
    """Fresh child performs staging/publication/download with explicit phase times."""
    from dataclasses import replace
    from hashlib import sha256
    from uuid import UUID,uuid4
    from backend.waveforms.commands import Command,dataset_request_hash,export_request_hash
    from backend.waveforms.datasets import DatasetService
    from backend.waveforms.export import DatasetExporter
    from backend.waveforms.contracts import CaptureKey
    from tests.waveform_pairing_support import CREATOR,REVIEWER
    from tests.waveform_support import LIMITS
    def check():
        if deadline is not None and time.monotonic()>=deadline: raise RuntimeError('safety.preparation_deadline')
    service=DatasetService(lambda:connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:connect('reader'))
    def call(operation,body,draft=None):
        check()
        began=time.monotonic(); target=draft.id if draft else None
        emit('dataset_phase_started',phase=operation)
        command=Command(REVIEWER if operation=='freeze' else CREATOR,uuid4(),draft.revision if draft else 0,'Synthetic resource dataset','0'*64)
        command=replace(command,request_hash=dataset_request_hash(operation,target,command,body=body))
        result=getattr(service,operation)(body,command)
        elapsed=time.monotonic()-began
        emit('dataset_phase',phase=operation,elapsed_seconds=elapsed)
        if elapsed>120: raise RuntimeError('safety.dataset_operation_deadline')
        check()
        return result
    body=dict(title='Synthetic resource archive',purpose='raw_archive',split_plan=dict(kind='none'))
    draft=call('create',body)
    for start in range(0,len(members),100):
        draft=call('update',dict(target_id=str(draft.id),changes=dict(upsert_members=members[start:start+100])),draft)
    body=dict(target_id=str(draft.id)); draft=call('submit',body,draft); version=call('freeze',body,draft)
    exporter=DatasetExporter(lambda:connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:connect('reader'))
    body=dict(version_id=str(version.id),purpose='evidence'); command=Command(REVIEWER,uuid4(),0,'Synthetic resource export','0'*64)
    check()
    export_started=time.monotonic(); emit('dataset_phase_started',phase='export_record')
    record=exporter.create(body,replace(command,request_hash=export_request_hash(version.id,command,body=body)))
    if time.monotonic()-export_started>120: raise RuntimeError('safety.dataset_operation_deadline')
    check()
    if stage=='prepare':
        return prepared_export(exporter,record,version,root,members=version.member_count,update_batches=(len(members)+99)//100)
    manifest=sha256(); length=0; began=time.monotonic(); emit('dataset_phase_started',phase='download')
    root=Path(root); root.mkdir(parents=True,exist_ok=False)
    with (root/'manifest.json').open('xb') as output:
        for block in exporter.open(record.id,actor=REVIEWER).stream_manifest():
            check()
            manifest.update(block); length+=len(block); output.write(block)
    if manifest.hexdigest()!=version.content_sha256 or length!=version.manifest_bytes: raise AssertionError('Manifest exact-byte identity')
    files=[]
    for offset in range(0,len(members),100):
        check()
        for descriptor in exporter.files(version.id,actor=REVIEWER,offset=offset):
            check()
            key=dict(descriptor['key']); key['capture_id']=UUID(key['capture_id'])
            digest=sha256(); size=0; file_started=time.monotonic(); emit('dataset_phase_started',phase='capture_download')
            filename=str(descriptor['capture_pk'])+'.owpwave'
            with (root/filename).open('xb') as output:
                for block in exporter.open(record.id,actor=REVIEWER).stream_capture(CaptureKey(**key)):
                    check()
                    digest.update(block); size+=len(block); output.write(block)
                    if time.monotonic()-file_started>120: raise RuntimeError('safety.dataset_download_deadline')
            if digest.hexdigest()!=descriptor['file_sha256'] or size!=descriptor['content_length']: raise AssertionError('Canonical capture bytes')
            files.append(dict(capture_pk=str(descriptor['capture_pk']),file_sha256=digest.hexdigest(),bytes=size,key=dict(descriptor['key']),relative_path=filename))
    emit('dataset_phase',phase='download',elapsed_seconds=time.monotonic()-began)
    check()
    return dict(version_id=str(version.id),dataset_id=str(version.dataset_id),export_id=str(record.id),export_root=str(root),
        manifest_sha256=manifest.hexdigest(),manifest_bytes=length,
        members=version.member_count,files=len(files),file_inventory=files,update_batches=(len(members)+99)//100)


def prepared_export(exporter,record,version,root,*,members,update_batches=0):
    """Immutable descriptors only; this does not open or authorize any download."""
    from dataclasses import asdict
    files=[]
    for offset in range(0,2000,100):
        page=exporter.files(version.id,actor=record.actor,offset=offset)
        files.extend(dict(d) for d in page)
        if len(page)<100: break
    expected=[dict(name='manifest.json',sha256=version.content_sha256,size=version.manifest_bytes)]
    expected.extend(dict(name=str(d['capture_pk'])+'.owpwave',sha256=d['file_sha256'],size=d['content_length'],key=dict(d['key'])) for d in files)
    return dict(version_id=str(version.id),dataset_id=str(version.dataset_id),export_id=str(record.id),actor=asdict(record.actor),
        export_root=str(root),manifest_sha256=version.content_sha256,manifest_bytes=version.manifest_bytes,
        members=members,files=len(files),expected=expected,update_batches=update_batches)


def download_prepared(connect,prepared,emit,*,deadline):
    """One version, fresh open AND first-advance authorization for every file."""
    import json
    from hashlib import sha256
    from uuid import UUID
    from backend.waveforms.commands import Actor
    from backend.waveforms.contracts import CaptureKey
    from backend.waveforms.export import DatasetExporter
    from tests.waveform_support import LIMITS
    exporter=DatasetExporter(lambda:connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:connect('reader'))
    actor=Actor(**prepared['actor']); root=Path(prepared['export_root']); root.mkdir(parents=True,exist_ok=False)
    completed=[]; all_started=time.monotonic()
    for file in prepared['expected']:
        started=time.monotonic(); emit('dataset_phase_started',phase='file_download',name=file['name'])
        def check():
            now=time.monotonic()
            if now>=deadline: raise RuntimeError('safety.preparation_deadline')
            if now-started>120: raise RuntimeError('safety.dataset_download_deadline')
        check(); context=None; iterator=None; original=None
        try:
            context=exporter.open(UUID(prepared['export_id']),actor=actor); check()
            key=dict(file.get('key',{}))
            if key: key['capture_id']=UUID(key['capture_id'])
            iterator=context.stream_capture(CaptureKey(**key)) if key else context.stream_manifest()
            if key: original=Path(file['original_path']).open('rb')
            digest=sha256(); length=0
            with (root/file['name']).open('xb') as output:
                for block in iterator:
                    check()
                    if original and original.read(len(block))!=block: raise AssertionError('Original whole bytes differ')
                    digest.update(block); length+=len(block); output.write(block)
            check()
            if original and original.read(1): raise AssertionError('Original export truncated')
            if digest.hexdigest()!=file['sha256'] or length!=file['size']: raise AssertionError('Frozen canonical file identity differs')
            result=dict(file,version_id=prepared['version_id'],export_id=prepared['export_id'],started=started,completed=time.monotonic())
            check(); completed.append(result)
            with (root/'download-progress.jsonl').open('a',encoding='utf-8') as output: output.write(json.dumps(result)+'\n')
            emit('dataset_file_complete',**result)
        finally:
            import sys
            active=sys.exc_info()[1]; first=active
            for resource in (iterator,context,original):
                if resource is None: continue
                try: resource.close()
                except BaseException as error:
                    if first is None: first=error
                    else: first.add_note('Download close failure: '+type(error).__name__)
            if first is not None and active is None: raise first
    emit('dataset_phase',phase='download',elapsed_seconds=time.monotonic()-all_started)
    return dict(prepared,downloads=completed,file_inventory=[dict(capture_pk=f['name'][:-8],file_sha256=f['sha256'],bytes=f['size'],key=f['key'],relative_path=f['name']) for f in completed if 'key' in f])


def run_resource_worker(config_path,stop,emit):
    import json
    from tests.waveform_dataset_support import pinned_connect
    root=Path(config_path).parent
    request=json.loads((root/'resource-request.json').read_text(encoding='utf-8'))
    emit('resource_ready')
    if request['kind']=='storage_monitor':
        class StorageSource:
            def scalar(self,query,params=()):
                with pinned_connect('reader') as connection: return connection.execute(query,params).fetchone()[0]
        sequence=0
        while not stop.is_set():
            sequence+=1; started=time.monotonic()
            emit('storage_started',sequence=sequence,started=started)
            if request.get('fault')=='blocked_storage' and sequence==2:
                with pinned_connect('reader') as connection: connection.execute('SELECT pg_advisory_xact_lock(499001219)')
            elif request.get('fault') not in (None,'blocked_storage'): raise ValueError('Unknown storage fault')
            sample=storage_sample(StorageSource(),Path(request['artifact_root']))
            emit('storage_sample',sequence=sequence,started=started,completed=time.monotonic(),sample=sample)
            if stop.wait(5): break
        emit('stopped',remaining_threads=[])
        return 0
    original_emit=emit; operation_sequence=0
    def emit(kind,**values):
        nonlocal operation_sequence
        if kind=='dataset_phase_started':
            operation_sequence+=1
            original_emit('resource_operation',sequence=operation_sequence,started=time.monotonic(),phase=values['phase'])
        original_emit(kind,**values)
    if request['kind']=='target_exports':
        from tools.waveform_restore import run_target_exports
        result=run_target_exports(request,emit)
    elif request['kind']=='dataset': result=dataset_work(pinned_connect,request['members'],request['export_root'],emit,deadline=request.get('preparation_deadline'),stage=request.get('stage','complete'))
    elif request['kind']=='download': result=download_prepared(pinned_connect,request['prepared'],emit,deadline=request['preparation_deadline'])
    elif request['kind']=='diagnostic_wait':
        emit('dataset_phase_started',phase='diagnostic_wait'); stop.wait(30); result=dict(stopped=True)
    elif request['kind']=='intake':
        from tests.waveform_measurements import ingest_file
        class Source:
            @staticmethod
            def connect(role='receiver'): return pinned_connect(role)
            def scalar(self,query,params=()):
                with self.connect('reader') as connection: return connection.execute(query,params).fetchone()[0]
        emit('dataset_phase_started',phase='intake_and_assembly')
        frame,pk,result=ingest_file(Source(),request['path'])
        result.update(capture_pk=str(pk),capture_id=str(frame.key.capture_id))
    else: raise ValueError('Unknown bounded resource job')
    (root/'resource-result.json').write_text(json.dumps(result,default=str,indent=2),encoding='utf-8')
    emit('resource_complete')
    collection_seconds=min(120,max(0,request['preparation_deadline']-time.monotonic())) if request['kind']=='target_exports' else 10
    if not stop.wait(collection_seconds): raise RuntimeError('Owned resource parent did not collect result')
    emit('stopped',remaining_threads=[])
    return 0


def measured_dataset(fixture,root,members,*,deadline=None,stage='complete'):
    from uuid import uuid4
    return measured_job(fixture,root,dict(kind='dataset',stage=stage,members=members,export_root=str(Path(root)/('exports-'+uuid4().hex))),deadline=deadline)


class TargetExportEvents:
    """Strict start/terminal pairing for this one target export set."""
    def __init__(self,watch,key):
        self.watch,self.key=watch,key; self.sequence=0; self.active=None; self.completed=0.; self.names=[]

    def accept(self,event):
        import math
        self.watch.check()
        kind=event['event']; sequence=event['sequence']; name=event['operation']; started=event['started']
        if (type(sequence) is not int or not isinstance(name,str) or not 1<=len(name)<=160 or
            type(started) not in (int,float) or not math.isfinite(started) or started>time.monotonic()): raise ValueError('Invalid target operation')
        if kind=='target_operation_started':
            if self.active is not None or sequence!=self.sequence+1 or started<self.completed: raise ValueError('Unpaired target start')
            self.watch.start_operation(self.key,sequence,started); self.sequence=sequence; self.active=(sequence,name,started)
        elif kind=='target_operation_complete':
            completed=event['completed']
            if (self.active!=(sequence,name,started) or type(completed) not in (int,float) or not math.isfinite(completed) or
                not started<=completed<=time.monotonic() or completed-started>=120): raise ValueError('Unpaired target completion')
            self.active=None; self.completed=completed; self.names.append(name)
        elif kind=='target_operation_failed': raise RuntimeError('safety.restore_export_failed')
        else: raise ValueError('Unknown target operation event')

    def finish(self):
        self.watch.check()
        if self.active is not None or not self.names or self.names[-1]!='finalization': raise ValueError('Incomplete target operations')
        return self.names


def supervised_target_exports(fixture,root,run_id,source_sha256,records,references,*,deadline=None):
    from tools.waveform_restore import target_export_request
    deadline=deadline or PreparationDeadline()
    request=target_export_request(root,run_id,source_sha256,records,references)
    measured=measured_job(fixture,root,request,deadline=deadline)
    deadline.check(root,'target-export-clean-complete')
    if (measured['status']!='passed' or measured['exit_code']!=0 or not measured['owned_cleanup']['complete'] or
        measured.get('target_operations_validated') is not True or measured.get('watchdog_closed') is not True):
        raise ValueError('Unsupervised target export result')
    return measured


def measured_job(fixture,root,request,*,deadline=None):
    import json,sys
    from contextlib import nullcontext,contextmanager
    from uuid import uuid4
    from tests.waveform_stack import Stack
    deadline=deadline or PreparationDeadline(); deadline.check(root,'resource-start')
    monitor=getattr(deadline,'storage_guard',None)
    watch=monitor.watchdog if monitor else ResourceWatchdog(until=deadline.until,clock=lambda:time.monotonic())
    key='work-'+uuid4().hex
    target_events=TargetExportEvents(watch,key) if request['kind']=='target_exports' else None
    began=time.monotonic()
    @contextmanager
    def operation():
        watch.start_operation(key,0,began)
        try:
            yield
            watch.end_operation(key)
            if not monitor:
                watch.close()
                evidence.result['watchdog_closed']=True
                deadline.check(root,'resource-watchdog-closed')
        except BaseException as error:
            watch.fail(error)
            if watch.failure is not error: raise watch.failure from error
            raise
    stack=Stack(fixture,evidence_root=root,_created=watch.register)
    with (nullcontext(watch) if monitor else watch),MeasurementEvidence(root,'resource',dict(job=request['kind'],members=len(request.get('members',[])),source=request.get('path'))) as evidence,operation():
      evidence.stack=stack
      with stack:
        (stack.root/'resource-request.json').write_text(json.dumps(dict(request,preparation_deadline=deadline.until)),encoding='utf-8')
        deadline.check(root,'resource-launch')
        process=stack.launch('resource_measure')
        samples=[]; phase_count=0; target_cursor=0
        def operations():
            nonlocal phase_count,target_cursor
            watch.check()
            if target_events:
                events=[e for e in stack.events('resource_measure') if e.get('event','').startswith('target_operation_')]
                for event in events[target_cursor:]: target_events.accept(event); target_cursor+=1
                return
            for event in stack.events('resource_measure','resource_operation'):
                if event['sequence']>phase_count:
                    watch.start_operation(key,event['sequence'],event['started']); phase_count=event['sequence']
        try:
            while not stack.events('resource_measure','resource_complete'):
                operations()
                evidence.result['preparation_clock']=deadline.check(root,'resource-running-'+str(phase_count))
                if process.poll() is not None: raise AssertionError('Resource worker exited before result: '+str(stack.events('resource_measure')))
                sample=sample_resource_job(stack,process,evidence,root,watchdog=watch); samples.append(sample)
                evidence.events('resource_measure')
                time.sleep(.1)
            operations()
            evidence.result['preparation_clock']=deadline.check(root,'resource-complete')
            result=json.loads((stack.root/'resource-result.json').read_text(encoding='utf-8'))
            if target_events:
                from tools.waveform_restore import _verify_target_exports
                names=target_events.finish()
                _verify_target_exports(request,result,operations=names)
                evidence.result.update(target_operations_validated=True,target_operations=names)
            evidence.result['result']=result
            sample=sample_resource_job(stack,process,evidence,root,watchdog=watch); samples.append(sample)
            watch.check()
        except BaseException as error:
            watch.fail(error)
            try:
                if process.poll() is None: stack.kill_owned('resource_measure',reason='resource_abort')
            except BaseException as cleanup: error.add_note('Owned kill failure: '+type(cleanup).__name__)
            if watch.failure is not error: raise watch.failure from error
            raise
        finally:
            active=sys.exc_info()[1]; first=active
            def cleanup(action):
                nonlocal first
                try: action()
                except BaseException as error:
                    if first is None: first=error
                    else: first.add_note('Resource cleanup failure: '+type(error).__name__)
            cleanup(lambda:stack.stop('resource_measure'))
            cleanup(lambda:evidence.events('resource_measure'))
            cleanup(lambda:evidence.result.update(samples=samples,elapsed_seconds=time.monotonic()-began,exit_code=process.returncode))
            cleanup(lambda:evidence.result.update(preparation_clock=deadline.record(root,'resource-cleanup')))
            if first is not None and active is None: raise first
        if process.returncode!=0: raise AssertionError('Resource child unclean exit')
        evidence.result['preparation_clock']=deadline.check(root,'resource-clean-complete')
        return evidence.complete(dict(result=result))


def sample_resource_job(stack,process,evidence,root,*,watchdog=None):
    handles=[process]+([stack.broker.process] if stack.broker else [])
    if watchdog:
        for handle in watchdog.live_handles():
            if not any(handle is existing for existing in handles): handles.append(handle)
    samples=[process_memory(handle) for handle in handles]; host=host_resources(root)
    controller=current_process_memory()
    evidence.sample(dict(monotonic=time.monotonic(),processes=samples,host=host,controller=controller))
    safety_check(host,samples)
    return samples[0]


def sender_storage_sample(spool_path):
    """DELETE-mode journals can vanish at commit; primary-file errors are fatal."""
    database_bytes=spool_path.stat().st_size
    try: journal_bytes=spool_path.with_name(spool_path.name+'-journal').stat().st_size
    except FileNotFoundError: journal_bytes=0
    return dict(database_bytes=database_bytes,journal_bytes=journal_bytes)


class SenderDeadline:
    """R75 workstation budgets: each operation <=120s, queue <=120s/capture."""
    def __init__(self,captures,*,began):
        if captures not in (1,3): raise ValueError('One/three queue contract')
        self.began=began; self.aggregate_seconds=120*captures
        self.cursor=0; self.active={}; self.last_activity=began

    def check(self,now,events):
        if now-self.began>self.aggregate_seconds: raise RuntimeError('safety.sender_aggregate_deadline')
        for event in events[self.cursor:]:
            kind=event.get('event')
            if kind not in ('sender_phase_started','sender_phase_complete'): continue
            key=(event['phase'],event['operation_id']); stamp=event['monotonic']
            if kind=='sender_phase_started':
                if key in self.active: raise AssertionError('Duplicate sender phase')
                self.active[key]=stamp
            else:
                started=self.active.pop(key,None)
                if started is None or stamp<started: raise AssertionError('Unmatched sender phase')
                if max(stamp-started,event['elapsed_seconds'])>120: raise RuntimeError('safety.sender_operation_deadline')
            self.last_activity=max(self.last_activity,stamp)
        self.cursor=len(events)
        if now-min(self.active.values(),default=self.last_activity)>120:
            raise RuntimeError('safety.sender_operation_deadline')


def _abort_sender(stack,process,result,error):
    try:
        if process.poll() is None: result['abort']=stack.kill_owned('sender_measure',reason='resource_abort')
    except BaseException as failure:
        error.add_note('Sender abort cleanup: '+type(failure).__name__)


def _finish_sender_workers(stack,process,result,began,*,error):
    first=error
    def attempt(stage,action):
        nonlocal first
        try: return action()
        except BaseException as failure:
            if first is None: first=failure
            else: first.add_note('Sender '+stage+' cleanup: '+type(failure).__name__)
            return None
    attempt('sender-stop',lambda:stack.stop('sender_measure'))
    result['events']=attempt('sender-events',lambda:stack.events('sender_measure'))
    result['exit_code']=process.returncode; result['elapsed_seconds']=time.monotonic()-began
    result['workers']={}
    for name,child in stack.processes.items():
        attempt(name+'-stop',lambda name=name:stack.stop(name))
        stopped=attempt(name+'-events',lambda name=name:stack.events(name,'stopped',current=True))
        result['workers'][name]=dict(exit_code=child.returncode,stopped=stopped[-1] if stopped else None)
    if first is not None and first is not error: raise first


def _finish_sender_evidence(samples_file,stack,result,evidence,entered,*,error):
    import json
    first=error
    def attempt(stage,action):
        nonlocal first
        try: return action()
        except BaseException as failure:
            if first is None: first=failure
            else: first.add_note('Sender '+stage+' cleanup: '+type(failure).__name__)
            return None
    attempt('samples-close',samples_file.close)
    processes=[]
    for item in [*stack.instances,*(stack.broker.instances if stack.broker else [])]:
        exit_code=attempt('process-state',item['process'].poll)
        processes.append(dict(instance_id=item['instance_id'],component=item['component'],pid=item['pid'],exit_code=exit_code))
    result['cleanup']=dict(complete=all(p['exit_code'] is not None for p in processes),processes=processes)
    result['total_elapsed_seconds']=time.monotonic()-entered
    def save():
        with (evidence/'result.json').open('x',encoding='utf-8') as output: json.dump(result,output,indent=2)
    attempt('envelope',save)
    if first is not None and first is not error: raise first


def measured_sender(fixture,root,paths,*,with_receipts=False):
    """Persist each owned sample and post-cleanup envelope even on failure."""
    import json
    from uuid import uuid4
    from backend.waveforms.sender_spool import WaveSenderSpool
    from tests.waveform_stack import Stack
    if len(paths) not in (1,3): raise ValueError('One/three queue contract')
    evidence=Path(root)/('sender-'+uuid4().hex); evidence.mkdir(exist_ok=False)
    result=dict(status='partial',captures=len(paths),samples=[],physical=[],events=[],
        envelope_path=str(evidence/'result.json'),samples_path=str(evidence/'samples.jsonl'),
        bounds=dict(operation_seconds=120,aggregate_seconds=120*len(paths),sample_interval_seconds=.05),
        limits='Workstation measurement bounds, not production SLA. Logical bytes do not cap SQLite physical size, journals or RSS')
    stack=Stack(fixture,evidence_root=root); entered=time.monotonic()
    samples_file=(evidence/'samples.jsonl').open('x',encoding='utf-8')
    try:
        with stack:
            spool_path=stack.root/'measured-sender.db'; budget=128*1024**2
            stack.sections['sender_spool']=dict(path=str(spool_path),max_bytes=budget); stack.save_config()
            if with_receipts:
                stack.launch('receive'); stack.await_event('receive','subscribed',gateway='GW')
                stack.launch('publish')
            with WaveSenderSpool.initialize(spool_path,max_bytes=budget) as spool:
                spool.bind('GW','oil')
                for path in paths:
                    with Path(path).open('rb') as source: spool.enqueue(source)
                result['logical_status']=spool.status()
                result['durability']=dict(journal_mode=spool._db.execute('PRAGMA journal_mode').fetchone()[0],
                    synchronous=spool._db.execute('PRAGMA synchronous').fetchone()[0])
                for _ in range(140): spool.apply_receipt('oil/v2/GW/PUMP-001/waveform-ack',b'{}')
                if spool.status()['diagnostic_events']!=128: raise AssertionError('Diagnostic ring bound')
                result['diagnostic_events']=128
            process=stack.launch('sender_measure'); stack.await_event('sender_measure','sender_ready')
            began=time.monotonic(); deadline=SenderDeadline(len(paths),began=began)
            try:
                while True:
                    if process.poll() is not None: raise AssertionError('Sender measurement exited early')
                    sample=process_memory(process); physical=sender_storage_sample(spool_path)
                    owned=[process_memory(p) for p in [stack.broker.process,*stack.processes.values()] if p.poll() is None]
                    host=host_resources(root)
                    result['samples'].append(sample); result['physical'].append(physical)
                    samples_file.write(json.dumps(dict(memory=sample,physical=physical,owned=owned,host=host))+'\n'); samples_file.flush()
                    safety_check(host,owned)
                    events=stack.events('sender_measure'); deadline.check(time.monotonic(),events)
                    batches=[e for e in events if e['event']=='sender_batch']; progress=[e for e in events if e['event']=='sender_progress']
                    done=(any(b['captures']==len(paths) for b in batches) and bool(progress) and progress[-1]['pending_messages']==0
                        if with_receipts else len(progress)>=2)
                    if done: break
                    time.sleep(.05)
            except BaseException as error:
                _abort_sender(stack,process,result,error)
                raise
            finally:
                import sys
                _finish_sender_workers(stack,process,result,began,error=sys.exc_info()[1])
            if any(v['exit_code']!=0 or not v['stopped'] or v['stopped'].get('remaining_threads') for v in result['workers'].values()):
                raise AssertionError('Sender scenario child residue')
        result['status']='passed'
        return result
    except BaseException as error:
        result['reason']=str(error) if str(error) in ('safety.sender_operation_deadline','safety.sender_aggregate_deadline',
            'safety.host_reserve','safety.owned_rss') else 'error.'+type(error).__name__
        error.add_note('Sender measurement envelope: '+str(evidence/'result.json'))
        raise
    finally:
        import sys
        _finish_sender_evidence(samples_file,stack,result,evidence,entered,error=sys.exc_info()[1])


def _finish_output_owners(clients,monitor,stack,evidence,samples,began,*,error):
    first=error
    def attempt(stage,action):
        nonlocal first
        try: action()
        except BaseException as failure:
            if first is None: first=failure
            else: first.add_note('Output '+stage+' cleanup: '+type(failure).__name__)
    for sock in clients: attempt('socket',sock.close)
    attempt('sampler',lambda:monitor.__exit__(type(first) if first else None,first,getattr(first,'__traceback__',None)))
    attempt('child',lambda:stack.stop('http_measure'))
    attempt('events',lambda:evidence.events('http_measure'))
    attempt('evidence',lambda:evidence.result.update(samples=samples,continuous_samples=monitor.samples,
        elapsed_seconds=time.monotonic()-began))
    if first is not None and first is not error: raise first


def measured_output_owners(fixture,root,route):
    """Real slow/reset/half-close clients share one reader permit, not one output."""
    import json
    import socket
    import struct
    from tests.waveform_stack import Stack
    from tests.test_waveform_config import toml_text
    from tests.waveform_measurements import OwnedSampler
    with MeasurementEvidence(root,'output_owners',dict(route=route)) as evidence,Stack(fixture,evidence_root=root) as stack:
        evidence.stack=stack
        sections=dict(stack.sections); sections['management']={'shared_accounting_bucket':'GW'}
        sections['waveforms']=dict(sections['waveforms'],max_db_connections=1)
        path=stack.root/'http.toml'; path.write_text(toml_text(sections),encoding='utf-8')
        process=stack.launch('http_measure',config=path); ready=stack.await_event('http_measure','http_ready')
        token=(stack.root/'http-token').read_text(encoding='utf-8'); clients=[]; samples=[]
        request=(f'GET /api/waveforms{route} HTTP/1.1\r\nHost: 127.0.0.1:{ready["port"]}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n').encode()
        began=time.monotonic()
        monitor=OwnedSampler(root,lambda:[process,stack.broker.process],
            lambda:stack.kill_owned('http_measure',reason='safety_output_abort') if process.poll() is None else None,sink=evidence.sample)
        monitor.__enter__()
        try:
            slow=socket.create_connection(('127.0.0.1',ready['port']),timeout=120); clients.append(slow)
            slow.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096); slow.sendall(request)
            stack.await_event('http_measure','http_write_started')
            samples.append(process_memory(process))
            reset=socket.create_connection(('127.0.0.1',ready['port']),timeout=120); clients.append(reset)
            reset.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096); reset.sendall(request)
            stack.await_condition(lambda:len(stack.events('http_measure','http_write_started'))>=2,'second output owner')
            healthy=socket.create_connection(('127.0.0.1',ready['port']),timeout=120); clients.append(healthy)
            healthy.sendall(request)
            reset.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('hh',1,0)); reset.close()
            response=bytearray()
            while True:
                block=healthy.recv(65536)
                if not block: break
                response.extend(block)
                sample=process_memory(process); samples.append(sample); safety_check(host_resources(root),[sample])
                if time.monotonic()-began>120: raise RuntimeError('safety.output_deadline')
            headers,body=bytes(response).split(b'\r\n\r\n',1)
            if b' 200 ' not in headers.splitlines()[0] or not json.loads(body).get('data'):
                raise AssertionError('Healthy read failed: '+headers.splitlines()[0].decode('ascii')+' '+str(json.loads(body).get('error',{}).get('code')))
            slow.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('hh',1,0)); slow.close()
            stack.await_condition(lambda:stack.events('http_measure','http_resources') and
                stack.events('http_measure','http_resources')[-1]['output_owners']==0,'output owner cleanup')
            events=stack.events('http_measure')
            writes=[e for e in events if e['event']=='http_write_started']
            if len(writes)<3 or any(e['db_active'] for e in writes): raise AssertionError('Output retained database permit')
            half=socket.create_connection(('127.0.0.1',ready['port']),timeout=120); clients.append(half)
            half.sendall(request); half.shutdown(socket.SHUT_WR); half_response=bytearray()
            while True:
                block=half.recv(65536)
                if not block: break
                half_response.extend(block)
                if time.monotonic()-began>120: raise RuntimeError('safety.output_deadline')
            half_headers,half_body=bytes(half_response).split(b'\r\n\r\n',1)
            if b' 200 ' not in half_headers.splitlines()[0] or json.loads(half_body)!=json.loads(body):
                raise AssertionError('Valid half-closed client response differs')
        finally:
            import sys
            _finish_output_owners(clients,monitor,stack,evidence,samples,began,error=sys.exc_info()[1])
        end=stack.await_event('http_measure','stopped')
        if end['output_owners'] or end['connections'] or end['remaining_threads']: raise AssertionError('Output cleanup residue')
        return evidence.complete(dict(half_close_bytes=len(half_body),healthy_bytes=len(body),healthy=True,reset=True,cleanup=end))


def _finish_measured_http(conn,sampler,stack,process,evidence,*,error,began,samples,baseline):
    """Attempt each HTTP cleanup stage; later errors never replace the first."""
    first=error; failures=[]; socket_closed=False; sampler_joined=False; child_stopped=False
    def failed(stage,exc):
        nonlocal first
        failures.append(dict(stage=stage,error_type=type(exc).__name__))
        if first is None: first=exc
        elif first is not exc: first.add_note('HTTP '+stage+' cleanup: '+type(exc).__name__)
    try:
        conn.close(); socket_closed=True
    except BaseException as exc: failed('socket',exc)
    try: sampler.__exit__(type(first) if first is not None else None,first,getattr(first,'__traceback__',None))
    except BaseException as exc: failed('sampler',exc)
    try: sampler_joined=not sampler.thread.is_alive()
    except BaseException as exc: failed('sampler-state',exc)
    try: stack.stop('http_measure')
    except BaseException as exc: failed('child',exc)
    try: child_stopped=process.poll() is not None
    except BaseException as exc: failed('child-state',exc)
    try: evidence.events('http_measure')
    except BaseException as exc: failed('events',exc)
    try:
        evidence.result.update(samples=samples,baseline=baseline,elapsed_seconds=time.monotonic()-began,
            request_started_monotonic=began,sampler_joined=sampler_joined,socket_closed=socket_closed,
            child_stopped=child_stopped,http_cleanup_failures=failures)
    except BaseException as exc: failed('evidence',exc)
    if first is not None: raise first


def measured_http(fixture,root,route,*,expected_points=None,mode='ordinary',seconds=120,fault=''):
    import http.client
    import json
    from tests.waveform_measurements import OwnedSampler
    from tests.waveform_stack import Stack
    from tests.test_waveform_config import toml_text
    if not 0<seconds<=120: raise ValueError('Bounded HTTP measurement deadline')
    with MeasurementEvidence(root,'http',dict(route=route,expected_points=expected_points,mode=mode,seconds=seconds,fault=fault)) as evidence,Stack(fixture,evidence_root=root) as stack:
        evidence.stack=stack
        sections=dict(stack.sections); sections['management']={'shared_accounting_bucket':'GW'}
        sections['waveforms']=dict(sections['waveforms'],max_db_connections=1)
        path=stack.root/'http.toml'; path.write_text(toml_text(sections),encoding='utf-8')
        process=stack.launch('http_measure',config=path,fault=fault)
        ready=stack.await_event('http_measure','http_ready'); port=ready['port']
        token=(stack.root/'http-token').read_text(encoding='utf-8')
        baseline=process_memory(process); samples=[baseline]
        evidence.sample(dict(processes=[baseline],host=host_resources(root)))
        def collected(value):
            samples.extend(value['processes']); evidence.sample(value); evidence.events('http_measure')
        def abort():
            if process.poll() is None: stack.kill_owned('http_measure',reason='http_measurement_abort')
        began=time.monotonic(); conn=http.client.HTTPConnection('127.0.0.1',port,timeout=seconds)
        sampler=OwnedSampler(root,lambda:[process],abort,seconds=seconds,began=began,sink=collected)
        sampler.__enter__()
        try:
            conn.request('GET','/api/waveforms'+route,headers={'Authorization':'Bearer '+token})
            response=conn.getresponse(); first=time.monotonic(); content=response.read()
            elapsed=time.monotonic()-began
            sampler.check()  # Reject even a late success racing the sampler interval.
            if response.status!=200: raise AssertionError('Measured HTTP response was not successful')
            result=json.loads(content); data=result.get('data',{}); channels=data.get('channels',[])
            points=len(data.get('points',[])) if mode=='xy' else sum(len(c['points']) for c in channels)
            if expected_points is not None and points!=expected_points: raise AssertionError('Measured output point count differs')
            witnesses=[]
            if channels:
                for index,channel in enumerate(channels):
                    selected=channel['points']; values=[point['value'] for point in selected]
                    indices=[point['index'] for point in selected]
                    if channel['status']!='ok' or not values or any(v is None for v in values) or len(set(indices))!=len(indices):
                        raise AssertionError('Measured calibrated source points are not distinct and valid')
                    if min(values)!=-50000+index*13 or max(values)!=50000+index*13:
                        raise AssertionError('Independent nonflat extrema were not preserved')
                    witnesses.append(dict(channel_id=channel['metadata']['channel_id'],minimum=min(values),maximum=max(values),
                        count=len(values),first=selected[:3],last=selected[-3:]))
            sampler.check()
            sample=process_memory(process); samples.append(sample); evidence.sample(dict(processes=[sample],host=host_resources(root)))
            from hashlib import sha256
            result=dict(baseline=baseline,samples=samples,bytes=len(content),points=points,channels=len(channels),
                response_sha256=sha256(content).hexdigest(),items=len(data.get('items',[])),snapshot_revision=data.get('snapshot_revision'),
                sample_witnesses=witnesses,
                received_before=data.get('received_before'),received_after=data.get('received_after'),
                first_byte_seconds=first-began,elapsed_seconds=elapsed)
        except BaseException as error:
            if sampler.failure is not None or str(error)=='safety.operation_deadline':
                try: abort()
                except BaseException as cleanup: error.add_note('HTTP owned abort: '+type(cleanup).__name__)
            if sampler.failure is not None: raise sampler.failure from error
            raise
        finally:
            import sys
            _finish_measured_http(conn,sampler,stack,process,evidence,error=sys.exc_info()[1],began=began,samples=samples,baseline=baseline)
        final=stack.await_event('http_measure','stopped')
        if final['remaining_threads'] or final['connections'] or final['output_owners']: raise AssertionError('HTTP shutdown residue')
        result['cleanup']=final; result['exit_code']=process.returncode
        return evidence.complete(result)


def maximum_scenarios(fixture,root):
    from tests.waveform_measurements import calibrated_channels,build_capture
    root=Path(root); root.mkdir(exist_ok=False)
    baseline=storage_sample(fixture,root); specs=calibrated_channels(fixture,32)
    inputs=[]; paths=[]
    for index in range(3):
        safety_check(host_resources(root)); path=root/('maximum-'+str(index)+'.owpwave')
        began=time.monotonic(); inputs.append(build_capture(path,channel_specs=specs)); paths.append(path)
        if time.monotonic()-began>120: raise RuntimeError('safety.generator_deadline')
    one=measured_sender(fixture,root,paths[:1],with_receipts=True); three=measured_sender(fixture,root,paths,with_receipts=True)
    intake=measured_job(fixture,root,dict(kind='intake',path=str(paths[0])))
    pk=intake['result']['capture_pk']; base=f'/captures/{pk}/streams/resource/window'
    raw=measured_http(fixture,root,base+'?start=0&count=20000&mode=raw',expected_points=640000)
    summary=measured_http(fixture,root,base+'?start=0&count=104857&mode=summary&budget=2000',expected_points=64000)
    owners=measured_output_owners(fixture,root,base+'?start=0&count=20000&mode=raw')
    after=storage_sample(fixture,root); storage_check(baseline,after)
    return dict(inputs=inputs,sender_one=one,sender_three=three,intake=intake,raw=raw,summary=summary,
        output_owners=owners,storage_before=baseline,storage_after=after,
        lease_comparison=dict(lease_seconds=30,cumulative_cap_seconds=300,assemblies=intake['result']['assemblies']))


def create_pair(fixture,*,count=20001):
    from dataclasses import replace
    from uuid import uuid4
    from backend.waveforms.commands import Command,pair_request_hash
    from backend.waveforms.pairing import PairService
    from tests.waveform_pairing_support import seed_pair,CREATOR,REVIEWER
    from tests.waveform_support import LIMITS
    vector=seed_pair(fixture,two=True,count=count)
    service=PairService(lambda:fixture.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:fixture.connect('reader'))
    body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
    command=Command(CREATOR,uuid4(),0,'Synthetic resource pair','0'*64)
    pair=service.propose(body,replace(command,request_hash=pair_request_hash('propose',None,command,body=body)))
    command=Command(REVIEWER,uuid4(),1,'Synthetic resource pair review','0'*64)
    service.review(pair.id,decision='approve',command=replace(command,request_hash=pair_request_hash('review',pair.id,command,decision='approve')))
    return pair,vector


def audit_scenarios(fixture,root,*,levels=(100,1000,10000)):
    from datetime import datetime,timedelta,timezone
    from urllib.parse import urlencode
    from backend.waveforms.queries import WaveformQueries,utc
    from backend.waveforms.commands import Actor
    from backend.waveforms.errors import WaveformError
    import threading
    query=WaveformQueries(lambda:fixture.connect('reader')); actor=Actor('local','task17-audit-volume','Synthetic audit volume','operator')
    fact=fixture.scalar('SELECT fact_id FROM waveforms.fact_metadata ORDER BY payload_bytes LIMIT 1')
    count=0; results=[]; began=time.monotonic()
    for goal in levels:
        while count<goal:
            operation=time.monotonic(); query.raw(fact,actor); count+=1
            if time.monotonic()-operation>120: raise RuntimeError('safety.audit_operation_deadline')
            if count%100==0: safety_check(host_resources(root))
        pending=threading.Event(); release=threading.Event(); errors=[]
        class DelayedCommit:
            def __init__(self,connection): self.connection=connection
            def __getattr__(self,name): return getattr(self.connection,name)
            def __enter__(self): self.connection.__enter__(); return self
            def __exit__(self,*args): return self.connection.__exit__(*args)
            def commit(self):
                pending.set()
                if not release.wait(120): raise RuntimeError('safety.audit_late_commit_deadline')
                self.connection.commit()
        def late_audit():
            try: WaveformQueries(lambda:DelayedCommit(fixture.connect('reader'))).raw(fact,actor)
            except BaseException as error: errors.append(type(error).__name__)
        writer=threading.Thread(target=late_audit,name='owned-late-audit'); writer.start()
        try:
            if not pending.wait(10): raise AssertionError('Late audit transaction did not reach commit')
            after=datetime.now(timezone.utc)-timedelta(days=1)
            filters=dict(family='raw',actor_id=actor.user_id,received_after=utc(after),page_size=100)
            response=measured_http(fixture,root,'/audit?'+urlencode(filters))
            if response['items']!=min(goal,100): raise AssertionError('Audit page cardinality')
            actual=fixture.scalar("SELECT count(*) FROM waveforms.audit_metadata WHERE actor->>'user_id'=%s AND recorded_at>=%s AND recorded_at<%s",(actor.user_id,after,response['received_before']))
            if actual!=goal: raise AssertionError('Measured audit cardinality differs')
        finally:
            release.set(); writer.join(10)
        if writer.is_alive() or errors: raise AssertionError('Late audit owner did not complete cleanly')
        count+=1
        try: query.fetch('audit',filters|dict(received_before=response['received_before'],snapshot_revision=response['snapshot_revision'],page=2))
        except WaveformError as error:
            if error.code!='waveform.refresh_required': raise
        else: raise AssertionError('Audit exact count staleness not detected')
        results.append(dict(target_rows=goal,actual_rows=actual,response=response,stale_page_rejected=True))
    return dict(levels=results,elapsed_seconds=time.monotonic()-began,timeout_seconds=3,
                limit='Three measured volumes are not constant-time or production-capacity proof')
