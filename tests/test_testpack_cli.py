from contextlib import redirect_stdout,redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from tools.testpack.__main__ import main
from tools.testpack.storage import load_pack


class TestPackCLI(unittest.TestCase):
    def test_smoke_cli_repeatable_and_no_services(self):
        config=Path(__file__).resolve().parents[1]/'config/testpack/smoke.json'
        with TemporaryDirectory(prefix='测试包 命令 ') as temp:
            a,b=Path(temp)/'one',Path(temp)/'two'
            with patch('socket.socket',side_effect=AssertionError('offline generation must not open sockets')):
                for target in (a,b):
                    with redirect_stdout(StringIO()):
                        self.assertEqual(main(['generate','--config',str(config),'--output',str(target)]),0)
            first,second=load_pack(a),load_pack(b)
            self.assertEqual(first.manifest,second.manifest)
            self.assertEqual(sum(c['facts'].get('data_kind')=='cycle' for c in first.manifest['cases']),8)
            self.assertEqual(sum(c['facts'].get('data_kind')=='current' for c in first.manifest['cases']),4)
            report=Path(temp)/'checks.json'
            with redirect_stdout(StringIO()):
                self.assertEqual(main(['validate','--pack',str(a),'--report',str(report)]),0)
            before=report.read_bytes()
            with redirect_stderr(StringIO()):
                self.assertEqual(main(['validate','--pack',str(a),'--report',str(report)]),2)
            self.assertEqual(report.read_bytes(),before)


if __name__=='__main__':
    unittest.main()
