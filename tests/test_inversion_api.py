"""HTTP boundary with real SQLite identity and an ephemeral local server."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.http_server import create_server
from backend.seed import bootstrap_local_auth

PREFIX = '/api/production-inversion'


class EnvelopeTests(unittest.TestCase):
    def test_definite_noncommit_proof_is_action_and_code_specific(self):
        from backend.inversion.api import noncommit_details
        self.assertEqual(noncommit_details('assemblies.create',409,'inversion.attachment_mismatch'),
                         dict(commit_outcome='not_committed',action='assemblies.create'))
        for action,status,code in (('exports.create',409,'inversion.attachment_mismatch'),
                ('parameters.create',409,'inversion.attachment_mismatch'),
                ('assemblies.create',503,'inversion.attachment_mismatch'),
                ('assemblies.create',409,'inversion.export_manifest_corrupt'),
                ('assemblies.create',409,'inversion.idempotency_conflict')):
            self.assertEqual(noncommit_details(action,status,code),{})

    def test_installed_model_identifier_requires_bounded_string(self):
        from backend.inversion.api import InversionApi
        from backend.inversion.errors import InversionContractError
        from backend.waveforms.commands import Actor
        from uuid import uuid4
        actor = Actor('local', '1', 'Author', 'administrator')
        envelope = {'command': {'key': str(uuid4()), 'expected': {'kind': 'absent'},
                               'reason': 'Register installed model'}, 'body': {}}
        for value in (None, True, 1, [], {}, '', ' ', 'a' * 129):
            with self.subTest(value=value):
                envelope['body'] = {'installed_id': value}
                with self.assertRaises(InversionContractError):
                    InversionApi._envelope('models.create', envelope, actor)
        for value in ('beam_energy_balance/1', 'not_installed/1'):
            envelope['body'] = {'installed_id': value}
            self.assertEqual(InversionApi._envelope('models.create', envelope, actor)[1], envelope['body'])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        with patch('backend.http_server.seed_database', bootstrap_local_auth):
            self.server = create_server('127.0.0.1', 0, Path(self.tmp.name) / 'app.db')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close)
        self.token = self.server.auth.login('administrator', 'Admin@2026').token

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5)

    def request(self, method, suffix, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        base = {'Authorization': 'Bearer ' + self.token}
        if body is not None: body = json.dumps(body); base['Content-Type'] = 'application/json'
        conn.request(method, PREFIX + suffix, body, base | (headers or {}))
        response = conn.getresponse(); payload = response.read(); conn.close()
        return response.status, json.loads(payload) if payload else None, response.getheader('Cache-Control')

    def test_health_reports_disabled_and_no_store(self):
        status, body, cache = self.request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertEqual(body['data']['status'], 'disabled')
        self.assertEqual(cache, 'no-store')

    def test_input_metadata_opt_in_requires_waveform_read_before_dispatch(self):
        path='/results/11111111-1111-4111-8111-111111111111'
        with self.server.database.transaction(None,'test.revoke','role',None) as conn:
            conn.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='administrator') AND permission_id=(SELECT id FROM permissions WHERE code='waveform.read')")
        self.assertEqual(self.request('GET',path)[0],503)  # Default read still reaches disabled runtime.
        status,_,cache=self.request('GET',path+'?include_input_metadata=true')
        self.assertEqual((status,cache),(403,'no-store'))
        for query in ('include_input_metadata=false','include_input_metadata=1','include_input_metadata=true&include_input_metadata=true'):
            self.assertEqual(self.request('GET',path+'?'+query)[0],400)

    def parameter_runtime(self):
        from backend.inversion.parameters import ParameterService
        from backend.inversion.runtime import InversionRuntime
        from unittest.mock import MagicMock
        conn=MagicMock(); conn.__enter__.return_value=conn
        conn.cursor.return_value.fetchone.return_value=None
        parameters=ParameterService(lambda:conn,read_connect=lambda:conn,authorize=lambda *_:None,accounting_bucket='GW')
        runtime=InversionRuntime('ready')
        runtime.allowed_origins=self.server.inversion_runtime.allowed_origins
        runtime.services_for=lambda _: {'parameters':parameters}
        self.server.inversion_runtime=runtime
        return parameters

    def test_missing_public_parameter_is_404(self):
        from uuid import uuid4
        self.parameter_runtime()
        status,payload,_=self.request('GET','/parameters/'+str(uuid4()))
        self.assertEqual(status,404,payload)
        self.assertEqual(payload['code'],'inversion.parameter_missing')

    def test_parameter_dependency_and_version_conflicts_remain_409(self):
        from uuid import uuid4
        from backend.inversion.transactions import InversionError
        parameters=self.parameter_runtime(); path='/parameters/'+str(uuid4())
        command={'key':str(uuid4()),'expected':{'kind':'absent'},'reason':'Submit absent dependency'}
        status,payload,_=self.request('POST',path+'/submit',{'command':command})
        self.assertEqual(status,409,payload); self.assertEqual(payload['code'],'inversion.parameter_missing')
        for code in ('dependency_missing','version_conflict'):
            with patch.object(parameters,'get',side_effect=InversionError(code,status=409)):
                status,payload,_=self.request('GET',path)
                self.assertEqual(status,409,payload); self.assertEqual(payload['code'],'inversion.'+code)

    def test_unknown_method_path_headers_and_fields_refused(self):
        for method, path, expected in [('DELETE','/runs',405),('GET','/runs/finish',400),('GET','/runs%2f',400),('GET','/health?surprise=1',400)]:
            with self.subTest(path=path): self.assertEqual(self.request(method,path)[0],expected)
        self.assertEqual(self.request('GET','/health',headers={'Transfer-Encoding':'chunked'})[0],400)
        self.assertEqual(self.request('POST','/runs',{'actor':'forged'})[0],400)

    def test_mechanical_versions_has_finite_bounded_read_route(self):
        from uuid import uuid4
        path='/assemblies/'+str(uuid4())+'/versions'
        # A valid disabled read reaches availability, not unknown-route404.
        self.assertEqual(self.request('GET',path)[0],503)
        for query in ('?limit=101','?offset=100001','?limit=1&limit=2','?unknown=1'):
            self.assertEqual(self.request('GET',path+query)[0],400)
        self.assertEqual(self.request('POST',path,{})[0],405)

    def test_parameter_assembly_filter_requires_canonical_uuid(self):
        from uuid import uuid4
        identifier=str(uuid4())
        self.assertEqual(self.request('GET','/parameters?assembly_id='+identifier+'&limit=1')[0],503)
        for query in ('?assembly_id=bad','?assembly_id='+identifier.upper(),
                      '?assembly_id='+identifier+'&assembly_id='+identifier,
                      '?assembly_id='+identifier+'&unknown=1'):
            self.assertEqual(self.request('GET','/parameters'+query)[0],400,query)

    def test_body_limit_before_json_parse(self):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        conn.request('POST', PREFIX+'/runs', b'{', {'Authorization':'Bearer '+self.token, 'Content-Type':'application/json','Content-Length':'262145'})
        response=conn.getresponse(); response.read(); conn.close()
        self.assertEqual(response.status, 400)

    def test_preflight_requires_no_command_envelope(self):
        from backend.inversion.runtime import InversionRuntime
        class Queries:
            def preflight(self, body): return dict(allowed=True, missing=[], provisional=True)
        runtime = InversionRuntime('ready', queries=Queries())
        runtime.allowed_origins = self.server.inversion_runtime.allowed_origins
        self.server.inversion_runtime = runtime
        from uuid import uuid4
        body = {k:str(uuid4()) for k in ('dataset_version_id','pair_id','assembly_id','parameter_id','model_id')}
        body.update({k:'a'*64 for k in ('dataset_sha256','pair_sha256','parameter_sha256','model_sha256')})
        body.update(member_ordinal=0,pair_review_version=1,parameter_review_version=1,model_review_version=1,selection_version=1,operating_condition_ids=[str(uuid4())])
        self.assertEqual(self.request('POST','/runs/preflight',{'body':body})[0],200)

    def test_duplicate_auth_origin_and_body_ambiguity_refused(self):
        for headers in ({'Host':'evil.invalid'},{'Origin':'http://evil.invalid'}):
            self.assertEqual(self.request('GET','/health',headers=headers)[0],403)
        conn=http.client.HTTPConnection(*self.server.server_address,timeout=5)
        conn.putrequest('GET',PREFIX+'/health')
        conn.putheader('Authorization','Bearer '+self.token)
        conn.putheader('Authorization','Bearer '+self.token)
        conn.endheaders(); response=conn.getresponse(); response.read(); conn.close()
        self.assertEqual(response.status,400)
        conn=http.client.HTTPConnection(*self.server.server_address,timeout=5)
        conn.request('POST',PREFIX+'/runs',b'{"body":{},"body":{}}',{'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        response=conn.getresponse(); response.read(); conn.close(); self.assertEqual(response.status,400)

    def test_expected_precondition_has_one_canonical_wire_shape(self):
        from uuid import uuid4
        for expected in ({'kind':'absent','version':None},{'kind':'match','version':True},{'kind':'match','version':1.5},{'kind':'match','version':0}):
            body={'command':{'key':str(uuid4()),'expected':expected,'reason':'Strict precondition'},'body':{}}
            self.assertEqual(self.request('POST','/runs',body)[0],400)

    def test_malformed_absolute_uri_is_a_controlled_error(self):
        import socket
        with socket.create_connection(self.server.server_address,timeout=5) as sock:
            sock.sendall(('GET http://[/api/production-inversion/health HTTP/1.0\r\nHost: 127.0.0.1:'+str(self.server.server_port)+'\r\nAuthorization: Bearer '+self.token+'\r\n\r\n').encode())
            response=http.client.HTTPResponse(sock)
            response.begin(); response.read()
            self.assertEqual(response.status,400)

    def test_points_can_page_every_bounded_result_block(self):
        from backend.inversion.routes import query
        self.assertEqual(query('offset=131071&limit=1','results.points'),{'offset':131071,'limit':1})
        from types import SimpleNamespace
        from backend.inversion.queries import point_page
        page=point_page(SimpleNamespace(id='result',content_sha256='a'*64,chunks=[]),0,1)
        self.assertEqual((page['result_id'],page['content_sha256']),('result','a'*64))

    def test_recovery_route_requires_exact_public_action(self):
        from backend.inversion.routes import route,query
        from uuid import uuid4
        self.assertEqual(route('GET',PREFIX+'/requests/'+str(uuid4()))[0],'requests.get')
        self.assertEqual(query('action=runs.create','requests.get'),{'action':'runs.create'})

    def test_audit_filter_does_not_collide_with_dispatch_argument(self):
        from backend.inversion.queries import InversionQueries
        from unittest.mock import MagicMock
        conn=MagicMock(); conn.__enter__.return_value=conn
        conn.execute.return_value.fetchall.return_value=[]
        result=InversionQueries(lambda:conn).fetch('audit',action='runs.create')
        self.assertEqual(result['items'],[])

    def test_recovery_refuses_ambiguous_or_wrong_subject_audit(self):
        from backend.inversion.queries import InversionQueries
        from backend.inversion.transactions import InversionError
        from backend.waveforms.commands import Actor
        from unittest.mock import MagicMock
        from uuid import uuid4
        subject=uuid4(); conn=MagicMock(); conn.__enter__.return_value=conn
        conn.execute.return_value.fetchone.return_value=(subject,1,'a'*64)
        actor=Actor('platform-local','1','operator','administrator')
        for rows in ([],[('runs.create',uuid4())],[('runs.create',subject)]*2):
            conn.execute.return_value.fetchall.return_value=rows
            with self.assertRaises(InversionError) as error:
                InversionQueries(lambda:conn).recover(uuid4(),'runs.create',actor)
            self.assertEqual(error.exception.code,'inversion.request_binding_conflict')

    def test_export_recovery_checks_intent_capability_before_lookup(self):
        from backend.inversion.runtime import InversionRuntime
        from unittest.mock import MagicMock
        from uuid import uuid4
        queries=MagicMock(); queries.recover.return_value={'state':'unknown'}
        runtime=InversionRuntime('ready',queries=queries)
        runtime.allowed_origins=self.server.inversion_runtime.allowed_origins
        self.server.inversion_runtime=runtime
        with self.server.database.transaction(None,'test.revoke','role',None) as conn:
            conn.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='administrator') AND permission_id=(SELECT id FROM permissions WHERE code='inversion.audit.read')")
        suffix='/requests/'+str(uuid4())+'?action=exports.create'
        self.assertEqual(self.request('GET',suffix+'&intent=evidence')[0],403)
        queries.recover.assert_not_called()
        queries.recover.return_value={'state':'committed','subject_id':str(uuid4()),'version':1,'content_sha256':'a'*64}
        status,recovered,_=self.request('GET',suffix+'&intent=quantitative')
        self.assertEqual(status,200); self.assertEqual(recovered['data']['state'],'committed')
        self.assertEqual(self.request('GET',suffix)[0],400)
        self.assertEqual(self.request('GET',suffix.replace('exports.create','runs.create')+'&intent=evidence')[0],400)


from tests.inversion_worker import RunCase


from tests.inversion_http_support import HttpRuntimeMixin


class LiveApiTests(HttpRuntimeMixin, RunCase):
    """Pinned PostgreSQL fixture plus genuine SQLite session at HTTP and worker."""
    def setUp(self):
        super().setUp()
        self.start_http()

    def test_attachment_rejection_is_definite_noncommit_and_correctable(self):
        from copy import deepcopy
        from uuid import uuid4
        original=self.assembly_body()
        before=tuple(self.scalar('SELECT count(*) FROM inversion.'+table) for table in ('assemblies','audit','receipts'))
        for missing in (False,True):
            bad=deepcopy(original)
            bad['installation_evidence'][0]['content_sha256']='0'*64
            if missing: bad['installation_evidence'][0]['id']=str(uuid4())
            envelope=self.envelope(bad)
            status,response=self.http('POST','/assemblies',envelope)
            self.assertEqual((status,response['code']),(409,'inversion.attachment_mismatch'))
            self.assertEqual(response['details'],dict(commit_outcome='not_committed',action='assemblies.create'))
            self.assertEqual(tuple(self.scalar('SELECT count(*) FROM inversion.'+table) for table in ('assemblies','audit','receipts')),before)
            recovery='/requests/'+envelope['command']['key']+'?action=assemblies.create'
            self.assertEqual(self.http('GET',recovery)[1]['data'],{'state':'unknown'})
        status,response=self.http('POST','/assemblies',self.envelope(original))
        self.assertEqual(status,201,response)
        self.assertEqual(response['data']['subject_id'],original['assembly_id'])

    def test_models_malformed_scalar_is_400_and_uninstalled_string_is_409(self):
        before=self.scalar('SELECT count(*) FROM inversion.model_versions')
        for value in (None,True,1,[],{},'', 'a'*129):
            self.assertEqual(self.http('POST','/models',self.envelope({'installed_id':value}))[0],400)
        status,response=self.http('POST','/models',self.envelope({'installed_id':'not_installed/1'}))
        self.assertEqual((status,response['code']),(409,'inversion.model_not_installed'))
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.model_versions'),before)

    def test_blocked_run_detail_exposes_exact_durable_causes_without_result(self):
        from dataclasses import replace
        from uuid import UUID,uuid4
        from backend.inversion.runtime import worker_services
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.result_codec import encode
        from backend.inversion.comparison import compare
        from tests.test_inversion_comparison import prediction
        from tests.inversion_governance_support import command,REVIEWER
        worker=worker_services(canonical_bytes({'config_path':str(self.runtime_path)}))
        for cause in ('not_applicable','input_changed'):
            status,created=self.http('POST','/runs',self.envelope(self.case['request']))
            self.assertEqual(status,202,created)
            attempt=worker.runs.claim(uuid4()); run=worker.runs.get(attempt.run_id)
            authority=worker.runs._authority(run.authority,run.actor)
            acquired=worker.acquisition.prepare_input(run.request,run.actor,check=lambda:None)
            pred=prediction(); prepared=encode(pred,compare(pred,acquired.reference_inputs,acquired.policy),acquired.snapshot)
            if cause=='not_applicable':
                body=prepared.body; body.update(prediction_status='not_applicable',prediction_reason_codes=['insufficient_coverage'],coverage_fraction='1/3',valid_point_count=2)
                prepared=replace(prepared,metadata=canonical_bytes(body))
            else: self.parameters.revoke(self.request.parameter_id,command(REVIEWER,3))
            self.assertIsNone(worker.runs.publish(attempt,prepared,authority))
            terminal=worker.runs.get(run.id)
            original_event=self.scalar('SELECT body FROM inversion.run_events WHERE run_id=%s AND version=%s',(run.id,terminal.version))
            status,response=self.http('GET','/runs/'+str(run.id)); self.assertEqual(status,200,response)
            detail=response['data']; diagnostic=detail['terminal_diagnostics']
            self.assertEqual((detail['state'],detail['reason_code'],detail['result_id']),('blocked',cause,None))
            self.assertEqual((diagnostic['run_id'],diagnostic['version'],diagnostic['state'],diagnostic['reason_code']),
                (str(run.id),terminal.version,'blocked',cause))
            self.assertEqual(diagnostic['reasons'],['insufficient_coverage'] if cause=='not_applicable' else ['parameter_pin_changed'])
            if cause=='not_applicable': self.assertEqual((diagnostic['coverage_fraction'],diagnostic['point_count'],diagnostic['valid_point_count']),('1/3',3,2))
            self.assertEqual(self.http('GET','/runs/'+str(run.id))[1]['data'],detail)
            self.assertEqual(self.scalar('SELECT body FROM inversion.run_events WHERE run_id=%s AND version=%s',(run.id,terminal.version)),original_event)
            self.assertIsNone(self.server.inversion_runtime.queries.run_diagnostics(replace(terminal,version=terminal.version-1)))
            self.assertNotIn('authority',json.dumps(detail)); self.assertNotIn('session',json.dumps(detail)); self.assertNotIn(str(self.runtime_path),json.dumps(detail))
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),0)
        with self.server.database.transaction(None,'test.revoke','role',None) as conn:
            conn.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='administrator') AND permission_id=(SELECT id FROM permissions WHERE code='inversion.read')")
        with patch.object(self.server.inversion_runtime.queries,'run_diagnostics',side_effect=AssertionError('Read must authorize first')):
            self.assertEqual(self.http('GET','/runs/'+str(run.id))[0],403)

    def test_filtered_parameters_latest_lineage_and_assembly_isolation(self):
        from backend.inversion.contracts import canonical_bytes, strict_json
        from tests.inversion_governance_support import command
        assembly=self.case['request']['assembly_id']
        original=self.case['parameters']
        body=strict_json(canonical_bytes(original.body))
        body['parent_version']=original.version
        body['geometry']['pivot_x_m']='2.25'
        created=self.parameters.create_version(body,command(version=original.version))
        other=self.create_assembly()[0].subject_id
        other_parameter=self.parameters.create_version(self.body(other),command())
        for subject,want in ((assembly,[created.subject_id,original.id]),
                             (str(other),[other_parameter.subject_id])):
            path='/parameters?assembly_id='+subject
            status,payload=self.http('GET',path+'&offset=0&limit=1')
            self.assertEqual(status,200,payload)
            self.assertEqual(payload['data']['items'][0]['id'],str(want[0]))
            self.assertEqual(payload['data']['items'][0]['version'],len(want))
            status,next_page=self.http('GET',path+'&offset=1&limit=1')
            self.assertEqual(status,200,next_page)
            self.assertEqual([item['id'] for item in next_page['data']['items']],[str(item) for item in want[1:]])
        empty=self.create_assembly()[0].subject_id
        self.assertEqual(self.http('GET','/parameters?assembly_id='+str(empty)+'&limit=1')[1]['data']['items'],[])
        self.assertEqual((self.scalar('SELECT queued FROM inversion.capacity'),
                          self.scalar('SELECT running FROM inversion.capacity'),
                          self.scalar('SELECT reserved_bytes FROM inversion.capacity')),(0,0,0))

    def test_engineer_reads_exact_selection_pins_and_mechanical_history(self):
        from uuid import uuid4
        from hashlib import sha256
        from datetime import datetime
        from backend.inversion.contracts import canonical_bytes
        from tests.inversion_governance_support import command,MID
        # Existing role and a real current session, never models.manage.
        engineer=self.server.auth.login('production_engineer','Production@2026')
        with self.server.database.transaction(None,'test.waveform_grants','role',None) as conn:
            for capability in ('waveform.read','waveform.raw.read','waveform.export'):
                conn.execute("INSERT OR IGNORE INTO role_permissions(role_id,permission_id,source_type) SELECT r.id,p.id,'system' FROM roles r CROSS JOIN permissions p WHERE r.code='production_engineer' AND p.code=?",(capability,))
        current=self.server.auth.me(engineer.token)
        self.assertNotIn('inversion.models.manage',current.permissions)
        assembly=self.case['request']['assembly_id']
        selection_path='/assemblies/'+assembly+'/selections'
        status,payload=self.http('GET',selection_path+'?limit=1',token=engineer.token)
        self.assertEqual(status,200,payload)
        selected=payload['data']['items'][0]
        self.assertEqual((payload['data']['limit'],len(payload['data']['items'])),(1,1))
        pins=self.case['request'].copy(); pins['selection_version']=selected['version']
        for key in ('assembly_id','parameter_id','parameter_sha256','parameter_review_version','model_id','model_sha256','model_review_version'):
            self.assertEqual(selected['body'][key],self.case['request'][key]); pins[key]=selected['body'][key]
        status,preflight=self.http('POST','/runs/preflight',{'body':pins},token=engineer.token)
        self.assertEqual(status,200,preflight); self.assertEqual(preflight['data']['missing'],[])
        for path in ('/selections','/selections/'+selected['id']+'/rollback'):
            self.assertEqual(self.http('POST',path,{},token=engineer.token)[0],403)
        self.assertEqual(self.http('GET',selection_path+'?offset=1&limit=1',token=engineer.token)[1]['data']['items'],[])
        for suffix in ('?limit=101','?limit=1&limit=2','?unknown=1'):
            self.assertEqual(self.http('GET',selection_path+suffix,token=engineer.token)[0],400)
        # Separate genuine service-created physical lineage; no synthetic result inserts.
        receipt,original=self.create_assembly(source_organization='Known installation')
        successor=original | dict(parent_version=1,valid_from=MID,asset_number='TEST-002')
        self.parameters.create_assembly(successor,command(version=1))
        path='/assemblies/'+str(receipt.subject_id)+'/versions'
        status,history=self.http('GET',path,token=engineer.token)
        self.assertEqual(status,200,history)
        data=history['data']; self.assertEqual((data['assembly_id'],data['offset'],data['limit']),(str(receipt.subject_id),0,50))
        self.assertEqual([row['version'] for row in data['items']],[2,1])
        latest,old=data['items']
        self.assertEqual(old['declared'],dict(interval_known=True,valid_from=original['valid_from'],valid_to=None,body=original))
        self.assertEqual(old['content_sha256'],sha256(canonical_bytes(original)).hexdigest())
        self.assertEqual(old['effective']['revision'],2)
        self.assertEqual(datetime.fromisoformat(old['effective']['valid_to']),datetime.fromisoformat(MID))
        self.assertEqual(latest['effective']['revision'],1); self.assertIsNone(latest['effective']['valid_to'])
        self.assertEqual(latest['declared']['body'],successor)
        self.assertNotIn('actor',json.dumps(history)); self.assertNotIn('authority',json.dumps(history))
        page=self.http('GET',path+'?offset=1&limit=1',token=engineer.token)[1]['data']
        self.assertEqual([row['version'] for row in page['items']],[1])
        self.assertEqual(self.http('GET',path+'?offset=100000',token=engineer.token)[1]['data']['items'],[])
        self.assertEqual(self.http('GET','/assemblies/'+str(uuid4())+'/versions?offset=100000',token=engineer.token)[0],404)
        legacy=self.inversion_fixture.assembly()
        unknown=self.http('GET','/assemblies/'+str(legacy)+'/versions',token=engineer.token)[1]['data']['items'][0]
        self.assertEqual(unknown['declared'],dict(interval_known=False,valid_from=None,valid_to=None,body={}))
        self.assertIsNone(unknown['effective'])
        self.assertEqual((self.scalar('SELECT queued FROM inversion.capacity'),self.scalar('SELECT running FROM inversion.capacity'),self.scalar('SELECT reserved_bytes FROM inversion.capacity')),(0,0,0))

    def test_preflight_is_not_calculation(self):
        before=self.scalar('SELECT count(*) FROM waveforms.audit_metadata')
        status,payload=self.http('POST','/runs/preflight',{'body':self.case['request']})
        self.assertEqual(status,200,payload); self.assertEqual(payload['data']['missing'],[])
        self.assertEqual((self.scalar('SELECT count(*) FROM inversion.run_requests'),self.scalar('SELECT count(*) FROM waveforms.audit_metadata')-before,self.scalar('SELECT running FROM inversion.capacity')),(0,0,0))
        for path in ('/assemblies','/parameters','/models','/runs','/audit'):
            status,payload=self.http('GET',path); self.assertEqual(status,200,payload)
        self.assertEqual(self.http('GET','/runs?limit=101')[0],400)
        self.assertEqual(self.http('GET','/results/'+str(self.request.parameter_id)+'/points?limit=1001')[0],400)
        from backend.inversion.runtime import verify_configuration, connections
        from backend.inversion.config import ConfigurationError
        factories=connections(self.server.inversion_runtime.config)
        class OtherDatabaseIdentity:
            def __init__(self): self.conn=factories[3]()
            def __enter__(self): self.conn.__enter__(); return self
            def __exit__(self,*args): return self.conn.__exit__(*args)
            def __getattr__(self,key): return getattr(self.conn,key)
            def execute(self,statement,*args):
                result=self.conn.execute(statement,*args)
                if isinstance(statement,str) and statement.startswith('SELECT current_user,current_database()'):
                    row=result.fetchone()
                    class Altered:
                        def fetchone(self): return (*row[:2],row[2]+1,*row[3:])
                    return Altered()
                return result
        with patch('backend.inversion.runtime.connections',return_value=(*factories[:3],OtherDatabaseIdentity)):
            with self.assertRaises(ConfigurationError): verify_configuration(self.server.inversion_runtime.config,self.server.auth)

    def test_recovery_is_actor_scoped_current_authorized_and_read_only(self):
        from uuid import uuid4
        from backend.inversion.queries import InversionQueries
        envelope=self.envelope(self.case['request']); key=envelope['command']['key']
        suffix='/requests/'+key+'?action=runs.create'
        status,unknown=self.http('GET',suffix)
        self.assertEqual(status,200,unknown); self.assertEqual(unknown['data'],{'state':'unknown'})
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.run_requests'),0)
        status,created=self.http('POST','/runs',envelope)
        self.assertEqual(status,202,created)
        status,recovered=self.http('GET',suffix)
        self.assertEqual(status,200,recovered)
        self.assertEqual(recovered['data']['subject_id'],created['data']['run_id'])
        self.assertEqual(recovered['data']['state'],'committed')
        self.assertEqual(recovered['data']['run']['run_id'],created['data']['run_id'])
        self.assertNotIn('body_matches',recovered['data']); self.assertNotIn('authority',json.dumps(recovered))
        other=self.server.auth.login('algorithm_engineer','Algorithm@2026')
        self.assertEqual(self.http('GET',suffix,token=other.token)[1]['data'],{'state':'unknown'})
        self.assertEqual(self.http('GET','/requests/'+key+'?action=parameters.create')[0],409)
        self.assertEqual(self.http('GET','/requests/'+key+'?action=runs.claim')[0],400)
        changed=json.loads(json.dumps(envelope)); changed['command']['reason']='Different original command'
        self.assertEqual(self.http('POST','/runs',changed)[0],409)
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.run_requests'),1)
        self.assertEqual(self.http('GET','/audit?action=runs.create')[0],200)
        with self.server.database.transaction(None,'test.revoke','role',None) as conn:
            conn.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='administrator') AND permission_id=(SELECT id FROM permissions WHERE code='waveform.raw.read')")
        with patch.object(InversionQueries,'recover',side_effect=AssertionError('Lookup must not precede auth')) as lookup:
            self.assertEqual(self.http('GET',suffix)[0],403); lookup.assert_not_called()

    def test_authority_fresh_replay_claim_and_no_bearer_in_durable_request(self):
        from backend.inversion.runtime import worker_services
        from backend.inversion.contracts import canonical_bytes
        from uuid import UUID,uuid4
        envelope=self.envelope(self.case['request'])
        status,created=self.http('POST','/runs',envelope)
        self.assertEqual(status,202,created)
        id=created['data']['run_id']
        self.assertNotIn('authority',created['data']); self.assertNotIn('actor',created['data'])
        self.assertEqual(self.http('POST','/runs',envelope)[1]['data']['run_id'],id)
        stored=self.scalar('SELECT body::text FROM inversion.run_requests WHERE id=%s',(UUID(id),))
        self.assertNotIn(self.session.token,stored); self.assertNotIn('token',stored)
        worker=worker_services(canonical_bytes({'config_path':str(self.runtime_path)}))
        self.server.auth.logout(self.session.token)
        self.assertEqual(self.http('POST','/runs',envelope)[0],401)
        self.assertIsNone(worker.runs.claim(uuid4()))
        self.assertEqual(worker.runs.get(UUID(id)).state,'blocked')

    def test_publication_and_download_recheck_real_session(self):
        from backend.inversion.runtime import worker_services
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.result_codec import encode
        from backend.inversion.comparison import compare
        from tests.test_inversion_comparison import prediction
        from backend.inversion.transactions import InversionError
        from uuid import UUID,uuid4
        runtime=self.server.inversion_runtime
        status,created=self.http('POST','/runs',self.envelope(self.case['request']))
        self.assertEqual(status,202,created)
        worker=worker_services(canonical_bytes({'config_path':str(self.runtime_path)}))
        attempt=worker.runs.claim(uuid4()); run=worker.runs.get(attempt.run_id)
        authority=worker.runs._authority(run.authority,run.actor)
        prepared=worker.acquisition.prepare_input(run.request,run.actor,check=lambda:None)
        series=prediction(); result=encode(series,compare(series,prepared.reference_inputs,prepared.policy),prepared.snapshot)
        published=worker.runs.publish(attempt,result,authority)
        self.assertIsNotNone(published)
        status,view=self.http('GET','/results/'+str(published.id)); self.assertEqual(status,200,view)
        self.assertNotIn('input_snapshot_metadata',view['data'])
        status,metadata_view=self.http('GET','/results/'+str(published.id)+'?include_input_metadata=true')
        self.assertEqual(status,200,metadata_view)
        metadata=metadata_view['data']['input_snapshot_metadata']
        self.assertEqual(metadata['result_id'],str(published.id))
        self.assertEqual(metadata['content_sha256'],view['data']['content_sha256'])
        self.assertEqual(metadata['snapshot_sha256'],prepared.snapshot.content_sha256)
        self.assertEqual(metadata['observation']['relative_alignment_ns'],prepared.snapshot.body['observation']['relative_alignment_ns'])
        self.assertNotIn('samples',metadata)
        self.assertNotIn('session',json.dumps(view)); self.assertNotIn('authority',json.dumps(view))
        status,points=self.http('GET','/results/'+str(published.id)+'/points?offset=0&limit=2')
        self.assertEqual(status,200,points); self.assertEqual(len(points['data']['rows']),2)
        self.assertEqual((points['data']['result_id'],points['data']['content_sha256']),(view['data']['id'],view['data']['content_sha256']))
        status,comparison=self.http('GET','/results/'+str(published.id)+'/comparison')
        self.assertEqual(status,200,comparison)
        self.assertEqual((comparison['data']['result_id'],comparison['data']['content_sha256']),(view['data']['id'],view['data']['content_sha256']))
        self.assertEqual(self.http('POST','/results/'+str(published.id)+'/review',self.envelope({'decision':'approve'}))[0],409)
        export_command=self.envelope({'result_id':str(published.id),'intent':'evidence','format':'json'})
        status,export=self.http('POST','/exports',export_command)
        self.assertEqual(status,201,export)
        recovery='/requests/'+export_command['command']['key']+'?action=exports.create&intent='
        self.assertEqual(self.http('GET',recovery+'quantitative')[0],409)
        status,recovered=self.http('GET',recovery+'evidence')
        self.assertEqual(status,200,recovered); self.assertEqual(recovered['data']['subject_id'],export['data']['id'])
        id=export['data']['id']; status,payload=self.http('GET','/exports/'+id)
        self.assertEqual(status,200,payload)
        self.assertNotIn(self.session.token,json.dumps(payload))
        # A previously valid claim snapshot is not publication authorization.
        status,second=self.http('POST','/runs',self.envelope(self.case['request']))
        self.assertEqual(status,202,second)
        second_attempt=worker.runs.claim(uuid4())
        from backend.inversion.exports import ResultExporter
        original_open=ResultExporter.open
        def revoke_at_byte_start(exporter,*args,**kwargs):
            context=original_open(exporter,*args,**kwargs)
            self.server.auth.logout(self.session.token)
            return context
        with patch.object(ResultExporter,'open',revoke_at_byte_start):
            self.assertEqual(self.http('GET','/exports/'+id)[0],401)
        self.assertEqual(self.http('GET','/exports/'+id)[0],401)
        with self.assertRaises(InversionError): worker.runs.publish(second_attempt,result,authority)
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.results'),1)

    def test_cancel_after_raw_permission_removed_and_other_actor_refused(self):
        status,created=self.http('POST','/runs',self.envelope(self.case['request']))
        self.assertEqual(status,202,created)
        id=created['data']['run_id']
        other=self.server.auth.login('production_engineer','Production@2026')
        self.assertEqual(self.http('POST','/runs/'+id+'/cancel',self.envelope(version=1),token=other.token)[0],403)
        with self.server.database.transaction(None,'test.promote','user',None) as conn:
            conn.execute("UPDATE users SET role_id=(SELECT id FROM roles WHERE code='administrator'),version=version+1 WHERE id=?",(other.user_id,))
        runtime=self.server.inversion_runtime; original_services=runtime.services_for
        def demote_before_cancel(ref):
            services=original_services(ref)
            with self.server.database.transaction(None,'test.demote','user',None) as conn:
                conn.execute("UPDATE users SET role_id=(SELECT id FROM roles WHERE code='production_engineer'),version=version+1 WHERE id=?",(other.user_id,))
            return services
        with patch.object(runtime,'services_for',demote_before_cancel):
            self.assertEqual(self.http('POST','/runs/'+id+'/cancel',self.envelope(version=1),token=other.token)[0],403)
        with self.server.database.transaction(None,'test.revoke','role',None) as conn:
            conn.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='administrator') AND permission_id=(SELECT id FROM permissions WHERE code='waveform.raw.read')")
        status,cancelled=self.http('POST','/runs/'+id+'/cancel',self.envelope(version=1))
        self.assertEqual(status,200,cancelled)
        self.assertEqual(cancelled['data']['state'],'cancelled')
