"""Strict waveform HTTP contracts; integration cases use the real handler."""
import importlib
import unittest
from backend.waveforms.errors import WaveformError
import http.client
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from tests.waveform_support import WaveformDatabaseCase,LIMITS
from tests.waveform_dataset_support import DatasetCase


class WaveformHTTPTests(DatasetCase):
    def setUp(self):
        super().setUp()
        from backend.http_server import create_server
        from backend.seed import bootstrap_local_auth
        from backend.waveforms.runtime import build_waveform_runtime
        from tests.test_waveform_config import config_sections,toml_text
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        sections=config_sections(); sections['management']={'shared_accounting_bucket':'GW'}
        for role in ('reader','operator','receiver'): sections['postgres_'+role]=self.raw_config['postgres_'+role]
        path=Path(self.temp.name)/'management.toml'; path.write_text(toml_text(sections),encoding='utf-8'); self.management_path=path
        runtime=build_waveform_runtime(path)
        self.assertEqual(runtime.status,'ready','Management runtime readiness required')
        with patch('backend.http_server.seed_database',bootstrap_local_auth):
            self.server=create_server('127.0.0.1',0,Path(self.temp.name)/'app.db',waveform_config_path=path)
        runtime.close()
        self.tokens={}
        for role,password in {'administrator':'Admin@2026','algorithm_engineer':'Algorithm@2026','reviewer':'Review@2026','maintenance_engineer':'Maintenance@2026'}.items():
            self.tokens[role]=self.server.auth.login(role,password).token
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02}); self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown(); self.thread.join(5); self.server.server_close()

    def test_window_discovery_read_only_permission_and_one_slot(self):
        from tests.waveform_pairing_support import seed_pair
        vector=seed_pair(self); rt=self.server.waveform_runtime
        id=self.scalar('SELECT capture_pk FROM waveforms.capture_metadata LIMIT 1')
        rt.connections.budget=rt.connections.available=1
        status,result=self.request('GET',f'/captures/{id}',role='reviewer')
        self.assertEqual(status,200,result); self.assertIn('streams',result['data'])
        self.assertEqual(result['data']['streams'][1]['channels'][0]['quantity'],'load')
        status,result=self.request('GET',f'/captures/{id}/streams/sensor/window?start=0&count=3&mode=raw',role='reviewer')
        self.assertEqual(status,200,result)
        self.assertEqual([p['value'] for p in result['data']['channels'][0]['points']],[10.,10.,10.])
        self.assertNotIn('raw_value',json.dumps(result)); self.assertNotIn('data_base64',json.dumps(result))
        self.assertEqual(rt.connections.available,1); self.assertFalse(rt.streams)
        self.assertEqual(self.request('GET',f'/captures/{id}/streams/sensor/window?start=0&count=20001&mode=summary')[0],422)
        from backend.waveforms.evidence_contracts import thaw
        from uuid import UUID
        cal=vector['selection']['channels'][0]['calibration']
        body=thaw(vector['evidence_service'].get(UUID(cal['id'])).body)
        for scale in ('1e999','1000000000000000000'):
            status,value=self.post('/evidence',dict(kind='calibration',body=body|dict(scale=scale)))
            self.assertEqual(status,422,value)

    def test_capture_discovery_rejects_late_accepted_manifest_snapshot(self):
        from tests.test_waveform_repository import capture,observation
        from backend.waveforms.repository import WaveformRepository
        from backend.waveforms.queries import WaveformQueries
        manifest,chunk=capture(); repo=WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW')
        repo.ingest(observation(chunk))
        id=self.scalar('SELECT capture_pk FROM waveforms.capture_metadata LIMIT 1')
        original=WaveformQueries.window_snapshot if hasattr(WaveformQueries,'window_snapshot') else None
        self.assertIsNotNone(original,'Coherent window snapshot required')
        calls=0
        def race(queries,*args,**kwargs):
            nonlocal calls
            result=original(queries,*args,**kwargs); calls+=1
            if calls==1: repo.ingest(observation(manifest))
            return result
        with patch.object(WaveformQueries,'window_snapshot',race):
            status,result=self.request('GET',f'/captures/{id}')
        self.assertEqual(status,409,result); self.assertEqual(result['error']['code'],'waveform.stale_dependency')

    def test_window_get_send_half_close_preserves_response(self):
        import socket
        from tests.waveform_pairing_support import seed_pair
        seed_pair(self); id=self.scalar('SELECT capture_pk FROM waveforms.capture_metadata LIMIT 1')
        request=(f'GET /api/waveforms/captures/{id}/streams/sensor/window?start=0&count=3&mode=raw HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nAuthorization: Bearer {self.tokens["reviewer"]}\r\nConnection: close\r\n\r\n').encode()
        with socket.create_connection(('127.0.0.1',self.server.server_port),timeout=10) as sock:
            sock.sendall(request); sock.shutdown(socket.SHUT_WR); received=b''
            while True:
                part=sock.recv(65536)
                if not part: break
                received+=part
        self.assertIn(b'200 OK',received)
        value=json.loads(received.split(b'\r\n\r\n',1)[1])
        self.assertEqual(value['data']['channels'][0]['points'][0]['value'],10.)
        self.assertFalse(self.server.waveform_runtime.streams)

    def test_xy_route_one_slot_and_unchanged_full_inspection_two_slots(self):
        pair,vector=self.pair(); rt=self.server.waveform_runtime
        rt.connections.budget=rt.connections.available=1
        path=f'/pairs/{pair.id}/preview/window?start=0&count=3&mode=summary&budget=3'
        status,result=self.request('GET',path,role='reviewer')
        self.assertEqual(status,200,result); self.assertEqual([p['displacement'] for p in result['data']['points']],[0.,4.,0.])
        self.assertFalse(result['data']['quantitative_use_allowed'])
        self.assertEqual(self.request('GET',f'/pairs/{pair.id}/preview',role='reviewer')[0],503)
        self.assertEqual(self.request('POST',path.split('?')[0],{})[0],405)
        self.assertEqual(self.request('GET',path+'&channel_ids=load')[0],422)
        self.assertEqual(rt.connections.available,1); self.assertFalse(rt.streams)
        rt.connections.budget=rt.connections.available=2
        status,result=self.request('GET',f'/pairs/{pair.id}/preview',role='reviewer')
        self.assertEqual(status,200,result); self.assertEqual(result['data']['current_status'],'eligible')

    def test_window_lost_audit_commit_reply_never_decodes_or_succeeds(self):
        import psycopg
        from tests.waveform_pairing_support import seed_pair
        seed_pair(self); id=self.scalar('SELECT capture_pk FROM waveforms.capture_metadata LIMIT 1')
        before=self.scalar('SELECT count(*) FROM waveforms.audit_metadata'); committed=[]
        class LostReply(psycopg.Connection):
            def commit(self):
                super().commit()
                if getattr(self,'audited_read',False):
                    committed.append(True)
                    raise psycopg.OperationalError('Synthetic lost committed audit reply')
        def connect(**kw):
            base=kw['cursor_factory']
            class AuditedCursor(base):
                def execute(self,query,*args,**kwargs):
                    result=super().execute(query,*args,**kwargs)
                    if isinstance(query,str) and 'waveforms.audited_raw(' in query: self.connection.audited_read=True
                    return result
            return LostReply.connect(**(kw|dict(cursor_factory=AuditedCursor)))
        with patch('backend.waveforms.runtime.psycopg.connect',side_effect=connect),patch('backend.waveforms.windows.parse_frame',side_effect=AssertionError('Unaudited decode')) as parse:
            status,result=self.request('GET',f'/captures/{id}/streams/sensor/window?start=0&count=3&mode=raw')
        self.assertEqual(status,503,result); self.assertFalse(parse.called)
        self.assertEqual(committed,[True]); self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.audit_metadata'),before+1)
        self.assertEqual(self.server.waveform_runtime.connections.available,4); self.assertFalse(self.server.waveform_runtime.streams)

    def test_window_reset_releases_operation_and_capacity(self):
        import socket,struct,os
        from backend.waveforms.windows import WaveformWindows
        from backend.waveforms.runtime import WindowOperation
        from tests.waveform_pairing_support import seed_pair
        seed_pair(self); id=self.scalar('SELECT capture_pk FROM waveforms.capture_metadata LIMIT 1')
        entered=threading.Event(); resume=threading.Event(); ended=threading.Event()
        original=WaveformWindows.samples; cleanup=WindowOperation.__exit__
        def paused(owner,*args,**kwargs):
            entered.set(); resume.wait(5); yield from original(owner,*args,**kwargs)
        def done(owner,*args):
            try: return cleanup(owner,*args)
            finally: ended.set()
        with patch.object(WaveformWindows,'samples',paused),patch.object(WindowOperation,'__exit__',done):
            sock=socket.create_connection(('127.0.0.1',self.server.server_port),timeout=5)
            try:
                sock.sendall((f'GET /api/waveforms/captures/{id}/streams/sensor/window?start=0&count=3&mode=raw HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nAuthorization: Bearer {self.tokens["reviewer"]}\r\n\r\n').encode())
                self.assertTrue(entered.wait(5))
                sock.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('hh' if os.name=='nt' else 'ii',1,0)); sock.close(); resume.set()
                self.assertTrue(ended.wait(5))
            finally: resume.set(); sock.close()
        self.assertFalse(self.server.waveform_runtime.streams); self.assertEqual(self.server.waveform_runtime.connections.available,4)

    def test_window_shutdown_owns_response_until_cleanup_without_handler_escape(self):
        from backend.waveforms.api import WaveformApi
        from tests.waveform_pairing_support import seed_pair
        seed_pair(self); id=self.scalar('SELECT capture_pk FROM waveforms.capture_metadata LIMIT 1'); rt=self.server.waveform_runtime
        entered=threading.Event(); resume=threading.Event(); stopped=threading.Event(); errors=[]
        original=WaveformApi._json
        def pause(api,status,body):
            if status==200 and api.operation:
                entered.set(); resume.wait(5)
            return original(api,status,body)
        def request():
            try: self.request('GET',f'/captures/{id}/streams/sensor/window?start=0&count=3&mode=raw')
            except (OSError,http.client.HTTPException): pass
        def close():
            try: rt.close()
            finally: stopped.set()
        with patch.object(WaveformApi,'_json',pause),patch.object(self.server,'handle_error',side_effect=lambda *a:errors.append('escaped')):
            client=threading.Thread(target=request); client.start()
            self.assertTrue(entered.wait(5)); self.assertEqual(rt.connections.available,4); self.assertEqual(len(rt.streams),1)
            closer=threading.Thread(target=close); closer.start()
            self.assertFalse(stopped.wait(.05)); resume.set(); client.join(5); closer.join(5)
            self.assertFalse(client.is_alive()); self.assertFalse(closer.is_alive())
        self.assertEqual(errors,[]); self.assertFalse(rt.streams)

    def test_window_deadline_returns_complete_error_json(self):
        from backend.waveforms.runtime import WindowOperation
        from tests.waveform_pairing_support import seed_pair
        seed_pair(self); id=self.scalar('SELECT capture_pk FROM waveforms.capture_metadata LIMIT 1')
        original=WindowOperation.__enter__; escaped=[]
        def expired(owner):
            original(owner); owner.deadline=0; return owner
        with patch.object(WindowOperation,'__enter__',expired),patch.object(self.server,'handle_error',side_effect=lambda *a:escaped.append(True)):
            try: status,result=self.request('GET',f'/captures/{id}/streams/sensor/window?start=0&count=3&mode=raw')
            except http.client.IncompleteRead: self.fail('Deadline truncated the controlled error response')
        self.assertEqual(status,503,result); self.assertEqual(result['error']['code'],'waveform.window_timeout')
        self.assertFalse(escaped); self.assertFalse(self.server.waveform_runtime.streams)


    def request(self,method,path,body=None,role='administrator',headers=None,raw=False):
        h={'Authorization':'Bearer '+self.tokens[role]}; h.update(headers or {})
        if body is not None and not isinstance(body,bytes): body=json.dumps(body).encode(); h['Content-Type']='application/json'
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        try:
            c.request(method,'/api/waveforms'+path,body,h); r=c.getresponse(); value=r.read(); self.headers=dict(r.getheaders())
            return r.status,value if raw else json.loads(value)
        finally: c.close()

    def test_cookie_get_and_origin_denial(self):
        status,value=self.request('GET','/captures',headers={'Authorization':'','Cookie':'platform_session='+self.tokens['administrator']})
        self.assertEqual(status,200); self.assertEqual(value['data']['items'],[])
        self.assertEqual(self.request('GET','/captures',headers={'Origin':'https://untrusted.example'})[0],403)

    def test_raw_download_commit_and_permissions(self):
        fact=self.insert_fact(payload=b'exact raw bytes')
        self.assertEqual(self.request('GET',f'/facts/{fact}/raw',role='reviewer')[0],403)
        status,raw=self.request('GET',f'/facts/{fact}/raw',raw=True)
        self.assertEqual((status,raw),(200,b'exact raw bytes'))
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.audit_metadata WHERE fact_id=%s',(fact,)),1)

    def test_import_create_recover_and_denied_replay(self):
        body={'idempotency_key':str(uuid4()),'expected_version':'0','reason':'test',
              'body':{'file_sha256':'a'*64,'bytes':1,'line_count':1,'final_lf':False,'gateway_id':'GW','device_id':'D','capture_id':str(uuid4())}}
        status,value=self.request('POST','/imports',body,role='maintenance_engineer'); self.assertEqual(status,200,value)
        id=value['data']['import_id']; self.assertEqual(value['data']['version'],'1')
        self.assertEqual(self.request('GET','/imports/'+id,role='algorithm_engineer')[1]['data']['missing_lines'],[0])
        with self.server.database.connection() as c:
            c.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='maintenance_engineer') AND permission_id=(SELECT id FROM permissions WHERE code='waveform.import')")
        self.assertEqual(self.request('POST','/imports',body,role='maintenance_engineer')[0],403)

    def post(self,path,body,version=0,role='administrator',key=None):
        return self.request('POST',path,dict(idempotency_key=str(key or uuid4()),expected_version=str(version),reason='Synthetic HTTP test',body=body),role=role)

    def test_every_mutating_route_requires_current_grants(self):
        id=str(uuid4())
        with self.server.database.connection() as c:
            c.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='administrator') AND permission_id IN (SELECT id FROM permissions WHERE code LIKE 'waveform.%' AND code<>'waveform.read')")
        for path in ('/imports',f'/imports/{id}/lines/0',f'/imports/{id}/seal',f'/imports/{id}/cancel','/attachments','/evidence',f'/evidence/{id}/review',f'/evidence/{id}/revoke',f'/captures/{id}/evaluations','/pairs',f'/pairs/{id}/review','/datasets',f'/datasets/{id}/update',f'/datasets/{id}/submit',f'/datasets/{id}/freeze',f'/dataset-versions/{id}/exports'):
            with self.subTest(path=path): self.assertEqual(self.post(path,{})[0],403)
        for path in (f'/imports/{id}',f'/facts/{id}/raw',f'/attachments/{id}/raw',f'/exports/{id}/manifest',f'/exports/{id}/captures/{id}/file','/audit'):
            with self.subTest(path=path): self.assertEqual(self.request('GET',path)[0],403)

    def test_raw_upload_does_not_use_json_body(self):
        from hashlib import sha256
        from tests.test_waveform_repository import capture
        from urllib.parse import quote
        manifest,chunk=capture(); chunk=chunk+b' '*(262144-len(chunk))
        source=manifest+b'\n'+chunk; m=json.loads(manifest)
        body=dict(file_sha256=sha256(source).hexdigest(),bytes=len(source),line_count=2,final_lf=False,gateway_id=m['gateway_id'],device_id=m['device_id'],capture_id=m['capture_id'])
        status,value=self.post('/imports',body); self.assertEqual(status,200,value); id=value['data']['import_id']
        for line,payload in enumerate((manifest,chunk)):
            headers={'Content-Type':'application/octet-stream','Idempotency-Key':str(uuid4()),'If-Match':str(line+1),'X-Waveform-Reason':quote('精确原文')}
            status,value=self.request('POST',f'/imports/{id}/lines/{line}',payload,headers=headers)
            self.assertEqual(status,200,value)
            fact=value['data']['fact_id']; self.assertEqual(self.scalar('SELECT raw_payload FROM waveforms.facts WHERE fact_id=%s',(fact,),role='receiver'),payload)
        self.assertEqual(self.post(f'/imports/{id}/seal',{},3)[0],200)

    def test_two_heads_pagination_and_fixed_stalled_time(self):
        from urllib.parse import urlencode
        from tests.test_waveform_repository import capture,observation
        from backend.waveforms.repository import WaveformRepository
        repo=WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW')
        repo.ingest(observation(capture()[0])); repo.ingest(observation(capture()[0]))
        status,data=self.request('GET','/captures?page_size=1'); self.assertEqual(status,200,data); first=data['data']
        args=dict(page=2,page_size=1,snapshot_revision=first['snapshot_revision'],received_before=first['received_before'])
        self.assertEqual(self.request('GET','/captures?'+urlencode(args))[0],200)
        with self.connect('migration_owner') as c: c.execute('UPDATE waveforms.state_head SET version=version+1')
        self.assertEqual(self.request('GET','/captures?'+urlencode(args))[0],409)
        self.assertEqual(self.request('GET','/captures?snapshot_revision=invalid&received_before='+first['received_before'])[0],422)

    def test_audit_late_commit_count_and_excluded_filter(self):
        from urllib.parse import urlencode
        from psycopg.types.json import Jsonb
        from datetime import datetime,timezone
        import time
        fact=self.insert_fact(); c=self.connect('receiver')
        c.execute("INSERT INTO waveforms.audit(intake_gateway_id,fact_id,actor,action) VALUES('GW',%s,%s,'raw.read')",(fact,Jsonb(dict(domain='test',user_id='1',username='test',role_code='reader'))))
        start=time.monotonic(); status,result=self.request('GET','/audit?family=raw&page_size=1'); elapsed=time.monotonic()-start
        self.assertEqual(status,200,result); first=result['data']; self.assertEqual(first['items'],[])
        c.commit(); c.close()
        args={k:first[k] for k in ('snapshot_revision','received_before','received_after')}; args.update(page=2,page_size=1,family='raw')
        self.assertEqual(self.request('GET','/audit?'+urlencode(args))[0],409)
        print(f'Owned audit pagination cardinality=1 initial_seconds={elapsed:.4f}')

    def test_pair_same_platform_actor_cannot_self_review(self):
        from tests.waveform_pairing_support import seed_pair
        vector=seed_pair(self)
        status,pair=self.post('/pairs',dict(selection=vector['selection'],method_version='linear_adjacent/1'))
        self.assertEqual(status,200,pair); id=pair['data']['id']
        self.assertEqual(pair['data']['creator']['domain'],'platform-local')
        self.assertEqual(self.post(f'/pairs/{id}/review',dict(decision='approve'),1)[0],409)
        self.assertEqual(self.post(f'/pairs/{id}/review',dict(decision='approve'),1,'reviewer')[0],200)

    def test_dataset_discovery_and_audited_export(self):
        from hashlib import sha256
        service,version,vector=self.frozen()
        status,result=self.request('GET',f'/datasets/{version.dataset_id}?view=files&version_id={version.id}')
        self.assertEqual(status,200,result); descriptor=result['data']['items'][0]
        self.assertIn('capture_pk',descriptor)
        status,result=self.post(f'/dataset-versions/{version.id}/exports',dict(purpose='evidence'))
        self.assertEqual(status,200,result); id=result['data']['export']['id']
        status,payload=self.request('GET',f'/exports/{id}/manifest',raw=True); self.assertEqual(status,200)
        self.assertEqual((len(payload),sha256(payload).hexdigest()),(version.manifest_bytes,version.content_sha256))
        status,payload=self.request('GET',f'/exports/{id}/captures/{descriptor["capture_pk"]}/file',raw=True)
        self.assertEqual(status,200); self.assertEqual(sha256(payload).hexdigest(),descriptor['file_sha256'])
        self.assertEqual(self.server.waveform_runtime.connections.available,4)

    def test_failed_commit_not_http_success(self):
        import psycopg
        original=psycopg.connect
        class CommitFailure(psycopg.Connection):
            def commit(self):
                super().commit()
                raise psycopg.OperationalError('private committed reply lost')
        fact=self.insert_fact(payload=b'never unaudited bytes')
        # Preserve the real adapter/factory/database; inject only the commit-reply boundary.
        def connect(**kw): return CommitFailure.connect(**kw)
        with patch('backend.waveforms.runtime.psycopg.connect',side_effect=connect):
            status,result=self.request('GET',f'/facts/{fact}/raw')
        self.assertEqual(status,503); self.assertNotIn('private',json.dumps(result))
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.audit_metadata WHERE fact_id=%s',(fact,)),1)
        self.assertEqual(self.server.waveform_runtime.connections.available,4)

    def test_bad_framing_closes_without_processing_pipelined_request(self):
        import socket
        address=('127.0.0.1',self.server.server_port)
        for headers in ('Content-Length: 2\r\nContent-Length: 2','Content-Length: 2\r\nTransfer-Encoding: chunked','Content-Length: 65537','Content-Length: 2\r\nIf-Match: 1\r\nIf-Match: 1'):
            data=(f'POST /api/waveforms/imports HTTP/1.1\r\nHost: 127.0.0.1:{address[1]}\r\nAuthorization: Bearer {self.tokens["administrator"]}\r\nContent-Type: application/json\r\n{headers}\r\n\r\n{{}}GET /api/waveforms/health HTTP/1.1\r\nHost: 127.0.0.1:{address[1]}\r\n\r\n').encode()
            with socket.create_connection(address,timeout=5) as sock:
                sock.sendall(data); sock.shutdown(socket.SHUT_WR); result=b''
                while True:
                    piece=sock.recv(65536)
                    if not piece: break
                    result+=piece
            self.assertEqual(result.count(b'HTTP/1.'),1,result)
            self.assertNotIn(b'200 OK',result)

    def test_post_cookie_origin_and_expired_session(self):
        headers={'Authorization':'','Cookie':'platform_session='+self.tokens['administrator']}
        self.assertEqual(self.request('POST','/datasets',{},headers=headers)[0],403)
        headers['Origin']=f'http://127.0.0.1:{self.server.server_port}'
        self.assertEqual(self.request('POST','/datasets',{},headers=headers)[0],422)
        with self.server.database.connection() as c: c.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+08:00'")
        self.assertEqual(self.request('GET','/health')[0],401)

    def test_empty_bearer_cookie_post_without_origin_never_invokes_write(self):
        service=self.server.waveform_runtime.services['datasets']; before=self.scalar('SELECT count(*) FROM waveforms.dataset_drafts')
        body=dict(idempotency_key=str(uuid4()),expected_version='0',reason='Synthetic auth boundary',
                  body=dict(title='Must not be written',purpose='raw_archive',split_plan=dict(kind='none')))
        cookie='platform_session='+self.tokens['administrator']
        with patch.object(service,'create',wraps=service.create) as create:
            for authorization in ('Bearer ','Bearer \t  '):
                with self.subTest(authorization=repr(authorization)):
                    status,result=self.request('POST','/datasets',body,headers={'Authorization':authorization,'Cookie':cookie})
                    self.assertEqual(status,403,result)
            self.assertEqual(create.call_count,0,'Cookie fallback must not reach the write service')
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_drafts'),before)
        # Preserve valid browser cookie GET/Origin POST and authenticated Bearer POST.
        headers={'Authorization':'Bearer ','Cookie':cookie}
        self.assertEqual(self.request('GET','/datasets',headers=headers)[0],200)
        headers['Origin']=f'http://127.0.0.1:{self.server.server_port}'
        self.assertEqual(self.request('POST','/datasets',body,headers=headers)[0],200)
        body['idempotency_key']=str(uuid4())
        self.assertEqual(self.request('POST','/datasets',body)[0],200)

    def test_budget_one_allows_draft_get_and_refuses_compound(self):
        status,result=self.post('/datasets',dict(title='budget test',purpose='raw_archive',split_plan=dict(kind='none')))
        self.assertEqual(status,200,result); id=result['data']['id']
        pool=self.server.waveform_runtime.connections; pool.budget=pool.available=1
        self.assertEqual(self.request('GET',f'/datasets/{id}')[0],200)
        self.assertEqual(self.post(f'/dataset-versions/{uuid4()}/exports',dict(purpose='evidence'))[0],503)
        self.assertEqual(pool.available,1)

    def test_dataset_summary_pins_revision_when_freeze_commits_between_reads(self):
        from concurrent.futures import ThreadPoolExecutor
        from backend.waveforms.commands import Actor
        pair,_=self.pair(); writer,submitted=self.submitted(pair)
        rt=self.server.waveform_runtime; pool=rt.connections; pool.budget=pool.available=1
        service=rt.services['datasets']; original=service.get_draft
        entered=threading.Event(); released=threading.Event()
        def paused(id,revision=None):
            entered.set()
            if not released.wait(8): raise RuntimeError('freeze race gate was not released')
            return original(id,revision=revision)
        with patch.object(service,'get_draft',side_effect=paused),ThreadPoolExecutor(1) as executor:
            request=executor.submit(self.request,'GET',f'/datasets/{submitted.id}')
            try:
                self.assertTrue(entered.wait(4),'HTTP summary never reached draft retrieval')
                self.assertEqual(pool.available,0); self.assertFalse(pool.active)
                # A separately connected reviewed producer commits the real freeze
                # after HTTP selection but before HTTP draft retrieval proceeds.
                body=dict(target_id=str(submitted.id)); reviewer=Actor('local','race-reviewer','Synthetic reviewer','reviewer')
                version=writer.freeze(body,self.command('freeze',submitted.id,submitted.revision,reviewer,body=body))
            finally: released.set()
            status,result=request.result(10)
        self.assertEqual(status,200,result)
        self.assertEqual((result['data']['draft']['revision'],result['data']['draft']['state'],result['data']['version']),
                         (str(submitted.revision),'submitted',None))
        self.assertEqual(pool.available,1); self.assertFalse(pool.active)
        self.assertEqual(self.request('GET',f'/datasets/{submitted.id}')[0],503)
        status,result=self.request('GET',f'/datasets/{submitted.id}?revision={submitted.revision}')
        self.assertEqual(status,200,result); self.assertEqual(result['data']['draft']['state'],'submitted'); self.assertIsNone(result['data']['version'])
        pool.budget=pool.available=2
        for suffix in ('',f'?version_id={version.id}'):
            status,result=self.request('GET',f'/datasets/{submitted.id}'+suffix)
            self.assertEqual(status,200,result)
            self.assertEqual((result['data']['draft']['state'],result['data']['version']['id']),('frozen',str(version.id)))
        self.assertEqual(self.request('GET',f'/datasets/{submitted.id}?version_id={uuid4()}')[0],404)
        self.assertEqual(self.request('GET',f'/datasets/{submitted.id}?revision={submitted.revision}&version_id={version.id}')[0],422)
        self.assertEqual(pool.available,2); self.assertFalse(pool.active)

    def test_lazy_stream_audits_errors_restrictions_and_head(self):
        import socket
        import psycopg
        from hashlib import sha256
        service,version,vector=self.frozen()
        status,result=self.post(f'/dataset-versions/{version.id}/exports',dict(purpose='evidence')); self.assertEqual(status,200,result)
        id=result['data']['export']['id']
        research=self.post(f'/dataset-versions/{version.id}/exports',dict(purpose='research'))[1]['data']['export']['id']
        descriptor=self.request('GET',f'/datasets/{version.dataset_id}?view=files&version_id={version.id}')[1]['data']['items'][0]
        exporter=self.server.waveform_runtime.services['exports']; original=exporter._raw
        before=self.scalar("SELECT count(*) FROM waveforms.business_audit WHERE action='exports.download'")
        self.assertEqual(self.request('HEAD',f'/exports/{id}/manifest',raw=True)[0],405)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.business_audit WHERE action='exports.download'"),before)
        # The first advance must commit its business audit before any HTTP200.
        connect=psycopg.connect; operator=self.settings['operator']['user']
        class LostAuditReply(psycopg.Connection):
            def commit(self):
                active=self.info.transaction_status==psycopg.pq.TransactionStatus.INTRANS
                super().commit()
                if active: raise psycopg.OperationalError('private audit reply')
        def injected(**kw): return LostAuditReply.connect(**kw) if kw['user']==operator else connect(**kw)
        with patch('backend.waveforms.runtime.psycopg.connect',side_effect=injected):
            self.assertEqual(self.request('GET',f'/exports/{id}/manifest')[0],503)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.business_audit WHERE action='exports.download'"),before+1)
        with patch.object(exporter,'_raw',side_effect=psycopg.OperationalError('first raw fails')):
            self.assertEqual(self.request('GET',f'/exports/{id}/captures/{descriptor["capture_pk"]}/file')[0],503)
        calls=[]
        def later(fact,actor):
            calls.append(fact)
            if len(calls)==2: raise psycopg.OperationalError('later raw fails')
            return original(fact,actor)
        with patch.object(exporter,'_raw',side_effect=later),socket.create_connection(('127.0.0.1',self.server.server_port),timeout=10) as sock:
            sock.sendall((f'GET /api/waveforms/exports/{id}/captures/{descriptor["capture_pk"]}/file HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nAuthorization: Bearer {self.tokens["administrator"]}\r\n\r\n').encode())
            response=b''
            while True:
                piece=sock.recv(65536)
                if not piece: break
                response+=piece
        self.assertEqual(response.count(b'HTTP/1.'),1); self.assertIn(b'200 OK',response)
        self.assertNotIn(b'waveform.unavailable',response); self.assertLess(len(response.split(b'\r\n\r\n',1)[1]),descriptor['content_length'])
        self.assertEqual(len(calls),2); self.assertEqual(self.server.waveform_runtime.connections.available,4)
        profile=vector['selection']['profile']
        status,result=self.post(f'/evidence/{profile["id"]}/revoke',{},int(profile['version'])); self.assertEqual(status,200,result)
        self.assertEqual(self.request('GET',f'/exports/{research}/manifest')[0],409)
        status,raw=self.request('GET',f'/exports/{id}/manifest',raw=True); self.assertEqual(status,200)
        self.assertEqual(sha256(raw).hexdigest(),version.content_sha256); self.assertEqual(self.headers['X-Waveform-Restriction-State'],'restricted')
        with self.server.database.connection() as c:
            c.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='administrator') AND permission_id=(SELECT id FROM permissions WHERE code='waveform.export')")
        self.assertEqual(self.request('GET',f'/exports/{id}/manifest')[0],403)

    def test_attachment_boundary_and_concurrent_versions(self):
        from hashlib import sha256
        from concurrent.futures import ThreadPoolExecutor
        payload=b'%PDF-1.7\n'+b' '*(4194304-9)
        headers={'Content-Type':'application/pdf','Idempotency-Key':str(uuid4()),'If-Match':'0','X-Waveform-Reason':'Synthetic','X-Content-SHA256':sha256(payload).hexdigest()}
        status,result=self.request('POST','/attachments',payload,headers=headers); self.assertEqual(status,200,result)
        id=result['data']['id']
        self.assertEqual(self.request('GET',f'/attachments/{id}/raw',raw=True),(200,payload))
        self.assertEqual(self.request('POST','/attachments',payload+b' ',headers=headers)[0],413)
        status,result=self.post('/datasets',dict(title='Concurrent',purpose='raw_archive',split_plan=dict(kind='none'))); self.assertEqual(status,200,result)
        id=result['data']['id']
        def update(n): return self.post(f'/datasets/{id}/update',dict(changes=dict(metadata=dict(title='Version '+str(n),purpose='raw_archive',split_plan=dict(kind='none')))),1)[0]
        with ThreadPoolExecutor(2) as workers: results=list(workers.map(update,range(2)))
        self.assertEqual(sorted(results),[200,409])

    def test_actual_producer_revisions_registry_and_stalled_cutoff(self):
        from urllib.parse import urlencode
        from datetime import datetime,timezone
        from tests.test_waveform_repository import capture,observation
        from backend.waveforms.repository import WaveformRepository
        from backend.waveforms.assembly import Assembler
        from backend.waveforms.outbox import WaveOutbox
        from tools.provision_waveform_registry_test import owner_settings
        from tests.waveform_registry_support import WaveformRegistryCase
        repo=WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW')
        def token():
            status,result=self.request('GET','/captures?page_size=1'); self.assertEqual(status,200,result)
            v=result['data']; return dict(page=2,page_size=1,snapshot_revision=v['snapshot_revision'],received_before=v['received_before'])
        def check(args,status=409): self.assertEqual(self.request('GET','/captures?'+urlencode(args))[0],status)
        args=token(); manifest,chunk=capture(); repo.ingest(observation(manifest)); check(args)
        args=token(); repo.ingest(observation(chunk)); check(args)
        # Existing view says stalled NOW; explicit pagination cutoff precedes24h.
        other=capture()[0]; repo.ingest(observation(other)); args=token()
        args.update(page=1,received_before='2026-09-29T23:59:59.000000Z',stalled='false')
        from hashlib import sha256
        scope=dict(resource='captures',filters=dict(stalled='false'),page_size=1,received_before=args['received_before'],received_after=None)
        args['snapshot_revision']=args['snapshot_revision'].split('.q')[0]+'.q'+sha256(json.dumps(scope,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        status,first=self.request('GET','/captures?'+urlencode(args)); self.assertEqual(status,200,first)
        self.assertFalse(first['data']['items'][0]['stalled']); args['page']=2; check(args,200)
        args=token(); assembler=Assembler(lambda:self.connect('assembler'),clock=None,limits=LIMITS)
        lease=assembler.claim(uuid4()); self.assertIsNotNone(lease); assembler.run(lease); check(args)
        outbox=WaveOutbox(lambda:self.connect('receipt_publisher'),gateway_id='GW',limits=LIMITS)
        leases=outbox.claim(uuid4(),limit=10); self.assertTrue(leases)
        args=token(); self.assertTrue(outbox.finish(leases[0],delivered=True,error_code=None)); check(args)
        args=token(); self.assertFalse(outbox.finish(leases[0],delivered=True,error_code=None)); check(args,200)
        if len(leases)>1:
            args=token(); self.assertTrue(outbox.finish(leases[1],delivered=False,error_code='mqtt.failed')); check(args)
        self.registry_settings=owner_settings(); self.registry_connect=lambda:WaveformRegistryCase.registry_connect(self)
        args=token(); WaveformRegistryCase.registry_command(self,'well.create',area_code='HTTP',well_code='W'+uuid4().hex,name='Synthetic HTTP revision')
        check(args)

    def test_query_bound_tokens_reject_changed_scope_with_same_heads_and_counts(self):
        from urllib.parse import urlencode
        status,result=self.request('GET','/audit'); self.assertEqual(status,200,result); data=result['data']
        self.assertRegex(data['snapshot_revision'],r'\.q[0-9a-f]{64}$')
        args={k:data[k] for k in ('snapshot_revision','received_before','received_after')}; args.update(page=2)
        self.assertEqual(self.request('GET','/audit?'+urlencode(list(reversed(list(args.items())))))[0],200)
        self.assertEqual(self.request('GET','/audit?'+urlencode(args|dict(page_size=50)))[0],200)
        from datetime import timedelta
        from backend.waveforms.queries import timestamp,utc
        for change in (dict(received_before=utc(timestamp(args['received_before'])-timedelta(seconds=1))),
                       dict(received_after=utc(timestamp(args['received_after'])+timedelta(seconds=1))),
                       dict(page_size=1),dict(family='raw'),dict(actor_id='other')):
            self.assertEqual(self.request('GET','/audit?'+urlencode(args|change))[0],409)
        self.assertEqual(self.request('GET','/audit?'+urlencode(args|dict(snapshot_revision=data['snapshot_revision'][:-1]+'G')))[0],422)
        from backend.waveforms.repository import WaveformRepository
        from tests.test_waveform_repository import capture,observation
        repo=WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW')
        repo.ingest(observation(capture()[0])); repo.ingest(observation(capture()[0]))
        captures=self.request('GET','/captures')[1]['data']['items']; a,b=[v['capture_pk'] for v in captures]
        first=self.request('GET',f'/captures/{a}?view=facts')[1]['data']
        args=dict(view='facts',page=2,snapshot_revision=first['snapshot_revision'],received_before=first['received_before'])
        self.assertEqual(self.request('GET',f'/captures/{b}?'+urlencode(args))[0],409)

    def test_metadata_route_queries_and_exact_audit_subject_filters(self):
        from tests.waveform_pairing_support import seed_pair
        from psycopg.types.json import Jsonb
        from urllib.parse import urlencode
        vector=seed_pair(self)
        captures=self.request('GET','/captures')[1]['data']['items']; id=captures[0]['capture_pk']
        for path in ('/health','/evidence?kind=calibration&state=approved','/pairs','/datasets',f'/captures/{id}',f'/captures/{id}?view=facts',f'/captures/{id}?view=outcomes',f'/captures/{id}?view=provenance',f'/captures/{id}/evaluations',f'/evidence/{vector["profile"].id}'):
            status,result=self.request('GET',path)
            with self.subTest(path=path): self.assertEqual(status,200,result)
        self.assertEqual(self.request('GET','/captures?state=untrusted')[0],422)
        fact=self.insert_fact(); actor=dict(domain='test',user_id='wanted',username='test',role_code='reader')
        with self.connect('receiver') as c:
            c.execute("INSERT INTO waveforms.audit(intake_gateway_id,fact_id,actor,action,details) VALUES('GW',NULL,%s,'imports.test',%s)",(Jsonb(actor),Jsonb(dict(subject_id=str(fact)))))
        result=self.request('GET','/audit?family=raw&actor_id=wanted')[1]['data']; self.assertEqual(len(result['items']),1); self.assertIsNone(result['items'][0]['subject_id'])
        args=dict(family='raw',actor_id='wanted',page=2,snapshot_revision=result['snapshot_revision'],received_before=result['received_before'],received_after=result['received_after'])
        with self.connect('receiver') as c:
            c.execute("INSERT INTO waveforms.audit(intake_gateway_id,fact_id,actor,action) VALUES('GW',%s,%s,'raw.read')",(fact,Jsonb(actor|dict(user_id='excluded'))))
        self.assertEqual(self.request('GET','/audit?'+urlencode(args))[0],200)
        self.assertEqual(self.request('GET',f'/audit?family=raw&actor_id=wanted&subject_id={fact}')[1]['data']['items'],[])
        self.assertEqual(self.request('GET',f'/captures/{id}/streams/sensor/window?start=0&count=20001&mode=summary&budget=2000')[0],422)

    def test_incremental_1000_members_reachable_with_small_commands(self):
        from urllib.parse import urlencode
        status,result=self.post('/datasets',dict(title='HTTP1000',purpose='raw_archive',split_plan=dict(kind='none')))
        self.assertEqual(status,200,result); id=result['data']['id']; revision=1
        for batch in range(10):
            members=[dict(kind='capture',reference_id=str(uuid4()),quality_run_id=str(uuid4()),split='unassigned') for _ in range(100)]
            body=dict(changes=dict(upsert_members=members)); self.assertLess(len(json.dumps(body).encode()),65536)
            status,result=self.post(f'/datasets/{id}/update',body,revision); self.assertEqual(status,200,result); revision+=1
        self.assertEqual(result['data']['member_count'],1000)
        status,result=self.request('GET',f'/datasets/{id}?view=members&page_size=100'); self.assertEqual(status,200,result)
        page=result['data']; self.assertEqual(len(page['items']),100)
        self.assertEqual(set(page['items'][0]),{'kind','reference_id','quality_run_id','split'})
        args=dict(view='members',page=10,page_size=100,snapshot_revision=page['snapshot_revision'],received_before=page['received_before'])
        self.assertEqual(len(self.request('GET',f'/datasets/{id}?'+urlencode(args))[1]['data']['items']),100)
        status,result=self.post(f'/datasets/{id}/update',dict(changes=dict(upsert_members=[dict(kind='capture',reference_id=str(uuid4()),quality_run_id=str(uuid4()),split='unassigned')])),revision)
        self.assertNotEqual(status,200)

    def test_multichunk_manifest_stream_and_disconnected_client_cleanup(self):
        import socket,time
        from hashlib import sha256
        from types import SimpleNamespace
        from uuid import UUID
        pair,vector=self.pair(); members=[dict(kind='pair',reference_id=str(pair.id),quality_run_id=str(pair.left_quality_run_id),split='unassigned')]
        for _ in range(11):
            status,result=self.post('/pairs',dict(selection=vector['selection'],method_version='linear_adjacent/1'))
            self.assertEqual(status,200,result); p=result['data']
            self.assertEqual(self.post(f'/pairs/{p["id"]}/review',dict(decision='approve'),1,'reviewer')[0],200)
            members.append(dict(kind='pair',reference_id=p['id'],quality_run_id=p['left_quality_run_id'],split='unassigned'))
        status,result=self.post('/datasets',dict(title='Multiple certificates',purpose='software_pair_test',split_plan=dict(kind='none'))); self.assertEqual(status,200,result); id=result['data']['id']
        self.assertEqual(self.post(f'/datasets/{id}/update',dict(changes=dict(upsert_members=members)),1)[0],200)
        self.assertEqual(self.post(f'/datasets/{id}/submit',{},2)[0],200)
        status,result=self.post(f'/datasets/{id}/freeze',{},3,'reviewer'); self.assertEqual(status,200,result); version=result['data']
        self.assertGreater(version['manifest_bytes'],65536)
        status,result=self.post(f'/dataset-versions/{version["id"]}/exports',dict(purpose='evidence')); self.assertEqual(status,200,result); export=result['data']['export']['id']
        status,raw=self.request('GET',f'/exports/{export}/manifest',raw=True)
        self.assertEqual(status,200); self.assertEqual((len(raw),sha256(raw).hexdigest()),(version['manifest_bytes'],version['content_sha256']))
        from backend.waveforms.export import ExportContext
        original_stream=ExportContext.stream_manifest; second=threading.Event(); release=threading.Event()
        def gated(context):
            iterator=original_stream(context)
            try:
                for n,piece in enumerate(iterator):
                    if n==1:
                        second.set()
                        if not release.wait(4): raise RuntimeError('consumer eagerly joined stream')
                    yield piece
            finally: iterator.close()
        with patch.object(ExportContext,'stream_manifest',gated):
            # Open/authorize/prime precede headers; the independent second-piece
            # gate below, not a short client deadline, detects eager joining.
            c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
            try:
                c.request('GET',f'/api/waveforms/exports/{export}/manifest',headers={'Authorization':'Bearer '+self.tokens['administrator']})
                response=c.getresponse(); self.assertEqual(response.status,200)
                self.assertTrue(second.wait(2)); self.assertFalse(self.server.waveform_runtime.connections.active)
                release.set(); self.assertEqual(len(response.read()),version['manifest_bytes'])
            finally: release.set(); c.close()
        # Exercise a real socket whose peer closes immediately after response headers.
        with socket.create_connection(('127.0.0.1',self.server.server_port),timeout=10) as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024)
            sock.sendall((f'GET /api/waveforms/exports/{export}/manifest HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nAuthorization: Bearer {self.tokens["administrator"]}\r\n\r\n').encode())
            received=b''
            while b'\r\n\r\n' not in received: received+=sock.recv(1)
            self.assertIn(b'200 OK',received)
        rt=self.server.waveform_runtime; deadline=time.monotonic()+7
        while rt.streams and time.monotonic()<deadline: time.sleep(.02)
        self.assertFalse(rt.streams); self.assertEqual(rt.connections.available,4)
        self.assertEqual(self.request('GET','/health')[0],200)
        from backend.waveforms.api import WaveformApi
        original_headers=WaveformApi._headers
        def narrow_headers(api,*args,**kwargs):
            api.h.connection.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024)
            return original_headers(api,*args,**kwargs)
        # A stalled real peer is cut off by the five-second write timeout.
        with patch.object(WaveformApi,'_headers',narrow_headers),socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,1024); sock.settimeout(10); sock.connect(('127.0.0.1',self.server.server_port))
            sock.sendall((f'GET /api/waveforms/exports/{export}/manifest HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nAuthorization: Bearer {self.tokens["administrator"]}\r\n\r\n').encode())
            received=b''
            while b'\r\n\r\n' not in received: received+=sock.recv(1)
            self.assertTrue(rt.streams)
            deadline=time.monotonic()+7
            while rt.streams and time.monotonic()<deadline: time.sleep(.02)
            self.assertFalse(rt.streams,'Stalled peer retained a download owner')
        self.assertEqual(rt.connections.available,4)
        print(f'Owned manifest bytes={version["manifest_bytes"]} disconnected-stream cleanup verified')

    def test_real_connection_peak_and_active_query_cancellation(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        import psycopg
        pool=self.server.waveform_runtime.connections; pool.budget=pool.available=3
        held=threading.Event(); release=threading.Event()
        def compound():
            with pool.reserve(2),pool.factory('operator')(),pool.factory('reader')():
                held.set(); self.assertTrue(release.wait(4))
        with ThreadPoolExecutor(1) as executor:
            future=executor.submit(compound); self.assertTrue(held.wait(2))
            try:
                with pool.reserve(1),pool.factory('receiver')(): self.assertEqual(len(pool.active),3)
            finally: release.set()
            future.result(3)
        self.assertEqual(pool.available,3)
        entered=threading.Event(); errors=[]
        def query():
            try:
                with pool.factory('reader')() as c:
                    entered.set(); c.execute('SELECT pg_sleep(30)')
            except psycopg.Error as error: errors.append(type(error).__name__)
        thread=threading.Thread(target=query); thread.start(); self.assertTrue(entered.wait(2))
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if self.scalar("SELECT count(*) FROM pg_stat_activity WHERE usename=current_user AND query='SELECT pg_sleep(30)' AND state='active'"): break
            time.sleep(.02)
        start=time.monotonic(); pool.close(); thread.join(3)
        self.assertFalse(thread.is_alive()); self.assertIn('QueryCanceled',errors); self.assertLess(time.monotonic()-start,5)
        self.assertEqual(pool.available,3); self.assertFalse(pool.active)

    def test_stop_during_first_stream_advance_closes_all_owners(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        _,version,_=self.frozen()
        result=self.post(f'/dataset-versions/{version.id}/exports',dict(purpose='evidence'))[1]; id=result['data']['export']['id']
        rt=self.server.waveform_runtime; exporter=rt.services['exports']; original=exporter._authorize_download
        entered=threading.Event(); release=threading.Event()
        def paused(context,key):
            result=original(context,key); entered.set()
            if not release.wait(5): raise RuntimeError('stop test release missing')
            return result
        def request():
            try: return self.request('GET',f'/exports/{id}/manifest',raw=True)[0]
            except (OSError,http.client.HTTPException): return 'closed'
        with patch.object(exporter,'_authorize_download',side_effect=paused),ThreadPoolExecutor(2) as executor:
            response=executor.submit(request); self.assertTrue(entered.wait(3)); stopping=executor.submit(rt.close)
            try:
                deadline=time.monotonic()+2
                while not rt.connections.stopped and time.monotonic()<deadline: time.sleep(.01)
                self.assertTrue(rt.connections.stopped)
            finally: release.set()
            stopping.result(4); self.assertNotEqual(response.result(4),200)
        self.assertFalse(rt.streams); self.assertFalse(rt.connections.active); self.assertEqual(rt.connections.available,4)

    def test_readiness_missing_grant_is_unavailable_without_repair(self):
        from psycopg import sql
        from backend.waveforms.runtime import build_waveform_runtime
        reader=sql.Identifier(self.settings['reader']['user']); before=self.scalar('SELECT version FROM waveforms.state_head')
        with self.connect('migration_owner') as c: c.execute(sql.SQL('REVOKE SELECT(version) ON waveforms.state_head FROM {}').format(reader))
        try:
            runtime=build_waveform_runtime(self.management_path)
            self.assertEqual(runtime.status,'unavailable')
            with self.connect('reader') as c:
                self.assertFalse(c.execute("SELECT has_column_privilege(current_user,'waveforms.state_head','version','SELECT')").fetchone()[0])
        finally:
            with self.connect('migration_owner') as c: c.execute(sql.SQL('GRANT SELECT(version) ON waveforms.state_head TO {}').format(reader))
        self.assertEqual(self.scalar('SELECT version FROM waveforms.state_head'),before)
        for state in ('disabled','unavailable'):
            self.server.waveform_runtime.status=state
            self.assertEqual(self.request('GET','/captures')[0],503)

    def test_internal_metadata_lookups_keep_bounded_readonly_transactions(self):
        from contextlib import contextmanager
        from backend.waveforms.commands import Actor
        queries=self.server.waveform_runtime.queries; original=queries.connect
        @contextmanager
        def inspected():
            with original() as connection:
                try: yield connection
                finally:
                    actual=connection.execute("SELECT current_setting('statement_timeout'),current_setting('lock_timeout'),current_setting('transaction_read_only')").fetchone()
                    self.assertEqual(actual,('3s','1s','on'))
        operations=(lambda:queries.capture_key(uuid4()),lambda:queries.dataset_selection(uuid4()),
                    lambda:queries.export_file(uuid4(),uuid4(),Actor('platform-local','1','test','reviewer')))
        with patch.object(queries,'connect',inspected):
            for operation in operations:
                with self.subTest(operation=operations.index(operation)):
                    try: operation()
                    except WaveformError as error: self.assertEqual(error.status,404)








class RouteTests(unittest.TestCase):
    def setUp(self):
        try: self.routes=importlib.import_module('backend.waveforms.routes')
        except ModuleNotFoundError: self.fail('Waveform routes are required')

    def test_summary_input_can_exceed_raw_window_limit(self):
        value=self.routes.query('start=-1&count=20001&mode=summary&budget=2000','window')
        self.assertEqual(value['count'],20001)
        for q in ('start=-0&count=1&mode=raw','start=0&count=20001&mode=raw','start=0&count=1&mode=summary&budget=2001'):
            with self.assertRaises(WaveformError): self.routes.query(q,'window')

    def test_unknown_duplicate_and_encoded_identifiers_fail(self):
        for q in ('page=1&page=2','untrusted=1','page=01','page_size=201'):
            with self.assertRaises(WaveformError): self.routes.query(q,'captures')
        for path in ('/api/waveforms/captures/%30','/api/waveforms/captures/00000000-0000-0000-0000-00000000000A'):
            with self.assertRaises(WaveformError): self.routes.route('GET',path)

    def test_known_wrong_method_is_distinct_from_unknown_route(self):
        for method,path,status in (('POST','/api/waveforms/captures',405),('unsupported','/api/waveforms/unknown',404)):
            with self.assertRaises(WaveformError) as caught: self.routes.route(method,path)
            self.assertEqual(caught.exception.status,status)

    def test_json_duplicate_depth_nonfinite_and_multiline(self):
        from backend.waveforms.api import strict_json
        self.assertEqual(strict_json(b'{\n"value":1\n}'),{'value':1})
        for value in (b'{"a":1,"a":2}',b'{"a":NaN}',b'{"a":1e999}',b'{"a":'+b'['*17+b'0'+b']'*17+b'}',b'\xff'):
            with self.assertRaises(WaveformError): strict_json(value)

    def test_success_header_write_has_bounded_timeout_and_restores(self):
        from backend.waveforms.api import WaveformApi
        from types import SimpleNamespace
        class Socket:
            timeout=17
            def gettimeout(self): return self.timeout
            def settimeout(self,value): self.timeout=value
        sock=Socket(); seen=[]
        handler=SimpleNamespace(connection=sock,send_response=lambda *a:None,send_header=lambda *a:None,end_headers=lambda:seen.append(sock.timeout))
        api=WaveformApi(handler); api._headers(200,3,'application/octet-stream')
        self.assertEqual(seen,[5]); self.assertEqual(sock.timeout,17)
