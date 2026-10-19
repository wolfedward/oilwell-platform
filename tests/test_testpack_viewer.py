import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import os
import subprocess

from tools.testpack.viewer import serve_viewer
from tools.testpack.contracts import PackConfig
from tools.testpack.storage import write_pack
from tests.test_testpack_contracts import example,GENERATOR


class TestPackViewer(unittest.TestCase):
    def test_compare_labels_toggles_hover_and_gaps(self):
        root=Path(__file__).resolve().parents[1]
        source=(root/'tests/fixtures/testpack_viewer.html').read_text(encoding='utf-8')
        source=source.replace('CHART_MODULE',json.dumps((root/'frontend/inversion/charts.js').as_uri()))
        source=source.replace('VIEWER_MODULE',json.dumps((root/'frontend/testpack/viewer.js').as_uri()))
        runtime=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
        with TemporaryDirectory() as folder:
            page=Path(folder)/'check.html';page.write_text(source,encoding='utf-8')
            result=subprocess.run([str(runtime/'bin/node.exe'),str(root/'tests/fixtures/inversion_fixture_runner.cjs'),page.as_uri(),'1120'],
                capture_output=True,text=True,encoding='utf-8',timeout=60,env=dict(os.environ,NODE_PATH=str(runtime/'node_modules')))
            self.assertEqual(result.returncode,0,result.stderr[-1500:])
            data=json.loads(result.stdout);self.assertTrue(data['ok'],data)

    def test_read_only_viewer_rejects_path_traversal(self):
        with TemporaryDirectory() as folder:
            pack=write_pack(Path(folder)/'pack',PackConfig(),(example(),),GENERATOR)
            viewer=serve_viewer(pack,None)
            try:
                def request(method,path,host=None):
                    connection=http.client.HTTPConnection('127.0.0.1',viewer.server_port,timeout=3)
                    connection.request(method,path,headers={} if host is None else {'Host':host})
                    response=connection.getresponse();raw=response.read();connection.close();return response.status,raw
                self.assertEqual(request('GET','/')[0],200)
                self.assertEqual(request('HEAD','/')[1],b'')
                self.assertEqual(request('POST','/api/commands')[0],405)
                for path in ('/../private/accounts.json','/%2e%2e/private/accounts.json','/pack/../manifest.json','/api/auth/login','/private','/missing'):
                    self.assertIn(request('GET',path)[0],(400,404))
                self.assertEqual(request('GET','/',host='attacker.example')[0],403)
                self.assertEqual(json.loads(request('GET','/data/manifest.json')[1])['pack_id'],pack.manifest['pack_id'])
            finally:viewer.shutdown();viewer.server_close()


if __name__=='__main__':unittest.main()
