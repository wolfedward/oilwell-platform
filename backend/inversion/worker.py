"""Disabled-by-default Windows supervisor with an owned kill-on-close job.

The configured top-level factory is deployment code, never a request field.
Only its canonical configuration crosses spawn; live services/connections do not.
The separate pure numerical interpreter receives a minimal environment and paths.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import multiprocessing
from multiprocessing.context import SpawnProcess
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID
from .contracts import strict_json
from .transactions import InversionError

@dataclass(frozen=True)
class WorkerServices:
    runs: object
    acquisition: object

@dataclass(frozen=True)
class WorkerRuntime:
    factory: object
    configuration: bytes = field(repr=False)
    temp_root: Path
    owner: UUID
    enabled: bool = False
    clock: object = field(default=time.monotonic,repr=False,compare=False)

    def __post_init__(self):
        if (not inspect.isfunction(self.factory) or '<locals>' in self.factory.__qualname__
            or type(self.owner) is not UUID or not isinstance(self.temp_root,Path)
            or type(self.enabled) is not bool or not callable(self.clock)):
            raise ValueError('Serializable installed factory, private root and owner required')
        strict_json(self.configuration)

class OwnedTemp:
    def __init__(self,root):
        self.root=root.resolve(strict=True)
        if not self.root.is_dir() or root.is_symlink() or root.is_junction(): raise ValueError('Private local staging root required')
        self.path=Path(tempfile.mkdtemp(prefix='inversion-',dir=self.root))
        self.identity=(self.path.stat().st_dev,self.path.stat().st_ino)
    def cleanup(self):
        if (self.path.is_symlink() or self.path.is_junction() or self.path.resolve().parent!=self.root
            or (self.path.stat().st_dev,self.path.stat().st_ino)!=self.identity):
            raise InversionError('staging_ownership_changed')
        shutil.rmtree(self.path)

class OwnedJob:
    """An unnamed, noninherited handle; never changes or opens another job."""
    def __init__(self):
        if os.name!='nt': raise InversionError('windows_containment_required')
        import ctypes as c
        from ctypes import wintypes as w
        self.c=c; self.api=c.WinDLL('kernel32',use_last_error=True)
        signatures={
            'CreateJobObjectW':([c.c_void_p,w.LPCWSTR],w.HANDLE),
            'SetInformationJobObject':([w.HANDLE,c.c_int,c.c_void_p,w.DWORD],w.BOOL),
            'AssignProcessToJobObject':([w.HANDLE,w.HANDLE],w.BOOL),
            'IsProcessInJob':([w.HANDLE,w.HANDLE,c.POINTER(w.BOOL)],w.BOOL),
            'TerminateJobObject':([w.HANDLE,w.UINT],w.BOOL),
            'QueryInformationJobObject':([w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p],w.BOOL),
            'CloseHandle':([w.HANDLE],w.BOOL)}
        for name,(args,result) in signatures.items():
            fn=getattr(self.api,name); fn.argtypes=args; fn.restype=result
        class Basic(c.Structure):
            _fields_=[('process_time',c.c_int64),('job_time',c.c_int64),('flags',w.DWORD),
                ('min_working',c.c_size_t),('max_working',c.c_size_t),('active_limit',w.DWORD),
                ('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class Extended(c.Structure):
            _fields_=[('basic',Basic),('io',c.c_uint64*6),('process_memory',c.c_size_t),
                ('job_memory',c.c_size_t),('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
        self.handle=self.api.CreateJobObjectW(None,None)
        if not self.handle: raise c.WinError(c.get_last_error())
        info=Extended(); info.basic.flags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway
        try: self._ok(self.api.SetInformationJobObject(self.handle,9,c.byref(info),c.sizeof(info)))
        except BaseException: self.close(); raise
    def _ok(self,value):
        if not value: raise self.c.WinError(self.c.get_last_error())
    def attach(self,process):
        from ctypes import wintypes as w
        handle=int(process.sentinel)
        self._ok(self.api.AssignProcessToJobObject(self.handle,handle))
        contained=w.BOOL()
        self._ok(self.api.IsProcessInJob(handle,self.handle,self.c.byref(contained)))
        if not contained.value: raise InversionError('containment_failed')
    def active(self):
        c=self.c
        class Accounting(c.Structure):
            _fields_=[('times',c.c_int64*4),('faults',c.c_uint32),('total',c.c_uint32),('active',c.c_uint32),('terminated',c.c_uint32)]
        result=Accounting()
        self._ok(self.api.QueryInformationJobObject(self.handle,1,c.byref(result),c.sizeof(result),None))
        return result.active
    def stop(self):
        self._ok(self.api.TerminateJobObject(self.handle,1))
        until=time.monotonic()+3
        while self.active():
            if time.monotonic()>=until: raise InversionError('owned_process_reap_failed')
            time.sleep(.01)
    def close(self):
        if self.handle: self.api.CloseHandle(self.handle); self.handle=None

def compute_environment(directory):
    """Allowlist, not a denylist: no inherited DB/token/credential-source settings."""
    return dict(SystemRoot=os.environ['SystemRoot'],TEMP=str(directory),TMP=str(directory),
                PYTHONUTF8='1',PYTHONIOENCODING='utf-8')

def launch_compute(input_path,output_path):
    root=str(Path(__file__).resolve().parents[2])
    bootstrap="import sys; from pathlib import Path; sys.path.insert(0,"+repr(root)+"); from backend.inversion.compute_child import compute; compute(Path(sys.argv[1]),Path(sys.argv[2]))"
    return subprocess.Popen([sys.executable,'-I','-B','-c',bootstrap,str(input_path),str(output_path)],
        cwd=input_path.parent,env=compute_environment(input_path.parent),stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW)

class _OwnedSpawnProcess(SpawnProcess):
    """Windows/CPython adapter retaining native ownership during Popen init.

    Popen creates its native handle before serializing, but BaseProcess normally
    receives Popen only after serialization succeeds. Keep that same instance
    reachable on failure; do not replace global spawn/native functions.
    """
    @staticmethod
    def _Popen(process):
        from multiprocessing.popen_spawn_win32 import Popen
        launcher=Popen.__new__(Popen)
        try:
            Popen.__init__(launcher,process)
        except BaseException:
            if getattr(launcher,'_handle',None) is not None:
                process._popen=launcher
                process._sentinel=launcher.sentinel
            elif getattr(launcher,'pid',None) is not None:
                process._ownership_uncertain=True
            raise
        return launcher

def _execute(factory,configuration,attempt,directory,control):
    # Nothing capable of preparation or child creation runs before attachment.
    if control.recv()!='contained': return
    services=factory(configuration)
    run=services.runs.get(attempt.run_id)
    def check():
        if datetime.now(timezone.utc)>=attempt.deadline: raise InversionError('deadline_expired')
        current=services.runs.get(attempt.run_id)
        if current.state!='running' or current.reason_code=='cancel_requested': raise InversionError('run_terminal')
    try:
        from .compute_child import write_input
        from .result_dispatch import read_result
        services.runs._authority(run.authority,run.actor)
        prepared=services.acquisition.prepare_input(run.request,run.actor,check=check)
        check(); input_path=directory/'input.json'; output_path=directory/'result.bin'
        write_input(prepared,input_path,request_bytes=run.request_bytes)
        child=launch_compute(input_path,output_path)
        while child.poll() is None:
            check(); time.sleep(.02)
        if child.wait()!=0: raise InversionError('compute_failed')
        check(); result=read_result(output_path,prepared.snapshot); check()
        authority=services.runs._authority(run.authority,run.actor)
        services.runs.publish(attempt,result,authority)
        control.send(('done',None))
    except InversionError as error:
        # Details never include SQL strings, credentials, exception repr or paths.
        failed=error.code in {'inversion.unavailable','inversion.compute_failed','inversion.deadline_expired',
            'inversion.input_size_limit','inversion.result_size_limit'}
        control.send(('failed' if failed else 'blocked',error.code.removeprefix('inversion.')))
    except (OSError,MemoryError): control.send(('failed','resource_failure'))
    except Exception: control.send(('failed','internal_failure'))
    finally: control.close()

def run_once(runtime:WorkerRuntime)->bool:
    if not runtime.enabled: return False
    services=runtime.factory(runtime.configuration)
    started=runtime.clock(); attempt=services.runs.claim(runtime.owner)
    if attempt is None: return False
    deadline=started+services.runs.limits.deadline_seconds
    owned=None; job=None; process=None; parent=None; child_channel=None; process_started=False
    stop_monitor=threading.Event(); cancelled=threading.Event(); monitor=None
    outcome=('failed','worker_interrupted'); reconciled=False
    try:
        owned=OwnedTemp(runtime.temp_root); job=OwnedJob()
        context=multiprocessing.get_context('spawn'); parent,child_channel=context.Pipe()
        process=_OwnedSpawnProcess(target=_execute,args=(runtime.factory,runtime.configuration,attempt,owned.path,child_channel))
        process.start(); process_started=True
        child_channel.close(); child_channel=None
        job.attach(process); parent.send('contained')
        def watch_cancel():
            while not stop_monitor.wait(.1):
                try:
                    current=services.runs.get(attempt.run_id)
                    if current.reason_code=='cancel_requested' or current.state=='cancelled':
                        cancelled.set(); return
                except Exception:
                    # Retry after the same bounded, stop-aware wait. The parent
                    # deadline remains independent even if a reader is blocked.
                    continue
        monitor=threading.Thread(target=watch_cancel,daemon=True); monitor.start()
        while process.is_alive():
            remaining=deadline-runtime.clock()
            if remaining<=0:
                outcome=('failed','deadline_expired'); break
            if cancelled.is_set():
                outcome=('cancelled','user_cancelled'); break
            if parent.poll(min(.02,remaining)):
                try: outcome=parent.recv()
                except EOFError: pass
                break
        if outcome==('failed','worker_interrupted') and parent.poll():
            try: outcome=parent.recv()
            except EOFError: pass
        # Even a successful attempt may have a surviving descendant. Kill and
        # prove the complete owned job is empty before releasing any capacity.
    except (OSError,MemoryError): outcome=('failed','resource_failure')
    except InversionError: outcome=('failed','containment_failed')
    finally:
        stop_monitor.set()
        try:
            if job: job.stop()
            if process:
                if getattr(process,'_ownership_uncertain',False):
                    raise InversionError('owned_process_reap_failed')
                if process_started or process.pid is not None:
                    if process.is_alive(): process.terminate() # exact gated process if assignment failed
                    process.join(3)
                    if process.is_alive(): raise InversionError('owned_process_reap_failed')
                process.close()
        finally:
            if job: job.close()
            if parent: parent.close()
            if child_channel: child_channel.close()
            if monitor: monitor.join(3.1)
        # Query committed terminal first, including after lost commit responses.
        # If this fails, leave owned staging intact for explicit reconciliation.
        current=services.runs.get(attempt.run_id)
        if current.state not in ('completed','blocked','failed','cancelled'):
            state,reason=outcome
            if state=='done': state,reason='failed','commit_not_observed'
            current=services.runs.finish(attempt,state,reason)
        reconciled=current.state in ('completed','blocked','failed','cancelled')
        if owned and reconciled: owned.cleanup()
    return True
