"""Public status is not approval, and never carries private workflow material."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import uuid4

from tools.testpack.inversion_http_v2 import InversionOutcome
from tools.testpack import report


def positive():
    rid,run,eid=(str(uuid4()) for _ in range(3))
    facts={'run':{'state':'completed','run_id':run,'result_id':rid},
        'result':{'id':rid,'content_sha256':'a'*64,'execution':'completed','source':'synthetic',
            'current_applicability':{'state':'applicable'},'field_validation':{'certified':False,'independent_comparison':False},
            'body':{'prediction_status':'applicable','coverage_fraction':'977/1024','source_proof':{'status':'verified'}}},
        'review':{'state':'approved'},'export':{'id':eid,'result_sha256':'a'*64,'intent':'quantitative'},
        'export_file_sha256':'b'*64,'native_prediction_count':1025,'native_proof_count':1025,
        'comparison':{'comparison':{'scope':'original_valid_support','coverage':{'joint_fraction':{'value':'977/1024'}},'primary':{'load_rmse':{'value':'0.08768016126631259'}}}},
        'proof_summary':{'verified':True,'max_base_speed_rad_s':'1/100000','max_load_error_N':'1000',
            'physical_N_error_Nm':'100','physical_J_error_m':'1/10000','numerical_N_error_Nm':'1/100',
            'numerical_J_error_m':'1/10000000','numerical_s_error_m':'1/10000000'},
        'export_bytes':12345,'enqueue_to_terminal_seconds':24.1}
    return InversionOutcome('cycle-base','passed','completed','completed','export','','passed',facts)


class InversionReportV2Tests(unittest.TestCase):
    def project(self,value):
        function=getattr(report,'project_inversion_outcome',None)
        self.assertTrue(callable(function),'separate inversion public projection absent')
        return function(value)

    def test_expected_refusal_separate_from_approved(self):
        value=InversionOutcome('guard-cancel','passed','refused','refused','worker','user_cancelled','refused',{})
        public=self.project(value)
        self.assertEqual(public['assertion_status'],'passed');self.assertEqual(public['approved_execution_status'],'refused')
        self.assertEqual(public['reason_code'],'user_cancelled');self.assertFalse(public['field_accuracy_certified'])

    def test_no_secret_paths_tokens_in_public_report(self):
        value=InversionOutcome('guard-wrong-pin','passed','refused','refused','run_create','inversion.parameter_pin_changed','refused',
            {'password':'SECRET','dsn':'postgresql://secret','path':'C:/Users/private','token':'PRIVATE','arbitrary':{'authorization':'BEARER'}})
        raw=json.dumps(self.project(value))
        for secret in ('SECRET','postgresql','C:/Users','PRIVATE','BEARER','password','dsn'):self.assertNotIn(secret,raw)

    def test_positive_requires_completed_reviewed_exported_budget_and_coverage(self):
        from backend.inversion.queries import wire
        baseline=positive();shown=self.project(baseline)
        self.assertEqual(shown['metrics']['load_rmse_N'],'0.08768016126631259')
        for change in ('queued','review','proof','coverage','rmse','export'):
            facts=wire(baseline.facts)
            if change=='queued':facts['run']['state']='queued'
            if change=='review':facts['review']['state']='unreviewed'
            if change=='proof':facts['proof_summary']['numerical_J_error_m']='1/100'
            if change=='coverage':facts['result']['body']['coverage_fraction']='1/2'
            if change=='rmse':facts['comparison']['comparison']['primary']['load_rmse']['value']=None
            if change=='export':facts['export']['result_sha256']='c'*64
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.project(InversionOutcome('cycle-base','passed','completed','completed','export','','passed',facts))

    def test_v1_report_still_readable(self):
        from tools.testpack.storage import write_pack
        from tools.testpack.contracts import PackConfig
        from tests.test_testpack_contracts import example,GENERATOR
        with TemporaryDirectory() as folder:
            root=Path(folder);pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR)
            run=root/'run';run.mkdir();path=report.write_report(pack,(),run,run_metadata={})
            record=json.loads(path.read_bytes());self.assertEqual(report.validate_public(record['public'])['schema'],'owp.testpack.public-report/1')
            detail=getattr(__import__('tools.testpack.viewer_details',fromlist=['inversion_detail']),'inversion_detail',None)
            self.assertTrue(callable(detail),'versioned read-only inversion detail absent')
            self.assertEqual(detail(pack,record['public'],'cycle-base')['approved_execution_status'],'not_run')

    def test_v2_report_round_trip_and_exact_case_detail(self):
        from tools.testpack.storage import write_pack
        from tools.testpack.contracts import PackConfig
        from tests.test_testpack_contracts import example,GENERATOR
        with TemporaryDirectory() as folder:
            root=Path(folder);pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR)
            run=root/'run';run.mkdir()
            try:path=report.write_report(pack,(),run,run_metadata={},inversion_outcomes=(positive(),))
            except TypeError:self.fail('v2 report persistence absent')
            output=report.export_public_report(run,root/'public');public=json.loads(output.read_bytes())
            self.assertEqual(public['schema'],'owp.testpack.public-report/2')
            self.assertEqual(public['status'],'incomplete','inversion must not complete other project tasks')
            from tools.testpack.viewer_details import inversion_detail
            self.assertEqual(inversion_detail(pack,public,'cycle-base')['approved_execution_status'],'passed')
            other=deepcopy(public);other['pack_id']='tp-'+'f'*24
            with self.assertRaises(ValueError):inversion_detail(pack,other,'cycle-base')

    def test_failed_inversion_cannot_hide_behind_other_passed_checks(self):
        from tools.testpack.storage import write_pack
        from tools.testpack.contracts import PackConfig,CheckResult
        from tests.test_testpack_contracts import example,GENERATOR
        with TemporaryDirectory() as folder:
            root=Path(folder);pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR);run=root/'run';run.mkdir()
            checks=[CheckResult(k,layer,'passed',{}, {}) for k,layer in report.REQUIRED.items()]
            failed=InversionOutcome('cycle-base','failed','completed','failed','worker','deadline_expired','failed',{})
            path=report.write_report(pack,checks,run,run_metadata={},inversion_outcomes=(failed,))
            self.assertEqual(json.loads(path.read_bytes())['public']['status'],'failed')

    def test_owned_readonly_viewer_delivers_exact_case_execution_summary(self):
        from tools.testpack.storage import write_pack
        from tools.testpack.contracts import PackConfig
        from tools.testpack.viewer import serve_viewer
        from tests.test_testpack_contracts import example,GENERATOR
        from urllib.request import urlopen
        with TemporaryDirectory() as folder:
            root=Path(folder);pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR);run=root/'run';run.mkdir()
            outcome=InversionOutcome('case-1','passed','completed','completed','export','','passed',positive().facts)
            report.write_report(pack,(),run,run_metadata={},inversion_outcomes=(outcome,));public=report.export_public_report(run,root/'public')
            server=serve_viewer(pack,public)
            try:
                with urlopen(f'http://127.0.0.1:{server.server_port}/data/cases/case-1.json',timeout=5) as response:data=json.load(response)
                self.assertEqual(data.get('inversion_summary',{}).get('approved_execution_status'),'passed')
            finally:server.shutdown();server.server_close();server.viewer_thread.join(5)


if __name__=='__main__':unittest.main()
