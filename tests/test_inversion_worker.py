import importlib.util
import unittest
from dataclasses import replace
from fractions import Fraction
from hashlib import sha256
from types import MappingProxyType
from tests.inversion_worker import RunCase,worker_services,accelerated_clock

class SupervisorTests(RunCase):
    def check_start_failure(self,partially_started):
        import tempfile
        from pathlib import Path
        from uuid import uuid4
        from multiprocessing.process import BaseProcess
        from unittest.mock import patch
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.runs import RunService
        from backend.inversion.worker import WorkerRuntime,run_once,OwnedJob
        events=[]; start=BaseProcess.start; join=BaseProcess.join; stop=OwnedJob.stop; finish=RunService.finish
        def failing_start(process):
            if partially_started: start(process)
            raise OSError('injected process start failure')
        def observed_stop(job):
            stop(job); self.assertEqual(job.active(),0); events.append('job_empty')
        def observed_join(process,*args,**kwargs):
            self.assertTrue(partially_started,'must not join a never-started process')
            value=join(process,*args,**kwargs); self.assertFalse(process.is_alive()); events.append('joined'); return value
        def observed_finish(service,*args,**kwargs):
            self.assertEqual(events,['job_empty','joined'] if partially_started else ['job_empty'])
            return finish(service,*args,**kwargs)
        run=self.create()
        with tempfile.TemporaryDirectory() as directory:
            runtime=WorkerRuntime(worker_services,canonical_bytes(dict(operator=self.inversion_fixture.settings['operator'],reader=self.inversion_fixture.settings['reader'],mode='prepare_block',root=directory)),Path(directory),uuid4(),enabled=True)
            with patch.object(BaseProcess,'start',failing_start),patch.object(BaseProcess,'join',observed_join),patch.object(OwnedJob,'stop',observed_stop),patch.object(RunService,'finish',observed_finish):
                self.assertTrue(run_once(runtime))
            self.assertEqual(list(Path(directory).iterdir()),[])
        current=self.service.get(run.id)
        self.assertEqual((current.state,current.reason_code),('failed','resource_failure'))
        self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),0)
        self.assertEqual(self.scalar('SELECT running FROM inversion.capacity'),0)

    def test_process_start_failure_reconciles_owned_resources(self):
        self.check_start_failure(False)

    def test_partially_started_process_is_reaped_before_settlement(self):
        self.check_start_failure(True)

    def test_spawn_serialization_failure_reaps_unassigned_native_child(self):
        import _winapi
        from multiprocessing import reduction
        from multiprocessing.context import get_spawning_popen
        from multiprocessing.process import BaseProcess
        from unittest.mock import patch
        from backend.inversion.runs import RunService
        dump=reduction.dump; handles=[]
        finish=RunService.finish
        def observed_finish(service,*args,**kwargs):
            self.assertEqual(len(handles),1)
            self.assertEqual(_winapi.WaitForSingleObject(handles[0],0),_winapi.WAIT_OBJECT_0)
            return finish(service,*args,**kwargs)
        def failing_dump(obj,*args,**kwargs):
            if isinstance(obj,BaseProcess):
                self.assertIsNone(obj._popen)
                launcher=get_spawning_popen()
                self.assertIsNotNone(launcher.pid)
                current=_winapi.GetCurrentProcess()
                handles.append(_winapi.DuplicateHandle(current,launcher._handle,current,0,False,_winapi.DUPLICATE_SAME_ACCESS))
                raise OSError('injected post-create serialization failure')
            return dump(obj,*args,**kwargs)
        try:
            with patch.object(reduction,'dump',failing_dump),patch.object(RunService,'finish',observed_finish):
                self.check_start_failure(True)
            self.assertEqual(len(handles),1)
            self.assertEqual(_winapi.WaitForSingleObject(handles[0],0),_winapi.WAIT_OBJECT_0)
        finally:
            for handle in handles: _winapi.CloseHandle(handle)

    def test_transient_cancel_read_failure_keeps_monitoring(self):
        from concurrent.futures import ThreadPoolExecutor
        import tempfile,threading,time
        from pathlib import Path
        from uuid import uuid4
        from multiprocessing.process import BaseProcess
        from unittest.mock import patch
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.runs import RunService
        from backend.inversion.worker import WorkerRuntime,run_once,OwnedJob
        from tests.inversion_worker import command
        failed_read=threading.Event(); events=[]; outcomes=[]
        get=RunService.get; stop=OwnedJob.stop; join=BaseProcess.join; finish=RunService.finish
        def flaky_get(service,*args,**kwargs):
            if threading.current_thread().daemon and not failed_read.is_set():
                failed_read.set(); raise OSError('injected transient cancellation read failure')
            return get(service,*args,**kwargs)
        def observed_stop(job):
            stop(job); self.assertEqual(job.active(),0); events.append('job_empty')
        def observed_join(process,*args,**kwargs):
            value=join(process,*args,**kwargs); self.assertFalse(process.is_alive()); events.append('joined'); return value
        def observed_finish(service,attempt,state,reason):
            self.assertEqual(events,['job_empty','joined']); outcomes.append((state,reason))
            return finish(service,attempt,state,reason)
        run=self.create()
        with tempfile.TemporaryDirectory() as directory:
            # Ten real seconds of deadline budget; cancellation must finish well before it.
            runtime=WorkerRuntime(worker_services,canonical_bytes(dict(operator=self.inversion_fixture.settings['operator'],reader=self.inversion_fixture.settings['reader'],mode='prepare_block',root=directory)),Path(directory),uuid4(),enabled=True,clock=lambda:time.monotonic()*18)
            with patch.object(RunService,'get',flaky_get),patch.object(OwnedJob,'stop',observed_stop),patch.object(BaseProcess,'join',observed_join),patch.object(RunService,'finish',observed_finish):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(run_once,runtime)
                    self.assertTrue(failed_read.wait(5))
                    current=self.service.get(run.id); self.service.cancel(run.id,command(current.version))
                    cancelled_at=time.monotonic()
                    self.assertTrue(future.result(timeout=15))
                    elapsed=time.monotonic()-cancelled_at
            self.assertEqual(list(Path(directory).iterdir()),[])
        self.assertEqual(outcomes,[('cancelled','user_cancelled')])
        self.assertLess(elapsed,4)
        self.assertEqual(self.service.get(run.id).state,'cancelled')
        self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),0)

    def test_real_acquisition_compute_codec_and_durable_outcome(self):
        import tempfile
        from pathlib import Path
        from uuid import uuid4
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.worker import WorkerRuntime,run_once
        from tests.inversion_worker import real_worker_services
        run=self.create()
        with tempfile.TemporaryDirectory() as directory:
            configuration=canonical_bytes(dict(operator=self.inversion_fixture.settings['operator'],reader=self.inversion_fixture.settings['reader'],
                wave_operator=self.settings['operator'],wave_reader=self.settings['reader']))
            runtime=WorkerRuntime(real_worker_services,configuration,Path(directory),uuid4(),enabled=True)
            self.assertTrue(run_once(runtime))
            self.assertEqual(list(Path(directory).iterdir()),[])
        current=self.service.get(run.id)
        self.assertEqual((current.state,current.reason_code),('blocked','not_applicable'))
        self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),0)

    def test_timeout_also_covers_blocking_serialization_and_disk_full(self):
        import tempfile
        from pathlib import Path
        from uuid import uuid4
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.worker import WorkerRuntime,run_once
        for mode,reason in (('serialize_block','deadline_expired'),('disk_full','resource_failure')):
            run=self.create()
            with tempfile.TemporaryDirectory() as directory:
                runtime=WorkerRuntime(worker_services,canonical_bytes(dict(operator=self.inversion_fixture.settings['operator'],reader=self.inversion_fixture.settings['reader'],mode=mode,root=directory)),Path(directory),uuid4(),enabled=True,clock=accelerated_clock)
                self.assertTrue(run_once(runtime)); self.assertEqual(list(Path(directory).iterdir()),[])
            self.assertEqual(self.service.get(run.id).reason_code,reason)

    def test_explicit_cancel_reaps_blocked_preparation_promptly(self):
        from concurrent.futures import ThreadPoolExecutor
        import tempfile,time
        from pathlib import Path
        from uuid import uuid4
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.worker import WorkerRuntime,run_once
        from tests.inversion_worker import command
        run=self.create()
        with tempfile.TemporaryDirectory() as directory:
            runtime=WorkerRuntime(worker_services,canonical_bytes(dict(operator=self.inversion_fixture.settings['operator'],reader=self.inversion_fixture.settings['reader'],mode='prepare_block',root=directory)),Path(directory),uuid4(),enabled=True,clock=accelerated_clock)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(run_once,runtime)
                until=time.monotonic()+5
                while self.service.get(run.id).state!='running' and time.monotonic()<until: time.sleep(.01)
                current=self.service.get(run.id); self.service.cancel(run.id,command(current.version))
                self.assertTrue(future.result(timeout=8))
        self.assertEqual(self.service.get(run.id).state,'cancelled')
    def test_timeout_covers_prepare_and_serialize(self):
        import tempfile
        from pathlib import Path
        from uuid import uuid4
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.worker import WorkerRuntime,run_once,OwnedJob
        from backend.inversion.runs import RunService
        from multiprocessing.process import BaseProcess
        from unittest.mock import patch
        events=[]; stop=OwnedJob.stop; join=BaseProcess.join; finish=RunService.finish
        def observed_stop(job):
            stop(job); self.assertEqual(job.active(),0); events.append('job_empty')
        def observed_join(process,*args,**kwargs):
            value=join(process,*args,**kwargs); self.assertFalse(process.is_alive()); events.append('joined'); return value
        def observed_finish(service,*args,**kwargs):
            self.assertEqual(events,['job_empty','joined']); return finish(service,*args,**kwargs)
        run=self.create()
        with tempfile.TemporaryDirectory() as directory:
            runtime=WorkerRuntime(worker_services,canonical_bytes(dict(operator=self.inversion_fixture.settings['operator'],reader=self.inversion_fixture.settings['reader'],mode='prepare_block',root=directory)),Path(directory),uuid4(),enabled=True,clock=accelerated_clock)
            with patch.object(OwnedJob,'stop',observed_stop),patch.object(BaseProcess,'join',observed_join),patch.object(RunService,'finish',observed_finish):
                self.assertTrue(run_once(runtime))
            self.assertEqual(list(Path(directory).iterdir()),[])
        self.assertEqual((self.service.get(run.id).state,self.service.get(run.id).reason_code),('failed','deadline_expired'))
        self.assertEqual(self.scalar('SELECT reserved_bytes FROM inversion.capacity'),0)

    def test_timeout_reaps_nested_owned_descendant(self):
        import tempfile
        from pathlib import Path
        from uuid import uuid4
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.worker import WorkerRuntime,run_once
        from tests.inversion_worker import process_exited
        run=self.create()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            runtime=WorkerRuntime(worker_services,canonical_bytes(dict(operator=self.inversion_fixture.settings['operator'],reader=self.inversion_fixture.settings['reader'],mode='nested_block',root=directory)),root,uuid4(),enabled=True,clock=accelerated_clock)
            self.assertTrue(run_once(runtime))
            pid=int((root/'nested.pid').read_text())
            self.assertTrue(process_exited(pid))
            self.assertEqual([p.name for p in root.iterdir()],['nested.pid'])
        self.assertEqual(self.service.get(run.id).state,'failed')

    def test_disabled_worker_does_not_claim_or_create_paths(self):
        import tempfile
        from pathlib import Path
        from uuid import uuid4
        from backend.inversion.worker import WorkerRuntime,run_once
        run=self.create()
        with tempfile.TemporaryDirectory() as directory:
            runtime=WorkerRuntime(worker_services,b'{}',Path(directory),uuid4())
            self.assertFalse(run_once(runtime))
            self.assertEqual(list(Path(directory).iterdir()),[])
        self.assertEqual(self.service.get(run.id).state,'queued')

