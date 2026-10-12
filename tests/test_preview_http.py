"""Real loopback HTTP checks of authentication, limits and rejection boundaries."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from tests.preview_support import make_source


class PreviewHTTPTests(unittest.TestCase):
    def setUp(self):
        from backend.preview.auth import initialize_accounts
        from backend.preview.snapshot import capture_snapshot, build_business_snapshot
        from backend.preview.server import create_preview_server
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); source=make_source(self.root)
        raw=capture_snapshot(source,self.root/'private')
        build_business_snapshot(Path(raw['path']),self.root/'business.db')
        self.accounts=initialize_accounts(self.root/'private')
        self.server=create_preview_server(self.root,port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.close_server)
        self.origin='http://127.0.0.1:'+str(self.server.server_port)

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)

    def request(self,path,method='GET',body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            data=json.dumps(body).encode() if body is not None else None
            conn.request(method,path,body=data,headers={'Content-Type':'application/json',**(headers or {})})
            response=conn.getresponse(); return response.status,dict(response.getheaders()),response.read()
        finally: conn.close()

    def login(self):
        account=self.accounts[0]
        status,headers,body=self.request('/api/preview/session','POST',{'username':account['username'],'password':account['password']},{'Origin':self.origin})
        self.assertEqual(status,200,body)
        self.assertIn('HttpOnly',headers['Set-Cookie']); self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        session=json.loads(body)['data']
        return {'Cookie':headers['Set-Cookie'].split(';')[0],'Origin':self.origin,'X-Preview-CSRF':session['csrf_token']}

    def test_session_is_independent_and_logout_revokes(self):
        self.assertEqual(self.request('/api/preview/catalog')[0],401)
        self.assertEqual(self.request('/api/preview/catalog',headers={'Cookie':'session=old-demo-token'})[0],401)
        headers=self.login()
        status,_,body=self.request('/api/preview/catalog',headers=headers)
        self.assertEqual(status,200); self.assertEqual(len(json.loads(body)['data']),55)
        self.assertEqual(self.request('/api/preview/session','DELETE',headers=headers)[0],200)
        self.assertEqual(self.request('/api/preview/catalog',headers=headers)[0],401)

    def test_control_unknown_api_and_private_files_rejected(self):
        headers=self.login()
        for path in ['/api/commands/x/retry','/api/preview/legacy/api/commands/x/retry','/api/modbus/write']:
            self.assertEqual(self.request(path,'POST',{},headers)[0],403,path)
        for path in ['/private/source.db','/business.db','/private/accounts.json','/frontend/../private/accounts.json',
                     '/assets/open-interface/%2e%2e/%2e%2e/private/accounts.json','/frontend/%252e%252e/private/accounts.json']:
            self.assertIn(self.request(path,headers=headers)[0],(400,404),path)
        self.assertEqual(self.request('/api/preview/not-a-route',headers=headers)[0],404)

    def test_origin_csrf_and_body_limits(self):
        headers=self.login()
        self.assertEqual(self.request('/api/preview/session','DELETE',headers={**headers,'Origin':'http://evil.test'})[0],403)
        self.assertEqual(self.request('/api/preview/session','DELETE',headers={**headers,'X-Preview-CSRF':'wrong'})[0],403)
        self.assertEqual(self.request('/api/preview/session','POST',{'padding':'x'*(1024*1024)}, {'Origin':self.origin})[0],413)

    def test_health_never_claims_production_control(self):
        status,_,body=self.request('/api/preview/health')
        self.assertEqual(status,200)
        data=json.loads(body)['data']
        self.assertEqual(data['mode'],'preview'); self.assertIs(data['control_enabled'],False)

    def test_expired_session_rejected(self):
        headers=self.login()
        token=headers['Cookie'].split('=',1)[1]
        self.server.auth.sessions[token]['expires']=0
        self.assertEqual(self.request('/api/preview/catalog',headers=headers)[0],401)

    def test_role_switch_invalidates_old_write_token_and_rehearsal_persists(self):
        from backend.preview.snapshot import file_hash
        headers=self.login(); before=file_hash(self.root/'business.db')
        body={'request_id':'http-request-001','object_id':'S1','action':'submit','expected_version':0,'values':{'on_minutes':40}}
        status,_,payload=self.request('/api/preview/rehearsals','POST',body,headers)
        self.assertEqual(status,200,payload)
        status,_,payload=self.request('/api/preview/actor','POST',{'role_code':'reviewer'},headers)
        self.assertEqual(status,200,payload)
        switched=json.loads(payload)['data']
        self.assertEqual(switched['actor']['role_code'],'reviewer')
        self.assertNotEqual(switched['actor']['id'],'preview-user-1')
        self.assertEqual(self.request('/api/preview/rehearsals','POST',body,headers)[0],403)
        headers['X-Preview-CSRF']=switched['csrf_token']
        status,_,payload=self.request('/api/preview/rehearsals','POST',{'request_id':'http-request-002','object_id':'S1','action':'approve','expected_version':1,'values':{}},headers)
        self.assertEqual(status,200,payload)
        self.assertFalse(json.loads(payload)['data']['device_sent'])
        self.assertEqual(file_hash(self.root/'business.db'),before)

    def test_static_symlink_cannot_expose_private_file(self):
        code=self.root/'code'; (code/'frontend').mkdir(parents=True)
        link=code/'frontend/leak.json'
        try: link.symlink_to(self.root/'private/accounts.json')
        except OSError: self.skipTest('Windows account has no symbolic-link privilege')
        self.server.code_root=code.resolve()
        self.assertEqual(self.request('/frontend/leak.json')[0],404)


if __name__=='__main__': unittest.main()
