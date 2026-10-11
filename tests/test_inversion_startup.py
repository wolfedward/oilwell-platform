import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.auth import AuthService
from backend.database import Database


class StartupTests(unittest.TestCase):
    def test_platform_import_and_disabled_runtime_need_no_postgres_driver(self):
        import subprocess,sys
        code="""
import builtins
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name=='psycopg' or name.startswith('psycopg.'): raise ModuleNotFoundError('optional driver absent')
    return original(name,*args,**kwargs)
builtins.__import__=guarded
from backend.http_server import create_server
from backend.inversion.runtime import build_inversion_runtime
assert build_inversion_runtime(None,auth=None).status=='disabled'
"""
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_launch_command_propagates_only_explicit_config(self):
        import server
        path=Path('private.toml').resolve()
        self.assertIn('--inversion-config='+str(path),server._launch_command('127.0.0.1',0,'token',inversion_config_path=path))
        self.assertFalse(any(s.startswith('--inversion-config') for s in server._launch_command('127.0.0.1',0,'token')))

    def runtime(self):
        try: return importlib.import_module('backend.inversion.runtime')
        except ModuleNotFoundError: self.fail('optional runtime required')

    def test_disabled_runtime_never_falls_back(self):
        runtime = self.runtime().build_inversion_runtime(None, auth=AuthService(Database(Path('absent.db'))))
        self.assertEqual(runtime.status, 'disabled')
        self.assertIsNone(runtime.queries)

    def test_startup_does_not_ddl_or_start_worker(self):
        module = self.runtime()
        with patch('backend.inversion.migrate.migrate', side_effect=AssertionError('startup DDL')), patch('backend.inversion.worker.run_once', side_effect=AssertionError('automatic worker')):
            runtime = module.build_inversion_runtime(None, auth=AuthService(Database(Path('absent.db'))))
            self.assertEqual(runtime.status, 'disabled')

    def test_expired_owner_is_recovered_before_claim(self):
        from backend.inversion.runtime import worker_tick
        events = []
        class Runs:
            def expired_owner(self): events.append('discover'); return 'expired-owner'
            def recover(self, owner): events.append(('recover', owner))
        with patch('backend.inversion.worker.run_once', side_effect=lambda runtime: events.append('claim') or False):
            worker_tick(object(), Runs())
        self.assertEqual(events, ['discover', ('recover', 'expired-owner'), 'claim'])
