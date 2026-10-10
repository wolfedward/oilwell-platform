"""Offline verifier tests; never discover or execute the repository suite."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import verify_inversion as verifier


class VerificationTests(unittest.TestCase):
    def test_internal_selection_loads_exact_names_once_without_discovery(self):
        names=('tests.test_fixture.Example.test_one',)
        calls=[]
        class Example(unittest.TestCase):
            def id(self): return names[0]
            def runTest(self): pass
        class Loader:
            errors=[]
            def discover(self,*args,**kwargs): raise AssertionError('No discovery for targeted scope')
            def loadTestsFromNames(self,actual): calls.append(actual); return unittest.TestSuite([Example()])
        with patch.object(verifier.unittest,'TestLoader',return_value=Loader()):
            suite,errors,scope=verifier.load_suite(False,names)
        self.assertEqual(calls,[names]); self.assertEqual(errors,0)
        self.assertEqual([case.id() for case in verifier.cases(suite)],list(names))
        self.assertEqual(scope,'explicit-targeted')
        self.assertEqual(verifier.verification_scope(True,names),'explicit-targeted')

    def test_internal_selection_rejects_invalid_missing_or_mismatched_cases(self):
        names=('tests.test_fixture.Example.test_one',)
        for invalid in ((),[],names*2,('os.system',),('tests.test_fixture',),tuple(
                'tests.test_fixture.Example.test_'+str(i) for i in range(33))):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                verifier.verification_scope(False,invalid)
        class Other(unittest.TestCase):
            def id(self): return 'tests.test_fixture.Example.test_other'
        for entries,errors in (([],[]),([Other()],[]),([],['redacted loader failure'])):
            class Loader:
                def loadTestsFromNames(self,actual): return unittest.TestSuite(entries)
            loader=Loader(); loader.errors=errors
            with patch.object(verifier.unittest,'TestLoader',return_value=loader),self.assertRaises(ValueError):
                verifier.load_suite(False,names)

    def test_default_modes_keep_one_original_discovery_scope(self):
        for include,pattern in ((False,'test_inversion_*.py'),(True,'test_*.py')):
            calls=[]
            class Loader:
                errors=[]
                def discover(self,*args,**kwargs): calls.append(kwargs['pattern']); return unittest.TestSuite()
                def loadTestsFromNames(self,names): raise AssertionError('No named load for default scope')
            with patch.object(verifier.unittest,'TestLoader',return_value=Loader()):
                _,errors,scope=verifier.load_suite(include,None)
            self.assertEqual(calls,[pattern]); self.assertEqual(scope,pattern); self.assertEqual(errors,0)

    def test_private_acl_requires_exact_sids_rights_and_inheritance(self):
        rows=[dict(sid=sid,rights=2032127,inheritance=3,propagation=0,access=0,inherited=False)
            for sid in ('S-1-5-21-test','S-1-5-32-544','S-1-5-18')]
        good=dict(protected=True,user='S-1-5-21-test',rules=rows)
        with patch.object(verifier.subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps(good))):
            verifier.protect_directory(Path('unused-owned-test-directory'))
        variants=[]
        for field,value in [('sid','S-1-1-0'),('rights',1179785),('inheritance',0),
                            ('propagation',1),('access',1),('inherited',True)]:
            bad=copy.deepcopy(good); bad['rules'][0][field]=value; variants.append(bad)
        variants.append(dict(good,protected=False))
        variants.append(dict(good,rules=rows+[rows[0]]))
        for bad in variants:
            with self.subTest(bad=bad), patch.object(verifier.subprocess,'run',
                    return_value=SimpleNamespace(stdout=json.dumps(bad))):
                with self.assertRaises(RuntimeError):
                    verifier.protect_directory(Path('unused-owned-test-directory'))

    def test_failed_job_attachment_reaps_exact_owned_child_and_stays_red(self):
        from tests.inversion_verification_security import Snapshot
        events=[]
        class Channel:
            def close(self): events.append('pipe_closed')
        class Process:
            pid=123; exitcode=None; alive=False
            def __init__(self,**kwargs): pass
            def start(self): self.alive=True
            def is_alive(self): return self.alive
            def terminate(self): events.append('terminated'); self.alive=False; self.exitcode=1
            def join(self,timeout): events.append('joined')
            def close(self): events.append('process_closed')
        class Job:
            def attach(self,process): raise OSError('private attach error')
            def active(self): return 0
            def close(self): events.append('job_closed')
        class Provider:
            def snapshot(self): events.append('snapshot'); return Snapshot({'fixture':'stable'})
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root=Path(directory); baseline=root/'baseline.json'; baseline.write_bytes(verifier.encoded({'fixture':'stable'}))
            config=root/'config.json'; config.write_bytes(verifier.encoded(dict(schema='inversion.verification/1',
                security_baseline_path=str(baseline),security_baseline_sha256=verifier.sha256(baseline.read_bytes()).hexdigest())))
            for name,value in [('ROOT',root),('_environment',lambda:(None,Provider())),
                ('_set_opt_ins',lambda _:None),('protect_directory',lambda _:None),
                ('source_inventory',lambda:{'file':'hash'})]:
                stack.enter_context(patch.object(verifier,name,value))
            stack.enter_context(patch.dict('os.environ',{'OILWELL_INVERSION_VERIFIER_CHILD':'0'}))
            stack.enter_context(patch('multiprocessing.get_context',return_value=SimpleNamespace(Pipe=lambda:(Channel(),Channel()))))
            stack.enter_context(patch('backend.inversion.worker.OwnedJob',Job))
            stack.enter_context(patch('backend.inversion.worker._OwnedSpawnProcess',Process))
            stack.enter_context(patch.object(verifier.subprocess,'run',return_value=SimpleNamespace(returncode=0)))
            stack.enter_context(patch.object(verifier.subprocess,'check_output',return_value='revision'))
            stack.enter_context(patch('builtins.print'))
            self.assertEqual(verifier.supervise(SimpleNamespace(config=config,output=root,include_existing_regression=False)),1)
            report=json.loads(next(root.glob('run-*/report.json')).read_bytes())
            self.assertFalse(report['ok']); self.assertFalse(report['cleanup_ok'])
        self.assertIn('terminated',events)
        self.assertIn('process_closed',events)
        self.assertLess(events.index('pipe_closed'),events.index('terminated'))
        self.assertLess(events.index('terminated'),events.index('joined'))
        self.assertEqual(events[-1],'snapshot')

    def test_final_fixture_failure_cannot_qualify_successful_case_ids(self):
        from tests.inversion_verification_security import Snapshot
        class Example(unittest.TestCase):
            def runTest(self): pass
            @classmethod
            def tearDownClass(cls): raise RuntimeError('private final fixture marker')
        class Provider:
            def snapshot(self): return Snapshot({'fixture':'stable'})
        with patch.object(verifier,'source_inventory',return_value={'file':'hash'}):
            report=verifier.run_suite(unittest.TestSuite([Example()]),Provider(),
                Snapshot({'fixture':'stable'}),sink=lambda _:None,source_before={'file':'hash'})
        report['cleanup_ok']=True
        self.assertEqual(report['terminal'][0]['outcome'],'success')
        self.assertEqual(report['categories'],{'error':1})
        self.assertFalse(verifier.qualify(report))

    def test_verifier_rejects_missing_skipped_or_stale_evidence(self):
        good = dict(discovered=['case.a'], started=['case.a'], terminal=[
            dict(id='case.a', outcome='success', duration_seconds=0.01)],
            tests_run=1, loader_errors=0, source_before={'a':'123'},
            source_after={'a':'123'}, security_ok=True, cleanup_ok=True,
            interrupted=False,unittest_ok=True,categories={})
        self.assertTrue(verifier.qualify(good))
        for field, value in [('started', []), ('terminal', []),
                             ('source_after', {'a':'456'}), ('tests_run', 0),
                             ('security_ok', False), ('cleanup_ok', False),
                             ('loader_errors', 1), ('interrupted', True),
                             ('unittest_ok',False),('categories',{'error':1})]:
            with self.subTest(field=field):
                bad=copy.deepcopy(good); bad[field]=value
                self.assertFalse(verifier.qualify(bad))
        for category in ('skip','failure','error','expected_failure','unexpected_success'):
            bad=copy.deepcopy(good); bad['terminal'][0]['outcome']=category
            self.assertFalse(verifier.qualify(bad))
        bad=copy.deepcopy(good); bad['discovered']*=2
        self.assertFalse(verifier.qualify(bad))

    def test_collector_accounts_failed_subtests_and_redacts_secrets(self):
        class Example(unittest.TestCase):
            def runTest(self):
                with self.subTest(secret='private marker'):
                    self.fail('private marker')
        case=Example()
        result=verifier.Results()
        unittest.TestSuite([case]).run(result)
        self.assertEqual(result.started,[case.id()])
        self.assertEqual(result.terminal[0]['outcome'],'failure')
        self.assertGreaterEqual(result.terminal[0]['duration_seconds'],0)
        self.assertNotIn('private marker',repr(result.terminal)+repr(result.failures))

    def test_fake_suite_runs_each_case_once_and_uses_actual_denominator(self):
        from tests.inversion_verification_security import Snapshot
        calls=[]
        class Example(unittest.TestCase):
            def runTest(self): calls.append('run')
        class Provider:
            def snapshot(self): return Snapshot({'fixture':'stable'})
        events=[]
        with patch.object(verifier,'source_inventory',return_value={'file':'hash'}):
            result=verifier.run_suite(unittest.TestSuite([Example()]),Provider(),
                Snapshot({'fixture':'stable'}),sink=events.append,source_before={'file':'hash'})
        self.assertEqual(calls,['run'])
        self.assertEqual(result['tests_run'],1)
        self.assertEqual(len(result['discovered']),1)
        self.assertFalse(verifier.qualify(result)) # External cleanup still required.
        result['cleanup_ok']=True
        self.assertTrue(verifier.qualify(result))

    def test_partial_duplicate_or_loader_error_never_runs_cases(self):
        calls=[]
        class Example(unittest.TestCase):
            def runTest(self): calls.append('run')
        for suite,errors in [(unittest.TestSuite(),0),
                (unittest.TestSuite([Example(),Example()]),0),
                (unittest.TestSuite([Example()]),1)]:
            with self.assertRaises(ValueError):
                verifier.run_suite(suite,None,None,sink=lambda _:None,source_before={'a':'b'},loader_errors=errors)
        self.assertEqual(calls,[])

    def test_collector_refuses_expected_failures_skips_and_fixture_errors(self):
        class Expected(unittest.TestCase):
            @unittest.expectedFailure
            def runTest(self): self.fail('private expected marker')
        class Unexpected(unittest.TestCase):
            @unittest.expectedFailure
            def runTest(self): pass
        class Skipped(unittest.TestCase):
            def runTest(self): self.skipTest('private skip marker')
        class FixtureError(unittest.TestCase):
            @classmethod
            def setUpClass(cls): raise RuntimeError('private fixture marker')
            def runTest(self): self.fail('must never execute')
        for cls,category in [(Expected,'expected_failure'),(Unexpected,'unexpected_success'),
                             (Skipped,'skip'),(FixtureError,'error')]:
            result=verifier.Results(); unittest.TestSuite([cls()]).run(result)
            self.assertEqual(result.outcomes[0][1],category)
            self.assertNotIn('private',repr(result.terminal)+repr(result.errors)+repr(result.skipped)+repr(result.expectedFailures))
        self.assertEqual(result.testsRun,0)
        self.assertTrue(result.shouldStop)

    def test_security_observer_stops_before_next_fixture_on_method_drift(self):
        from tests.inversion_verification_security import Snapshot, execute_cases
        class Provider:
            value=0
            def snapshot(self): return Snapshot({'test:acl':self.value})
        provider=Provider(); calls=[]; events=[]
        class Example(unittest.TestCase):
            def test_first(self): provider.value=1
            def test_next(self): calls.append('wrong')
        result=verifier.Results()
        from tests.inversion_verification_security import Evidence
        outcome=execute_cases([Example('test_first'),Example('test_next')],provider,
            expected_final=Snapshot({'test:acl':0}),approved_setup_changes={},
            classifier=lambda _: 'test',evidence=Evidence(sink=events.append),result=result)
        self.assertFalse(outcome.security_ok)
        self.assertEqual(calls,[])
        self.assertEqual(result.testsRun,1)
        self.assertIn('body_drift',[event['phase'] for event in events])


if __name__ == '__main__':
    unittest.main()
