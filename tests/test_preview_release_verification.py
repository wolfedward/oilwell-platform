import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

class ReleaseVerificationTests(unittest.TestCase):
    def test_browser_failure_report_returns_nonzero(self):
        from tools.verify_page_preview import main
        with tempfile.TemporaryDirectory() as tmp:
            result={'pages':[],'failures':[{'id':'system-health','error':'timeout'}],'page_errors':[],'outside_preview_requests':[],'overflow':[]}
            with patch('sys.argv',['verify','--target',tmp,'--output',str(Path(tmp)/'result.json')]),patch('tools.verify_page_preview.verify_code',return_value={'revision':'fixed'}),patch('tools.verify_page_preview.verify_manifest',return_value={'captured_at':'time','tables':{}}),patch('tools.verify_page_preview.status',return_value={'status':'ready','port':8772}),patch('tools.verify_page_preview.browser_check',return_value=result),patch('builtins.print'):
                with self.assertRaises(SystemExit) as caught:main()
                self.assertEqual(caught.exception.code,1)
    def test_browser_disconnect_does_not_attempt_another_write(self):
        from backend.preview.server import PreviewHandler
        handler=object.__new__(PreviewHandler)
        handler.headers={'Host':'127.0.0.1:8772'};handler.path='/api/preview/health';handler.command='GET'
        handler.server=SimpleNamespace(server_port=8772,repository=SimpleNamespace(meta={}))
        handler._json=Mock(side_effect=ConnectionAbortedError('browser navigated away'))
        handler._handle()
        self.assertEqual(handler._json.call_count,1)
