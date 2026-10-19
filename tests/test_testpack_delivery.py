from dataclasses import replace
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace

from tools.testpack.contracts import CheckResult


class TestPackDelivery(unittest.TestCase):
    def test_browser_failure_does_not_require_a_success_screenshot(self):
        from tools.testpack.delivery import browser_check
        from tools.testpack.storage import write_pack
        from tools.testpack.contracts import PackConfig
        from tests.test_testpack_contracts import example,GENERATOR
        with TemporaryDirectory() as folder:
            root=Path(folder);pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR)
            public=root/'report.json';public.write_text('{}')
            with patch('tools.testpack.delivery.serve_viewer') as serve,patch('tools.review_deploy_paths.private_acl'),patch('tools.testpack.delivery.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=b'{"ok":false,"failed_check":"actual_report_loaded"}')):
                serve.return_value.server_port=12345
                check=browser_check(pack,public,root)
            self.assertEqual(check.status,'failed')
            self.assertEqual(check.actual['failed_check'],'actual_report_loaded')

    def test_report_cli_refuses_unowned_run(self):
        from tools.testpack.__main__ import main
        with TemporaryDirectory() as folder,redirect_stderr(StringIO()):
            root=Path(folder)/'run';root.mkdir()
            self.assertEqual(main(['report','--run',str(root),'--output',str(Path(folder)/'public')]),2)

    def test_incomplete_step_set_cannot_pass_summary(self):
        from tools.testpack.delivery import summarize
        checks=(CheckResult('a','http','passed',{},{}),)
        result=summarize('summary','http',('a','b'),checks)
        self.assertEqual(result.status,'not_run')
        result=summarize('summary','http',('a',),checks)
        self.assertEqual(result.status,'passed')
        self.assertEqual(summarize('summary','http',('a',),(replace(checks[0],status='blocked'),)).status,'blocked')
        with self.assertRaises(ValueError):summarize('summary','http',('a',),checks+checks)

    def test_layer_mismatch_cannot_count_as_http_pass(self):
        from tools.testpack.delivery import summarize
        check=CheckResult('a','offline','passed',{}, {})
        with self.assertRaises(ValueError):summarize('summary','http',('a',),(check,))
