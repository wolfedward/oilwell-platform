"""Owned external fault-injection safety; all children created by this test."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from types import SimpleNamespace as N


def module(test):
    try:return importlib.import_module('tools.testpack.owned_compute_fault_v2')
    except ModuleNotFoundError:test.fail('owned compute fault harness absent')


@contextmanager
def real_tree():
    """All files and native processes here belong to this fresh temporary job."""
    from backend.inversion.worker import OwnedJob
    from tools.review_deploy import BASE_PYTHON
    from tools.testpack import owned_compute_fault_v2 as api
    with TemporaryDirectory(prefix='inversion-fault-topology-') as folder:
        root=Path(folder)
        subprocess.run([str(BASE_PYTHON),'-I','-B','-m','venv','--without-pip',str(root/'venv')],
            check=True,timeout=30,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        for path in ('code/tools','code/backend/inversion','private/inversion-staging/inversion-owned'):
            (root/path).mkdir(parents=True)
        support=Path(__file__).with_name('inversion_fault_process_support.py').read_bytes()
        (root/'code/tools/review_deploy_worker.py').write_bytes(support)
        (root/'code/backend/inversion/compute_child.py').write_bytes(support)
        request={'parameter_id':'fresh-owned-topology-only'}
        (root/'private/inversion-staging/inversion-owned/input.json').write_text(
            json.dumps({'snapshot':{'request':request}}),encoding='utf-8')
        env=dict(SystemRoot=os.environ['SystemRoot'],TEMP=str(root),TMP=str(root),
            PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
        child=None;job=OwnedJob()
        try:
            child=subprocess.Popen([str(root/'venv/Scripts/python.exe'),'-m','tools.review_deploy_worker',
                'inversion','--config',str(root/'private/inversion.toml')],cwd=root/'code',env=env,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW|0x4) # CREATE_SUSPENDED before any redirect child.
            job.attach(N(sentinel=int(child._handle)))
            with api._NativeProcess(child.pid) as native:
                tids=api.thread_ids(native)
                if len(tids)!=1:raise AssertionError('fresh suspended launcher must have one thread')
                handle=native.api.OpenThread(0x2,False,next(iter(tids)))
                if not handle:raise AssertionError('cannot resume fresh owned primary thread')
                try:
                    if native.api.ResumeThread(handle)!=1:raise AssertionError('unexpected initial suspension')
                finally:native.api.CloseHandle(handle)
            (root/'contained').touch()
            until=time.monotonic()+15
            while not (root/'compute.ticks').exists():
                if child.poll() is not None or time.monotonic()>=until:
                    raise AssertionError('fresh venv compute never became ready')
                time.sleep(.02)
            marker={'state':'running','processes':{'inversion':api.inspect_pid(child.pid)}}
            yield root,request,marker,job
        finally:
            try:
                job.stop()
                if job.active()!=0:raise AssertionError('test job still active')
                if child is not None:
                    if child.poll() is None:child.terminate()
                    child.wait(timeout=5)
            finally:job.close()


@contextmanager
def chain_fixture():
    from tools.review_deploy import BASE_PYTHON
    with TemporaryDirectory() as folder:
        root=Path(folder);stage=root/'private/inversion-staging/inversion-owned';stage.mkdir(parents=True)
        request={'parameter_id':'unique-test-parameter'}
        (stage/'input.json').write_text(json.dumps({'snapshot':{'request':request}}),encoding='utf-8')
        venv=str(root/'venv/Scripts/python.exe');base=str(BASE_PYTHON)
        worker=subprocess.list2cmdline([venv,'-m','tools.review_deploy_worker','inversion',
            '--config',str(root/'private/inversion.toml')])
        spawn=subprocess.list2cmdline([base,'-B','-c',
            'from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=101, pipe_handle=812)',
            '--multiprocessing-fork'])
        code=str((root/'code').resolve())
        bootstrap="import sys; from pathlib import Path; sys.path.insert(0,"+repr(code)+"); from backend.inversion.compute_child import compute; compute(Path(sys.argv[1]),Path(sys.argv[2]))"
        compute=subprocess.list2cmdline([venv,'-I','-B','-c',bootstrap,str(stage/'input.json'),str(stage/'result.bin')])
        chain=tuple(dict(pid=100+i,parent_pid=99+i,created=f'owned-{i}',executable=exe,command=cmd)
            for i,(exe,cmd) in enumerate(((venv,worker),(base,worker),(base,spawn),(venv,compute),(base,compute))))
        yield root,stage,request,chain


class OwnedFaultTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows venv topology target')
    def test_real_venv_chain_pauses_compute_not_redirector_and_reaps(self):
        api=module(self)
        with real_tree() as (root,request,marker,job):
            actual=json.loads((root/'compute-ready.json').read_bytes())
            worker=json.loads((root/'worker-ready.json').read_bytes())
            supervisor=json.loads((root/'supervisor-ready.json').read_bytes())
            # Independent child-side PID witnesses establish the actual topology.
            self.assertEqual(worker['parent_pid'],marker['processes']['inversion']['pid'])
            self.assertEqual(supervisor['parent_pid'],worker['pid'])
            self.assertEqual(actual['parent_pid'],supervisor['launcher_pid'])
            self.assertNotEqual(actual['pid'],supervisor['launcher_pid'])
            print('observed owned venv topology',marker['processes']['inversion']['pid'],
                worker['pid'],supervisor['pid'],supervisor['launcher_pid'],actual['pid'],flush=True)
            with patch.object(api,'checked_handle'),patch.object(api,'_marker',return_value=marker):
                with api.pause_compute(N(review_root=root),request) as paused:
                    self.assertEqual(paused.pid,actual['pid'],'must pause interpreter, not launcher')
                    self.assertEqual(Path(api.inspect_pid(paused.pid)['executable']).resolve(),Path(actual['base']).resolve())
                    before=(root/'compute.ticks').stat().st_size
                    supervisor_before=(root/'supervisor.ticks').stat().st_size
                    time.sleep(.2)
                    self.assertEqual((root/'compute.ticks').stat().st_size,before)
                    self.assertGreater((root/'supervisor.ticks').stat().st_size,supervisor_before)
                until=time.monotonic()+3
                while (root/'compute.ticks').stat().st_size==before and time.monotonic()<until:time.sleep(.02)
                self.assertGreater((root/'compute.ticks').stat().st_size,before,'abort must undo only owned suspension')
                with api.pause_compute(N(review_root=root),request) as paused:
                    job.stop();paused.require_terminated()
                    self.assertEqual(job.active(),0)

    def test_chain_and_request_must_match_before_suspension(self):
        api=module(self)
        self.assertTrue(callable(getattr(api,'validate_chain',None)),'explicit bounded redirector validation missing')
        with chain_fixture() as (root,stage,request,chain):
            self.assertEqual(api.validate_chain(root,chain,request),stage)
            with self.assertRaises(ValueError):api.validate_chain(root,chain,{'parameter_id':'other'})
            # Also support the base worker and direct base compute without shims.
            direct=(chain[1],chain[2],chain[4]|{'parent_pid':chain[2]['pid']})
            self.assertEqual(api.validate_chain(root,direct,request),stage)

    def test_similar_commands_wrong_images_ancestry_and_extra_redirects_refused(self):
        api=module(self)
        self.assertTrue(callable(getattr(api,'validate_chain',None)),'bounded role matcher missing')
        with chain_fixture() as (root,stage,request,chain):
            mutations=[(0,{'command':chain[0]['command'].replace('inversion.toml','other.toml')}),
                (1,{'executable':str(root/'other/python.exe')}),
                (2,{'command':chain[2]['command'].replace('parent_pid=101','parent_pid=999')}),
                (2,{'command':chain[2]['command'].replace('pipe_handle=812','pipe_handle=812); print(1')}),
                (2,{'command':chain[2]['command'].replace('-B','-X dev')}),
                (3,{'parent_pid':999}),
                (4,{'executable':chain[3]['executable']}),
                (4,{'command':chain[4]['command'].replace('compute_child','other')}),
                (4,{'pid':chain[2]['pid']})]
            for index,change in mutations:
                changed=list(chain);changed[index]=changed[index]|change
                with self.subTest(index=index,change=change),self.assertRaises(ValueError):
                    api.validate_chain(root,changed,request)
            with self.assertRaises(ValueError):api.validate_chain(root,chain[:-1],request)
            with self.assertRaises(ValueError):api.validate_chain(root,(*chain[:4],chain[3],chain[4]),request)

    def test_multiple_eligible_compute_processes_are_ambiguous(self):
        api=module(self)
        self.assertTrue(callable(getattr(api,'find_chain',None)),'bounded chain discovery missing')
        with chain_fixture() as (root,stage,request,chain):
            inventory={row['parent_pid']:[row] for row in chain[1:]}
            inventory[chain[3]['pid']].append(chain[4]|{'pid':105,'created':'other-owned'})
            with patch.object(api,'children',side_effect=lambda pid:inventory.get(pid,[])):
                with self.assertRaisesRegex(ValueError,'multiple'):api.find_chain(root,chain[0],request)

    def test_refreshed_ancestry_or_identity_change_refuses(self):
        api=module(self)
        with chain_fixture() as (root,stage,request,chain):
            inventory={row['parent_pid']:[row] for row in chain[1:]}
            with patch.object(api,'inspect_pid',return_value=chain[0]),patch.object(api,'children',side_effect=lambda pid:inventory.get(pid,[])):
                self.assertEqual(api.refresh_chain(chain),chain)
                inventory[chain[3]['pid']]=[chain[4]|{'created':'reused'}]
                with self.assertRaises(ValueError):api.refresh_chain(chain)
                inventory[chain[3]['pid']]=[]
                with self.assertRaises(ValueError):api.refresh_chain(chain)

    def test_wrong_identity_refuses_before_native_process_open(self):
        api=module(self)
        expected={'pid':100,'created':'owned','executable':'python','command':'worker'}
        with patch.object(api,'inspect_pid',return_value=expected|{'created':'reused'}),patch.object(api,'_NativeProcess') as native:
            with self.assertRaises(ValueError):api.pin_process(expected)
            native.assert_not_called()

    @unittest.skipUnless(os.name=='nt','Windows process containment target')
    def test_owned_thread_pause_restores_only_its_increment(self):
        api=module(self)
        # This process is freshly created here, never discovered by PID alone.
        child=subprocess.Popen([sys._base_executable,'-I','-c','import time; time.sleep(30)'],
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            expected=api.inspect_pid(child.pid)
            with api.pin_process(expected) as pinned:
                with api.ThreadPause(pinned) as paused:
                    self.assertTrue(paused.thread_ids)
                    self.assertIsNone(child.poll())
                self.assertTrue(paused.restored)
                self.assertIsNone(child.poll())
            child.terminate();self.assertIsNotNone(child.wait(timeout=5))
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)


if __name__=='__main__':unittest.main()
