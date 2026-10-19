from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from uuid import uuid4

from tools.testpack.report import write_report,export_public_report,overall_status,validate_public
from tools.testpack.contracts import CheckResult,PackConfig
from tools.testpack.storage import write_pack
from tests.test_testpack_contracts import example,GENERATOR


class TestPackReport(unittest.TestCase):
    def test_not_run_is_not_passed_and_failed_expected_reject(self):
        base=CheckResult('reject','offline','failed',{'expected':'digest_mismatch'},{'observed':'valid'})
        self.assertEqual(overall_status((base,)),'failed')
        self.assertNotEqual(overall_status(()),'passed')
        self.assertEqual(overall_status((replace(base,status='not_run'),)),'incomplete')
        self.assertEqual(overall_status((replace(base,status='blocked'),)),'blocked')

    def test_public_export_has_no_secret_or_private_paths(self):
        sentinel='PRIVATE_PASSWORD_7ef815'
        with TemporaryDirectory() as folder:
            root=Path(folder);pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR)
            run=root/'run';run.mkdir()
            check=CheckResult('safe-check','offline','passed',{'private':sentinel},
                {'password':sentinel,'filename':'C:/Users/private/secret.pem','record_id':sentinel,'baseline_messages':16,'field_validation':False})
            path=write_report(pack,(check,),run,run_metadata={'run_id':str(uuid4())})
            self.assertTrue(path.is_file())
            public=export_public_report(run,root/'public')
            raw=public.read_text(encoding='utf-8')
            for secret in (sentinel,'C:/Users','secret.pem','password'):self.assertNotIn(secret,raw)
            body=json.loads(raw);validate_public(body)
            self.assertNotEqual(body['status'],'passed')
            with self.assertRaises(ValueError):validate_public(body|{'private':sentinel})
            with self.assertRaises(ValueError):write_report(pack,(),run,run_metadata={'password':sentinel})

    def test_public_cannot_disagree_with_private_check(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR)
            run=root/'run';run.mkdir()
            path=write_report(pack,(CheckResult('safe-check','offline','failed',{},{}),),run,run_metadata={})
            body=json.loads(path.read_bytes());public=body['public']
            public['checks'][0]['status']='passed';public['status']='incomplete'
            public['layers']['offline']={'passed':1}
            path.write_text(json.dumps(body),encoding='utf-8')
            with self.assertRaises(ValueError):export_public_report(run,root/'public')

    def test_nested_projection_survives_canonical_key_reordering(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR)
            run=root/'run';run.mkdir()
            actual={'z':{'id':str(uuid4()),'input_sha256':'a'*64,'frame_count':12},
                    'a':{'id':str(uuid4()),'input_sha256':'b'*64,'frame_count':24}}
            write_report(pack,(CheckResult('nested','http','passed',{},actual),),run,run_metadata={})
            public=export_public_report(run,root/'public')
            self.assertTrue(public.is_file())


if __name__=='__main__':unittest.main()
