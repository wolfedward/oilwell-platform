from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class PreviewIsolationTests(unittest.TestCase):
    def test_missing_installation_and_non_loopback_fail_before_binding(self):
        from backend.preview.server import create_preview_server
        with tempfile.TemporaryDirectory() as temp, patch('socket.socket') as sock:
            with self.assertRaises(ValueError): create_preview_server(Path(temp),host='0.0.0.0',port=0)
            with self.assertRaises(FileNotFoundError): create_preview_server(Path(temp),port=0)
            sock.assert_not_called()

    def test_server_import_does_not_load_runtime_control_or_production_bootstrap(self):
        import subprocess, json
        result=subprocess.run(['python','-B','-c',"import backend.preview.server,sys,json; print(json.dumps([x for x in sys.modules if x in ('backend.simulator','backend.http_server','backend.acquisition_worker')]))"],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),[])