class CodecTests(unittest.TestCase):
    def test_supervisor_crash_closes_job_and_kills_all_descendants(self):
        import multiprocessing,tempfile,time
        from pathlib import Path
        from tests.inversion_worker import crash_tree_supervisor,process_exited
        context=multiprocessing.get_context('spawn'); parent,control=context.Pipe()
        with tempfile.TemporaryDirectory() as directory:
            owner=context.Process(target=crash_tree_supervisor,args=(directory,control)); owner.start(); control.close()
            try:
                self.assertTrue(parent.poll(10)); child=parent.recv(); marker=Path(directory,'grandchild.pid')
                until=time.monotonic()+10
                while not marker.exists() and time.monotonic()<until: time.sleep(.02)
                grandchild=int(marker.read_text())
                owner.terminate(); owner.join(3); self.assertFalse(owner.is_alive())
                until=time.monotonic()+3
                while not (process_exited(child) and process_exited(grandchild)) and time.monotonic()<until: time.sleep(.02)
                self.assertTrue(process_exited(child)); self.assertTrue(process_exited(grandchild))
            finally:
                if owner.is_alive(): owner.terminate(); owner.join(3)
                owner.close(); parent.close()
    def test_fixed_column_validation_rejects_unknown_shape(self):
        from backend.inversion.result_codec import encode,PreparedResult
        from backend.inversion.comparison import compare
        from tests.test_inversion_comparison import prediction,reference,policy
        pred=prediction(); result=encode(pred,compare(pred,reference(),policy()),self.snapshot())
        with self.assertRaises(RuntimeError): PreparedResult(result.snapshot,result.metadata,(b'{"kind":"arbitrary","rows":[]}',))
    def test_limits_clean_only_owned_temp(self):
        import tempfile
        from pathlib import Path
        from backend.inversion.worker import OwnedTemp
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); neighbor=root/'unrelated'; neighbor.mkdir()
            owned=OwnedTemp(root); owned.cleanup()
            self.assertTrue(neighbor.is_dir())

    def test_compute_launch_has_no_inherited_secrets(self):
        import tempfile,os
        from pathlib import Path
        from unittest.mock import patch
        from backend.inversion.worker import launch_compute
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'PGPASSWORD':'sentinel-secret','AUTH_TOKEN':'bearer-secret'}):
            with patch('backend.inversion.worker.subprocess.Popen') as start:
                launch_compute(Path(directory)/'input.json',Path(directory)/'result.bin')
                args,kwargs=start.call_args
                self.assertNotIn('PGPASSWORD',kwargs['env']); self.assertNotIn('AUTH_TOKEN',kwargs['env'])
                self.assertNotIn('sentinel-secret',str(args))
                self.assertTrue(kwargs['close_fds']); self.assertIn('-I',args[0])
    def snapshot(self):
        from backend.inversion.input_contracts import InputSnapshot
        from backend.inversion.contracts import canonical_bytes
        from tests.inversion_vectors import parameter_body
        body={'schema':'inversion.input/1','applicability':{'parameter_body':parameter_body()},'observation':{'relative_alignment_ns':'1/3','uncertainty_ns':'7/2'}}
        raw=canonical_bytes(body)
        return InputSnapshot(body,raw,sha256(raw).hexdigest())

    def test_fixed_columns_preserve_exact_time_index_and_fraction_diagnostics(self):
        import json
        from backend.inversion.result_codec import encode
        from backend.inversion.comparison import compare
        from tests.test_inversion_comparison import prediction,reference,policy
        pred=prediction(); point=replace(pred.points[0],time_ns=Fraction(9007199254740993,7),power_index=9007199254740993,
            intermediates=MappingProxyType({'theta_error_estimate_rad':Fraction(1,17)}))
        report=compare(pred,reference(),policy())
        pred=replace(pred,points=(point,)+pred.points[1:])
        encoded=encode(pred,report,self.snapshot())
        block=json.loads(encoded.chunks[0]); row=dict(zip(block['columns'],block['rows'][0]))
        self.assertEqual(row['time_ns'],'9007199254740993/7')
        self.assertEqual(row['power_index'],'9007199254740993')
        self.assertEqual(row['theta_error_estimate_rad'],'1/17')
        self.assertEqual(encoded.snapshot.body['observation']['relative_alignment_ns'],'1/3')
        self.assertLessEqual(encoded.result_bytes,16*1024**2)
        self.assertFalse(encoded.body['field_accuracy_certified'])

    def test_result_metadata_alone_is_subject_to_aggregate_limit(self):
        from backend.inversion.result_codec import encode
        from backend.inversion.comparison import compare
        from tests.test_inversion_comparison import prediction,reference,policy
        pred=prediction(); report=compare(pred,reference(),policy())
        pred=replace(pred,metadata={'oversize':'x'*(16*1024**2)})
        with self.assertRaises(RuntimeError): encode(pred,report,self.snapshot())

class WorkerFoundationTests(unittest.TestCase):
    def test_supervised_worker_and_codec_exist(self):
        for name in ('worker','compute_child','result_codec'):
            self.assertIsNotNone(importlib.util.find_spec('backend.inversion.'+name),name+' is required')
