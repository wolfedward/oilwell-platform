"""Management admission is atomic across nested roles and responsive to stop."""
import importlib
import threading
import time
import unittest
from unittest.mock import patch
from backend.waveforms.errors import WaveformError


class AdmissionTests(unittest.TestCase):
    def manager(self,n,**kw):
        try: module=importlib.import_module('backend.waveforms.runtime')
        except ModuleNotFoundError: self.fail('Management admission is required')
        return module.ManagementConnections({},n,**kw)

    def test_impossible_reservation_leaves_single_capacity_usable(self):
        pool=self.manager(1)
        with self.assertRaises(WaveformError):
            with pool.reserve(2): pass
        with pool.reserve(1): self.assertEqual(pool.available,0)
        self.assertEqual(pool.available,1)

    def test_concurrent_compound_is_atomic_and_timeout_releases(self):
        pool=self.manager(2,admission_timeout=.1); errors=[]
        def wait():
            try:
                with pool.reserve(2): self.fail('cannot reserve partial capacity')
            except WaveformError as e: errors.append(e.code)
        with pool.reserve(2):
            t=threading.Thread(target=wait); t.start(); t.join(1); self.assertFalse(t.is_alive())
        self.assertEqual(errors,['waveform.busy']); self.assertEqual(pool.available,2)

    def test_waiting_stop_does_not_resume_after_release(self):
        pool=self.manager(1); errors=[]; entered=threading.Event()
        def wait():
            entered.set()
            try:
                with pool.reserve(1): errors.append('incorrectly admitted')
            except WaveformError as e: errors.append(e.code)
        with pool.reserve(1):
            t=threading.Thread(target=wait); t.start(); entered.wait(1); pool.stop(); t.join(1)
        self.assertEqual(errors,['waveform.stopping']); self.assertEqual(pool.available,1)

    def test_nested_borrow_and_connect_exceptions_return_all_capacity(self):
        pool=self.manager(2); pool.settings={'reader':{},'operator':{}}
        class Owner:
            def __init__(self): self.closed=False; self.closes=0
            def __enter__(self): return self
            def __exit__(self,*args): self.close()
            def close(self): self.closes+=1; self.closed=True
        owner=Owner()
        with patch('backend.waveforms.runtime.psycopg.connect',side_effect=[owner,RuntimeError('second connect')]):
            with self.assertRaises(RuntimeError),pool.reserve(2):
                with pool.factory('operator')():
                    with pool.factory('reader')(): pass
        self.assertEqual(pool.available,2); self.assertEqual(len(pool.active),0)
        self.assertEqual(owner.closes,1)

    def test_late_connect_after_stop_never_executes_and_returns_capacity(self):
        pool=self.manager(1); pool.settings={'reader':{}}
        class Owner:
            closed=False
            def close(self): self.closed=True
        owner=Owner()
        def connect(**kw): pool.stop(); return owner
        with patch('backend.waveforms.runtime.psycopg.connect',side_effect=connect):
            with self.assertRaises(WaveformError):
                with pool.factory('reader')(): self.fail('Work began after stop')
        self.assertTrue(owner.closed); self.assertEqual(pool.available,1)

class ForwardingTests(unittest.TestCase):
    def test_disabled_platform_import_does_not_require_postgres_driver(self):
        import subprocess,sys
        script="""
import builtins
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name=='psycopg' or name.startswith('psycopg.'): raise ModuleNotFoundError('optional driver absent')
    return original(name,*args,**kwargs)
builtins.__import__=guarded
from backend.http_server import create_server
from backend.waveforms.runtime import build_waveform_runtime
assert build_waveform_runtime(None).status=='disabled'
"""
        result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_bound_default_port_origin_is_normalized(self):
        import tempfile
        from pathlib import Path
        from backend.http_server import create_server,PlatformHTTPServer
        from backend.seed import bootstrap_local_auth
        original=PlatformHTTPServer.server_bind
        def bind(server):
            original(server)
            server.server_port=80  # Preserve an owned ephemeral socket; test advertised origin only.
        with tempfile.TemporaryDirectory() as directory,patch.object(PlatformHTTPServer,'server_bind',bind),patch('backend.http_server.seed_database',bootstrap_local_auth):
            server=create_server('127.0.0.1',0,Path(directory)/'app.db')
            try: self.assertEqual(server.waveform_runtime.allowed_origins,('http://127.0.0.1','http://localhost'))
            finally: server.server_close()

    def test_explicit_waveform_config_is_forwarded_independently(self):
        import inspect
        import server
        self.assertIn('waveform_config_path',inspect.signature(server._launch_command).parameters,'Hidden child needs explicit independent waveform config')
        command=server._launch_command('127.0.0.1',18765,'test',waveform_config_path='waveforms.toml')
        self.assertTrue(any(s.startswith('--waveform-config=') for s in command))
        self.assertFalse(any(s.startswith('--center-config=') for s in command))

    def test_disabled_and_invalid_config_never_silently_enable(self):
        from backend.waveforms.runtime import build_waveform_runtime
        with patch('psycopg.connect',side_effect=AssertionError('unexpected connection')):
            self.assertEqual(build_waveform_runtime(None).status,'disabled')
            self.assertEqual(build_waveform_runtime('does-not-exist-waveform.toml').status,'unavailable')

    def test_waveform_cli_environment_and_explicit_override(self):
        import os
        from pathlib import Path
        import server
        with patch.dict(os.environ,{'PLATFORM_WAVEFORM_CONFIG':'env-wave.toml'}),patch('sys.argv',['server.py']):
            self.assertEqual(server.parse_arguments().waveform_config,Path('env-wave.toml'))
        with patch.dict(os.environ,{'PLATFORM_WAVEFORM_CONFIG':'env-wave.toml'}),patch('sys.argv',['server.py','--waveform-config','explicit-wave.toml']):
            self.assertEqual(server.parse_arguments().waveform_config,Path('explicit-wave.toml'))

class LifecycleTests(unittest.TestCase):
    def test_runtime_shutdown_waits_for_stream_owner_finally(self):
        from backend.waveforms.runtime import WaveformRuntime,ManagementConnections
        rt=WaveformRuntime('ready',connections=ManagementConnections({},1)); closed=threading.Event(); finished=threading.Event()
        class Context:
            def close(self): closed.set()
        class Socket:
            def shutdown(self,*args): pass
        item=(Context(),Socket()); rt.streams.add(item)
        def close(): rt.close(); finished.set()
        thread=threading.Thread(target=close); thread.start()
        try:
            self.assertTrue(closed.wait(1)); self.assertFalse(finished.wait(.05),'Shutdown must await stream cleanup')
        finally:
            with rt.lock:
                rt.streams.discard(item)
                if hasattr(rt.lock,'notify_all'): rt.lock.notify_all()
            thread.join(2)
        self.assertTrue(finished.is_set())

    def test_waveform_failure_does_not_skip_legacy_worker_cleanup(self):
        from backend.http_server import PlatformHTTPServer
        from http.server import ThreadingHTTPServer
        server=PlatformHTTPServer.__new__(PlatformHTTPServer); stopped=[]
        class Waveform:
            def close(self): raise RuntimeError('waveform incomplete')
        class Worker:
            def __init__(self,name): self.name=name
            def stop(self): stopped.append(self.name)
        server.waveform_runtime=Waveform()
        for name in ('simulator','workflow_worker','acquisition_worker'): setattr(server,name,Worker(name))
        with patch.object(ThreadingHTTPServer,'server_close'):
            with self.assertRaises(RuntimeError): server.server_close()
        self.assertEqual(stopped,['simulator','workflow_worker','acquisition_worker'])
