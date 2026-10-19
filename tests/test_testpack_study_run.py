from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
import unittest
from uuid import uuid4

from tools.testpack.study_run import StudySession,run_study_steps,STEPS,bind_runtime
from tools.testpack.study_inputs import generate_studies
from tools.testpack.contracts import PackConfig
from tools.testpack.cycles import generate_cycles
from tools.testpack.currents import generate_currents
from tools.testpack.history import generate_history


class TestPackStudyRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config=PackConfig()
        cases=generate_studies(config,generate_cycles(config),generate_history(config),generate_currents(config))
        cls.templates={c.case_id:json.loads(c.artifacts[0].content) for c in cases}
        cls.expected={c.case_id:c.expected[0]['expected'] for c in cases}

    def test_each_well_gets_its_own_recovery_id(self):
        template=deepcopy(self.templates['study-energy-schedule'])
        other=deepcopy(template['envelope']['payload']['wells'][0]);other['well_id']='TESTPACK-WELL-002'
        template['envelope']['payload']['wells'].append(other)
        template['dependencies'].append({'path':'wells/1/recovery_record_id','record_key':'recovery_fit:TESTPACK-WELL-002'})
        ids={'recovery_fit:TESTPACK-WELL-001':str(uuid4()),'recovery_fit:TESTPACK-WELL-002':str(uuid4())}
        output=bind_runtime(template,ids,'a'*64,'first')
        self.assertEqual([w['recovery_record_id'] for w in output['payload']['wells']],list(ids.values()))

    def test_source_pins_and_runtime_ids(self):
        template=self.templates['study-condition-prediction'];ids={'condition_training':str(uuid4())}
        first=bind_runtime(template,ids,'a'*64,'first')
        second=bind_runtime(template,ids,'a'*64,'renewed')
        self.assertNotEqual(first['request_id'],second['request_id'])
        self.assertEqual(first,bind_runtime(template,ids,'a'*64,'first'))
        self.assertEqual(first['source']['kind'],'software_fixture')
        self.assertIn('manifest-sha256:'+'a'*64,first['source']['references'])

    def test_run_failure_does_not_become_success_json(self):
        class Failed:
            def execute(self,step):raise RuntimeError('private error must not be copied')
        with TemporaryDirectory() as folder:
            results=run_study_steps(Failed(),Path(folder))
            self.assertEqual(results[0].status,'failed')
            self.assertTrue(all(r.status=='not_run' for r in results[1:]))
            self.assertNotIn('private error', (Path(folder)/'study-checks.json').read_text())

    def test_actual_engine_seven_kinds_and_review_export_revoke_and_retry(self):
        from backend.http_server import create_server
        from backend.review_runtime import initialize_identity
        from tools.testpack.lab_http import LabHttp
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'private').mkdir();identity=root/'identity.db'
            accounts=initialize_identity(identity)
            (root/'private/accounts.json').write_text(json.dumps(accounts))
            server=create_server('127.0.0.1',0,identity,runtime_mode='review')
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            client=object.__new__(LabHttp);client.root=root;client.port=server.server_port;client.profile='review';client.sessions={}
            try:
                session=StudySession(client,root,self.templates,self.expected,'a'*64,'test-session')
                results=run_study_steps(session,root,steps=STEPS[:-1])
                self.assertTrue(all(r.status=='passed' for r in results),[(r.check_id,r.actual) for r in results if r.status!='passed'])
                self.assertEqual(len({r['kind'] for r in session.records.values()}),7)
                self.assertTrue(session.snapshots)
                for exported in session.snapshots.values():
                    self.assertIs(exported['field_validation'],False)
                    self.assertIs(exported['control_eligible'],False)
            finally:
                server.shutdown();worker.join(3);server.server_close()


if __name__=='__main__':unittest.main()
