"""Debugger-style fault injection ONLY in an attested owned numerical child.

This is an opt-in local test tool, not a server switch or API. Supervisors stay
alive. Every acquired suspend increment is undone once on abort, never to zero.
Reference: learn.microsoft.com/windows/win32/api/processthreadsapi/
nf-processthreadsapi-suspendthread (debugging only, not synchronization).
"""
from contextlib import contextmanager,ExitStack
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import time

from backend.inversion.contracts import canonical_bytes
from tools.review_deploy import _marker
from tools.review_deploy_process import inspect_pid,owned_process
from .lab import checked_handle
from .storage import safe_path,strict_json


def command_argv(command):
    if os.name!='nt' or type(command) is not str or len(command)>32768:raise ValueError('bounded Windows command required')
    shell=c.WinDLL('shell32',use_last_error=True);kernel=c.WinDLL('kernel32',use_last_error=True)
    shell.CommandLineToArgvW.argtypes=[w.LPCWSTR,c.POINTER(c.c_int)];shell.CommandLineToArgvW.restype=c.POINTER(w.LPWSTR)
    kernel.LocalFree.argtypes=[c.c_void_p];kernel.LocalFree.restype=c.c_void_p
    count=c.c_int();argv=shell.CommandLineToArgvW(command,c.byref(count))
    if not argv:raise c.WinError(c.get_last_error())
    try:return [argv[i] for i in range(count.value)]
    finally:kernel.LocalFree(argv)


def validate_candidate(root,worker,supervisor,compute,request):
    root=safe_path(root)
    if supervisor.get('parent_pid')!=worker['pid'] or compute.get('parent_pid')!=supervisor['pid']:
        raise ValueError('compute ancestry mismatch')
    if 'spawn_main' not in supervisor['command']:raise ValueError('not a fresh spawn supervisor')
    argv=command_argv(compute['command'])
    code=str((root/'code').resolve())
    bootstrap="import sys; from pathlib import Path; sys.path.insert(0,"+repr(code)+"); from backend.inversion.compute_child import compute; compute(Path(sys.argv[1]),Path(sys.argv[2]))"
    if (len(argv)!=6 or argv[1:4]!=['-I','-c',bootstrap] or
        Path(argv[0]).resolve()!=(root/'venv/Scripts/python.exe').resolve()):
        raise ValueError('not the installed credential-free numerical command')
    input_path=safe_path(Path(argv[4]));output_path=safe_path(Path(argv[5]),missing=True)
    stage=input_path.parent;staging=safe_path(root/'private/inversion-staging')
    if (input_path.name!='input.json' or output_path!=stage/'result.bin' or stage.parent!=staging or
        not stage.name.startswith('inversion-') or input_path.stat().st_size>64*1024**2):
        raise ValueError('unowned compute staging paths')
    body=strict_json(input_path.read_bytes())
    if canonical_bytes(body['snapshot']['request'])!=canonical_bytes(request):raise ValueError('different run input')
    return stage


def children(parent):
    if type(parent) is not int or parent<=0:raise ValueError('exact parent required')
    command=("[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        f"@(Get-CimInstance Win32_Process -Filter 'ParentProcessId = {parent}' | "
        "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,CreationDate) | ConvertTo-Json -Compress")
    result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',command],capture_output=True,
        timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode or len(result.stdout)>262144:raise ValueError('bounded child inventory unavailable')
    body=json.loads(result.stdout.decode('utf-8-sig') or '[]')
    if isinstance(body,dict):body=[body]
    if type(body) is not list or len(body)>16:raise ValueError('ambiguous child inventory')
    return [dict(pid=int(p['ProcessId']),parent_pid=int(p['ParentProcessId']),created=str(p['CreationDate']),
        executable=str(p['ExecutablePath']),command=str(p['CommandLine'])) for p in body]


class _NativeProcess:
    def __init__(self,pid):
        if os.name!='nt':raise ValueError('Windows process handles required')
        self.pid=pid;self.api=c.WinDLL('kernel32',use_last_error=True)
        signatures={'OpenProcess':([w.DWORD,w.BOOL,w.DWORD],w.HANDLE),
            'CloseHandle':([w.HANDLE],w.BOOL),'WaitForSingleObject':([w.HANDLE,w.DWORD],w.DWORD),
            'GetProcessTimes':([w.HANDLE,c.POINTER(w.FILETIME),c.POINTER(w.FILETIME),c.POINTER(w.FILETIME),c.POINTER(w.FILETIME)],w.BOOL),
            'OpenThread':([w.DWORD,w.BOOL,w.DWORD],w.HANDLE),'GetProcessIdOfThread':([w.HANDLE],w.DWORD),
            'SuspendThread':([w.HANDLE],w.DWORD),'ResumeThread':([w.HANDLE],w.DWORD)}
        for name,(args,result) in signatures.items():
            fn=getattr(self.api,name);fn.argtypes=args;fn.restype=result
        self.handle=self.api.OpenProcess(0x1000|0x100000,False,pid)
        if not self.handle:raise c.WinError(c.get_last_error())
        try:
            self.require_alive();times=[w.FILETIME() for _ in range(4)]
            if not self.api.GetProcessTimes(self.handle,*(c.byref(t) for t in times)):raise c.WinError(c.get_last_error())
            self.creation_ticks=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
        except BaseException:self.close();raise

    def require_alive(self):
        if self.api.WaitForSingleObject(self.handle,0)!=258:raise ValueError('retained process is not alive')

    def require_terminated(self):
        if self.api.WaitForSingleObject(self.handle,3000)!=0:raise ValueError('owned compute not reaped')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle);self.handle=None

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


