from dataclasses import replace
from fractions import Fraction as Q
from hashlib import sha256
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from backend.inversion.contracts import canonical_bytes
from backend.inversion.input_contracts import InputSnapshot
from backend.inversion.transactions import InversionError


class ComputeV2Tests(unittest.TestCase):
    def require_versioned_transport(self):
        from importlib.util import find_spec
        self.assertIsNotNone(find_spec('backend.inversion.result_dispatch'),'v2 computation transport missing')

    def test_compute_has_no_reference_dependency(self):
        self.require_versioned_transport()
        from tests.inversion_result_v2_support import prepared_input,rows
        from backend.inversion.compute_child import write_input,compute,load_input
        from backend.inversion.result_dispatch import read_result
        original=prepared_input();body=json.loads(original.snapshot.canonical)
        for s in body['samples']['load']:
            if s['value_si'] is not None:s['value_si']=str(Q(s['value_si'])+2000)
        raw=canonical_bytes(body);snapshot=InputSnapshot(body,raw,sha256(raw).hexdigest())
        changed=replace(original,snapshot=snapshot,reference_inputs=replace(original.reference_inputs,
            load=replace(original.reference_inputs.load,samples=tuple(replace(s,value_si=None if s.value_si is None else s.value_si+2000) for s in original.reference_inputs.load.samples))))
        results=[]
        with tempfile.TemporaryDirectory() as directory:
            for index,item in enumerate((original,changed)):
                path=Path(directory)/f'{index}.json';output=Path(directory)/f'{index}.bin'
                write_input(item,path,request_bytes=0)
                loaded=load_input(path);self.assertEqual(loaded[0],item.prediction_inputs)
                compute(path,output);results.append(read_result(output,item.snapshot))
        self.assertEqual(rows(results[0],'prediction'),rows(results[1],'prediction'))
        self.assertEqual(rows(results[0],'proof'),rows(results[1],'proof'))
        self.assertNotEqual(results[0].body['comparison'],results[1].body['comparison'])

    def test_wrong_model_or_snapshot_schema_rejected(self):
        self.require_versioned_transport()
        from tests.inversion_result_v2_support import prepared_input
        from backend.inversion.compute_child import write_input,load_input
        from backend.inversion.errors import InversionContractError
        original=prepared_input()
        with tempfile.TemporaryDirectory() as directory:
            for mode in ('model','schema','sample'):
                path=Path(directory)/(mode+'.json');write_input(original,path,request_bytes=0)
                value=json.loads(path.read_bytes());body=value['snapshot']
                if mode=='model':body['applicability']['model_body']['artifact']['algorithm']='beam_energy_balance/1'
                elif mode=='schema':body['schema']='inversion.input/999'
                else:body['samples']['active_power'][10]['value_si']='123'
                # Mutation of a test artifact, never source files or real archives.
                path.write_bytes(canonical_bytes(value))
                with self.subTest(mode=mode),self.assertRaises((InversionError,InversionContractError)):load_input(path)

    def test_runtime_missing_no_fallback(self):
        self.require_versioned_transport()
        from tests.inversion_result_v2_support import prepared_input
        from backend.inversion.compute_child import write_input,compute
        item=prepared_input()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input';output=Path(directory)/'output';write_input(item,path,request_bytes=0)
            with patch('backend.inversion.interval_runtime.require_runtime',side_effect=InversionError('proof_runtime_unavailable')):
                with self.assertRaises(InversionError):compute(path,output)
            self.assertFalse(output.exists())

    def test_real_credential_free_child_completes_within_fixed_deadline(self):
        self.require_versioned_transport()
        import time
        from tests.inversion_result_v2_support import prepared_input,rows
        from backend.inversion.compute_child import write_input
        from backend.inversion.worker import launch_compute
        from backend.inversion.result_dispatch import read_result
        item=prepared_input()
        with tempfile.TemporaryDirectory(prefix='inversion-v2-child-') as directory:
            source=Path(directory)/'input.json';target=Path(directory)/'result.bin';write_input(item,source,request_bytes=0)
            started=time.monotonic();child=launch_compute(source,target)
            try:
                while child.poll() is None and time.monotonic()-started<180:time.sleep(.05)
                self.assertEqual(child.poll(),0,'owned numerical child failed or exceeded the fixed deadline')
                result=read_result(target,item.snapshot)
                self.assertEqual(result.status,'applicable');self.assertEqual(len(rows(result,'proof')),1025)
                print('owned numerical child seconds',round(time.monotonic()-started,3),flush=True)
            finally:
                if child.poll() is None:child.kill()
                child.wait(timeout=3)


class OwnedWorkerV2Tests(unittest.TestCase):
    def exercise(self,cancel):
        import os,time
        from concurrent.futures import ThreadPoolExecutor
        from multiprocessing.process import BaseProcess
        from uuid import uuid4
        from backend.inversion.worker import WorkerRuntime,WorkerServices,run_once,OwnedJob
        from tests.inversion_owned_worker_v2_support import Runs,PARENTS,factory,accelerated_clock
        runs=Runs();key=str(uuid4());PARENTS[key]=WorkerServices(runs,None)
        stopped=[];stop=OwnedJob.stop;join=BaseProcess.join;finish=runs.finish
        def observed_stop(job):
            stop(job);self.assertEqual(job.active(),0);stopped.append('empty')
        def observed_join(process,*args,**kwargs):
            result=join(process,*args,**kwargs);self.assertFalse(process.is_alive());stopped.append('joined');return result
        def settled(*args):
            self.assertEqual(stopped,['empty','joined']);return finish(*args)
        runs.finish=settled
        try:
            with tempfile.TemporaryDirectory(prefix='inversion-v2-owned-') as directory:
                root=Path(directory);configuration=canonical_bytes(dict(key=key,parent_pid=os.getpid(),root=str(root)))
                runtime=WorkerRuntime(factory,configuration,root,uuid4(),enabled=True,clock=accelerated_clock)
                with patch.object(OwnedJob,'stop',observed_stop),patch.object(BaseProcess,'join',observed_join):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future=pool.submit(run_once,runtime);until=time.monotonic()+8
                        while not (root/'contained-ready.json').exists() and not future.done() and time.monotonic()<until:time.sleep(.02)
                        self.assertTrue((root/'contained-ready.json').exists(),'child never passed containment gate')
                        if cancel:runs.record.reason_code='cancel_requested'
                        self.assertTrue(future.result(timeout=8))
                self.assertEqual([p.name for p in root.iterdir() if p.is_dir()],[])
            self.assertEqual(runs.settled,[('cancelled','user_cancelled') if cancel else ('failed','deadline_expired')])
        finally:PARENTS.pop(key,None)

    def test_timeout_reaps_only_owned_process_tree(self):self.exercise(False)
    def test_cancel_reaps_only_owned_process_tree(self):self.exercise(True)

    def test_late_or_wrong_owner_attempt_is_fenced(self):
        from datetime import datetime,timezone
        from types import SimpleNamespace as N
        from uuid import uuid4
        from backend.inversion.runs import RunService,Attempt
        owner=uuid4();run=N(generation=2,owner_id=owner)
        attempt=Attempt(uuid4(),uuid4(),2,owner,datetime.now(timezone.utc))
        self.assertIsNone(RunService._fence(run,attempt))
        for changed in (replace(attempt,generation=1),replace(attempt,owner_id=uuid4())):
            with self.assertRaises(InversionError) as caught:RunService._fence(run,changed)
            self.assertEqual(caught.exception.code,'inversion.attempt_fenced')
