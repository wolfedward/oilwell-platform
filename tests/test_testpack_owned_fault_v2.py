"""Owned external fault-injection safety; all children created by this test."""
import importlib
import os
from pathlib import Path
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


def module(test):
    try:return importlib.import_module('tools.testpack.owned_compute_fault_v2')
    except ModuleNotFoundError:test.fail('owned compute fault harness absent')


class OwnedFaultTests(unittest.TestCase):
    def test_chain_and_request_must_match_before_suspension(self):
        api=module(self)
        with TemporaryDirectory() as folder:
            root=Path(folder);stage=root/'private/inversion-staging/inversion-owned';stage.mkdir(parents=True)
            input_path=stage/'input.json';output_path=stage/'result.bin'
            request={'parameter_id':'unique-test-parameter'}
            from tools.testpack.storage import canonical_bytes
            input_path.write_bytes(canonical_bytes({'snapshot':{'request':request}}))
            code=str((root/'code').resolve())
            bootstrap="import sys; from pathlib import Path; sys.path.insert(0,"+repr(code)+"); from backend.inversion.compute_child import compute; compute(Path(sys.argv[1]),Path(sys.argv[2]))"
            command=subprocess.list2cmdline([str(root/'venv/Scripts/python.exe'),'-I','-B','-c',bootstrap,str(input_path),str(output_path)])
            worker={'pid':100,'created':'owned','executable':'python','command':'worker'}
            supervisor={'pid':101,'parent_pid':100,'command':'spawn_main','created':'owned2','executable':'python'}
            child={'pid':102,'parent_pid':101,'command':command,'created':'owned3','executable':'python'}
            self.assertEqual(api.validate_candidate(root,worker,supervisor,child,request),stage)
            for mutation in (dict(parent_pid=999),dict(command=command.replace('compute_child','unrelated'))):
                with self.assertRaises(ValueError):api.validate_candidate(root,worker,supervisor,child|mutation,request)
            with self.assertRaises(ValueError):api.validate_candidate(root,worker,supervisor,child,{'parameter_id':'other'})
            with self.assertRaises(ValueError):api.validate_candidate(root,worker,supervisor|{'parent_pid':999},child,request)

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
        child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)'],
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