def pin_process(expected):
    if not expected or not owned_process(expected,inspect_pid(expected['pid'])):raise ValueError('process identity changed before open')
    process=_NativeProcess(expected['pid'])
    try:
        if not owned_process(expected,inspect_pid(expected['pid'])):raise ValueError('process identity changed during open')
        process.require_alive();return process
    except BaseException:process.close();raise


def thread_ids(process):
    class Entry(c.Structure):
        _fields_=[('dwSize',w.DWORD),('cntUsage',w.DWORD),('th32ThreadID',w.DWORD),
            ('th32OwnerProcessID',w.DWORD),('tpBasePri',w.LONG),('tpDeltaPri',w.LONG),('dwFlags',w.DWORD)]
    api=process.api
    api.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];api.CreateToolhelp32Snapshot.restype=w.HANDLE
    for name in ('Thread32First','Thread32Next'):
        fn=getattr(api,name);fn.argtypes=[w.HANDLE,c.POINTER(Entry)];fn.restype=w.BOOL
    handle=api.CreateToolhelp32Snapshot(0x4,0) # System-wide; MUST filter owner PID.
    if handle==w.HANDLE(-1).value:raise c.WinError(c.get_last_error())
    try:
        value=Entry();value.dwSize=c.sizeof(value);found=set();count=0
        success=api.Thread32First(handle,c.byref(value))
        while success:
            count+=1
            if count>131072:raise ValueError('thread snapshot exceeds bound')
            if value.th32OwnerProcessID==process.pid:found.add(value.th32ThreadID)
            value.dwSize=c.sizeof(value);success=api.Thread32Next(handle,c.byref(value))
        if not found or len(found)>64:raise ValueError('bounded numerical thread set required')
        process.require_alive();return found
    finally:api.CloseHandle(handle)


class ThreadPause:
    def __init__(self,process):
        self.process=process;self.handles={};self.thread_ids=();self.restored=False

    def __enter__(self):
        p=self.process;api=p.api
        try:
            for _ in range(3):
                ids=thread_ids(p)
                for tid in sorted(ids-set(self.handles)):
                    p.require_alive();handle=api.OpenThread(0x2|0x800|0x100000,False,tid)
                    if not handle:raise c.WinError(c.get_last_error())
                    try:
                        if api.GetProcessIdOfThread(handle)!=p.pid:raise ValueError('thread belongs to another process')
                        p.require_alive();prior=api.SuspendThread(handle)
                        if prior==0xffffffff:raise c.WinError(c.get_last_error())
                        self.handles[tid]=handle
                        if prior!=0:raise ValueError('preexisting suspension refused')
                    except BaseException:
                        if tid not in self.handles:api.CloseHandle(handle)
                        raise
                if thread_ids(p)==set(self.handles):
                    self.thread_ids=tuple(sorted(self.handles));return self
            raise ValueError('thread set did not stabilize')
        except BaseException:self.__exit__(None,None,None);raise

    def __exit__(self,*_):
        errors=[];api=self.process.api
        for handle in self.handles.values():
            try:
                state=api.WaitForSingleObject(handle,0)
                if state==258:
                    if api.ResumeThread(handle)==0xffffffff:errors.append('resume failed')
                elif state!=0:errors.append('thread state unknown')
            finally:api.CloseHandle(handle)
        self.handles={};self.restored=not errors
        if errors:raise ValueError('owned thread cleanup failed')


@contextmanager
def pause_compute(lab,request):
    checked_handle(lab);root=lab.review_root;marker=_marker(root)
    if marker['state']!='running' or 'inversion' not in marker['processes']:raise ValueError('owned worker not running')
    worker=marker['processes']['inversion'];until=time.monotonic()+30
    with ExitStack() as stack:
        worker_handle=stack.enter_context(pin_process(worker));candidate=None
        while time.monotonic()<until:
            worker_handle.require_alive();parents=children(worker['pid'])
            candidates=[]
            for parent in parents:
                if 'spawn_main' not in parent['command']:continue
                for child in children(parent['pid']):
                    if 'backend.inversion.compute_child' not in child['command']:continue
                    validate_candidate(root,worker,parent,child,request)
                    candidates.append((parent,child))
            if len(candidates)>1:raise ValueError('multiple compute children; refuse fault injection')
            if candidates:candidate=candidates[0];break
            time.sleep(.1)
        if candidate is None:raise TimeoutError('fresh compute child not found')
        parent,child=candidate
        parent_handle=stack.enter_context(pin_process(parent));process=stack.enter_context(pin_process(child))
        validate_candidate(root,worker,parent,child,request)
        worker_handle.require_alive();parent_handle.require_alive()
        stack.enter_context(ThreadPause(process))
        yield process
