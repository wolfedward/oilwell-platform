"""Task9 isolated helpers; no child invokes a resetting database fixture."""
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4
import json
import time
from psycopg.types.json import Jsonb
from backend.inversion.contracts import AuthorityRef, AuthoritySnapshot, InversionCommand, VersionPrecondition, canonical_bytes
from backend.inversion.input_contracts import parse_run_request
from backend.waveforms.commands import Actor
from tests.inversion_support import InversionDatabaseCase
from tests.test_inversion_input import request_body
from tests.inversion_governance_support import GovernanceCase
from tests.waveform_dataset_support import DatasetCase

ACTOR=Actor('local','1','Run author','algorithm_engineer')
REF=AuthorityRef('local',1,1)
CAPABILITIES=frozenset(('inversion.run','waveform.read','waveform.raw.read','waveform.export'))

def accelerated_clock(): return time.monotonic()*60

def allow(actor,capability): pass

def process_exited(pid):
    import ctypes as c
    from ctypes import wintypes as w
    api=c.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD]; api.OpenProcess.restype=w.HANDLE
    api.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD]; api.WaitForSingleObject.restype=w.DWORD
    api.CloseHandle.argtypes=[w.HANDLE]
    handle=api.OpenProcess(0x100000,False,pid)
    if not handle: return c.get_last_error()==87
    try: return api.WaitForSingleObject(handle,0)==0
    finally: api.CloseHandle(handle)

def crash_tree_child(directory,control):
    import subprocess,sys
    from pathlib import Path
    from backend.inversion.worker import compute_environment
    if control.recv()!='contained': return
    child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)'],
        env=compute_environment(Path(directory)),creationflags=subprocess.CREATE_NO_WINDOW)
    Path(directory,'grandchild.pid').write_text(str(child.pid))
    time.sleep(60)

def crash_tree_supervisor(directory,control):
    import multiprocessing
    from backend.inversion.worker import OwnedJob
    job=OwnedJob(); context=multiprocessing.get_context('spawn'); parent,gate=context.Pipe()
    child=context.Process(target=crash_tree_child,args=(directory,gate)); child.start(); gate.close()
    job.attach(child); parent.send('contained'); control.send(child.pid)
    time.sleep(60) # Parent test abruptly terminates this owner: no finally runs.

def worker_services(configuration):
    import psycopg
    from backend.inversion.runs import RunService
    from backend.inversion.capacity import InversionLimits
    from backend.inversion.worker import WorkerServices
    settings=json.loads(configuration)
    service=RunService(lambda:psycopg.connect(**settings['operator']),
        read_connect=lambda:psycopg.connect(**settings['reader']),limits=InversionLimits(2147483648),
        authorize=allow,refresh_authority=authority)
    if settings['mode'] in ('serialize_block','disk_full'):
        import backend.inversion.compute_child as compute
        def injected(*args,**kwargs):
            if settings['mode']=='disk_full': raise OSError(28,'Injected full disk')
            time.sleep(60)
        compute.write_input=injected
    class Preparation:
        def prepare_input(self,request,actor,*,check):
            if settings['mode'] in ('serialize_block','disk_full'):
                from types import SimpleNamespace
                return SimpleNamespace()
            if settings['mode']=='nested_block':
                import subprocess,sys,os
                from pathlib import Path
                from backend.inversion.worker import compute_environment
                child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)'],
                    env=compute_environment(Path(settings['root'])),creationflags=subprocess.CREATE_NO_WINDOW)
                Path(settings['root'],'nested.pid').write_text(str(child.pid))
            time.sleep(60)
    return WorkerServices(service,Preparation())

def real_worker_services(configuration):
    import psycopg
    from backend.inversion.runs import RunService
    from backend.inversion.capacity import InversionLimits
    from backend.inversion.input_acquisition import InputAcquisition
    from backend.inversion.worker import WorkerServices
    from backend.waveforms.member_exports import MemberResearchExporter
    from tests.waveform_support import LIMITS
    settings=json.loads(configuration)
    operator=lambda:psycopg.connect(**settings['operator'])
    reader=lambda:psycopg.connect(**settings['reader'])
    runs=RunService(operator,read_connect=reader,limits=InversionLimits(2147483648),authorize=allow,refresh_authority=authority)
    exporter=MemberResearchExporter(lambda:psycopg.connect(**settings['wave_operator']),limits=LIMITS,
        shared_accounting_bucket='GW',read_connect=lambda:psycopg.connect(**settings['wave_reader']))
    acquirer=InputAcquisition(operator,read_connect=reader,exporter=exporter,authorize=allow,accounting_bucket='GW')
    return WorkerServices(runs,acquirer)

def authority(ref):
    return AuthoritySnapshot(ref,CAPABILITIES,datetime.now(timezone.utc),1)

def command(version=None,key=None):
    return InversionCommand(ACTOR,key or uuid4(),VersionPrecondition('absent') if version is None else VersionPrecondition('match',version),'Synthetic durable run test')

class RunCase(GovernanceCase,DatasetCase):
    def setUp(self):
        super().setUp()
        from backend.inversion.runs import RunService
        from tests.inversion_vectors import acquired_case
        self.case=acquired_case(self)
        self.service=RunService(self.inversion_fixture.connect,read_connect=lambda:self.inversion_fixture.connect('reader'),limits=self.inversion_fixture.limits,
                                authorize=self.authorize,refresh_authority=self.refresh)
        self.request=parse_run_request(canonical_bytes(self.case['request']))

    def scalar(self,statement,params=(),role='reader'):
        if 'inversion.' not in statement: return super().scalar(statement,params,role=role)
        return self.inversion_fixture.scalar(statement,params,role)

    def prepared(self):
        from backend.inversion.input_acquisition import InputAcquisition
        service=InputAcquisition(self.inversion_fixture.connect,read_connect=lambda:self.inversion_fixture.connect('reader'),
            exporter=self.case['exporter'],authorize=self.authorize,accounting_bucket='GW')
        return service.prepare_input(self.request,ACTOR,check=lambda:None)

    def result(self,*,applicable=True):
        from backend.inversion.result_codec import encode
        from backend.inversion.solver import predict
        from backend.inversion.comparison import compare
        prepared=self.prepared()
        if applicable:
            # Deterministic codec/publication seam; kernel accuracy is independently
            # tested, and end-to-end worker keeps its real not_applicable result.
            from tests.test_inversion_comparison import prediction as known_prediction
            prediction=known_prediction()
        else: prediction=predict(prepared.prediction_inputs,prepared.parameters,prepared.policy)
        report=compare(prediction,prepared.reference_inputs,prepared.policy)
        return encode(prediction,report,prepared.snapshot)

    def authorize(self,actor,capability):
        if not self.allowed:
            from backend.inversion.transactions import InversionError
            raise InversionError('forbidden',status=403)

    def refresh(self,ref):
        self.authorize(ACTOR,'inversion.run')
        return authority(ref)

    def create(self): return self.service.create(self.request,command(),REF)
