"""Zero-test HTTP runtime helper; no inherited concrete acceptance cases."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import tomllib
from unittest.mock import patch
from uuid import uuid4


class HttpRuntimeMixin:
    def start_http(self):
        from backend.http_server import create_server
        from backend.seed import bootstrap_local_auth
        from backend.inversion.runtime import build_inversion_runtime
        from tests.test_waveform_config import config_sections, toml_text
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        with patch('backend.http_server.seed_database',bootstrap_local_auth):
            self.server=create_server('127.0.0.1',0,Path(self.tmp.name)/'auth.db')
        self.addCleanup(self.server.server_close)
        sections=config_sections(); sections.update(self.raw_config)
        sections['management']={'shared_accounting_bucket':'GW'}
        waveform_path=Path(self.tmp.name)/'waveforms.toml'
        waveform_path.write_text(toml_text(sections),encoding='utf-8')
        raw=tomllib.loads(Path(os.environ['OILWELL_INVERSION_TEST_CONFIG']).read_text(encoding='utf-8'))
        general=dict(enabled=True,identity_db_path=str(self.server.database.path),
            waveform_config_path=str(waveform_path),temp_root=self.tmp.name,total_storage_bytes=2147483648)
        config={'inversion':general,**{k:v for k,v in raw.items() if k in ('postgres_reader','postgres_operator')}}
        self.runtime_path=Path(self.tmp.name)/'runtime.toml'
        self.runtime_path.write_text('\n'.join('['+section+']\n'+'\n'.join(
            key+' = '+json.dumps(value) for key,value in values.items())
            for section,values in config.items()),encoding='utf-8')
        runtime=build_inversion_runtime(self.runtime_path,auth=self.server.auth)
        self.assertEqual((runtime.status,runtime.code),('ready',None))
        runtime.allowed_origins=self.server.inversion_runtime.allowed_origins
        self.server.inversion_runtime=runtime
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.close_server)
        self.session=self.server.auth.login('administrator','Admin@2026')

    def close_server(self):
        self.server.shutdown(); self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def http_bytes(self,method,suffix,body=None,token=None):
        conn=http.client.HTTPConnection(*self.server.server_address,timeout=15)
        try:
            headers={'Authorization':'Bearer '+(token or self.session.token)}
            if body is not None: body=json.dumps(body); headers['Content-Type']='application/json'
            conn.request(method,'/api/production-inversion'+suffix,body,headers)
            response=conn.getresponse()
            return response.status,response.read()
        finally: conn.close()

    def http(self,method,suffix,body=None,token=None):
        status,raw=self.http_bytes(method,suffix,body,token)
        return status,json.loads(raw) if raw else None

    def envelope(self,body=None,version=None,key=None):
        result={'command':{'key':str(key or uuid4()),'expected':{'kind':'absent'} if version is None
            else {'kind':'match','version':version},'reason':'Explicit isolated HTTP validation'}}
        if body is not None: result['body']=body
        return result
