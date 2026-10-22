"""Pure wire/ownership tests: no database fixture and no service writes."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from tools.testpack.lab_http import LabHttp, LabHttpError
from tools.testpack.lab import DEFAULT_PORTS, validate_lab_ports
from backend.inversion.result_codec import POINT_COLUMNS


def api_module(test):
    try:
        return importlib.import_module('tools.testpack.inversion_http_v2')
    except ModuleNotFoundError:
        test.fail('bounded inversion HTTP session is not implemented')


class ScriptHttp:
    """An exact ordered transport double; no governance or DB impersonation."""
    def __init__(self, steps):
        self.steps=list(steps); self.calls=[]

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method,path,deepcopy(body),deepcopy(kwargs)))
        expected_method,expected_path,reply=self.steps.pop(0)
        if (method,path)!=(expected_method,expected_path):
            raise AssertionError((method,path,expected_method,expected_path))
        if isinstance(reply,Exception):raise reply
        return deepcopy(reply)


class InversionHttpTests(unittest.TestCase):
    def test_namespace_error_codes(self):
        client=object.__new__(LabHttp);client.port=19999;client.sessions={'author':{}}
        for raw,expected in (({'code':'inversion.self_review','details':{}},'inversion.self_review'),
                             ({'error':{'code':'waveform.review_conflict'}},'waveform.review_conflict')):
            with patch.object(client,'_send',return_value=(409,json.dumps(raw).encode(),'')):
                with self.assertRaises(LabHttpError) as caught:
                    client.request('POST','/api/production-inversion/models',{},actor='author')
                self.assertEqual(caught.exception.code,expected)
                self.assertEqual(caught.exception.status,409)

    def test_append_parameter_expected_version(self):
        module=api_module(self);key=str(uuid4())
        http=ScriptHttp([('POST','/api/production-inversion/parameter-versions',{'ok':True,'data':{'version':4}})])
        session=module.InversionSession(http)
        reply=session.post('/parameter-versions',{'schema':'inversion.parameter-set/2'},actor='algorithm_engineer',expected_version=3,key=key,expected_status=201)
        self.assertEqual(reply,{'version':4})
        self.assertEqual(http.calls[0][2]['command']['expected'],{'kind':'match','version':3})
        self.assertEqual(http.calls[0][2]['command']['key'],key)
        self.assertNotIn('idempotency_key',http.calls[0][2])

    def test_unknown_outcome_reuses_key_and_body(self):
        module=api_module(self);key=str(uuid4());subject=str(uuid4())
        path='/api/production-inversion/models'
        http=ScriptHttp([('POST',path,TimeoutError('response lost')),
            ('GET',f'/api/production-inversion/requests/{key}?action=models.create',
             {'ok':True,'data':{'state':'committed','subject_id':subject}}),
            ('POST',path,{'ok':True,'data':{'subject_id':subject,'version':1,'replayed':True}})])
        session=module.InversionSession(http)
        body={'installed_id':'beam_energy_balance/2'}
        reply=session.post('/models',body,actor='algorithm_engineer',expected_version=None,key=key,expected_status=201)
        self.assertEqual(reply['subject_id'],subject)
        self.assertEqual(http.calls[0][2],http.calls[2][2])
        self.assertEqual(http.calls[1][3]['actor'],'algorithm_engineer')
        self.assertEqual(http.calls[2][3]['expected'],(200,201))
        self.assertEqual(body,{'installed_id':'beam_energy_balance/2'})

    def test_unavailable_response_keeps_a_durable_same_key_intent(self):
        module=api_module(self);key=str(uuid4())
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            http=ScriptHttp([('POST','/api/production-inversion/models',LabHttpError(503,'inversion.unavailable')),
                ('GET',f'/api/production-inversion/requests/{key}?action=models.create',{'data':{'state':'unknown'}}),
                ('POST','/api/production-inversion/models',{'data':{'subject_id':str(uuid4()),'version':2,'replayed':False}})])
            try:session=module.InversionSession(http,journal_root=Path(folder))
            except TypeError:self.fail('durable command intent unavailable')
            session.post('/models',{'installed_id':'beam_energy_balance/2'},actor='algorithm_engineer',expected_version=None,key=key,expected_status=201)
            saved=json.loads((Path(folder)/(key+'.json')).read_bytes())
            self.assertEqual(saved['envelope'],http.calls[0][2]);self.assertEqual(saved['action'],'models.create')
            self.assertEqual(http.calls[0][2],http.calls[2][2])

    def test_two_pages_exactly_1025(self):
        module=api_module(self);result_id=str(uuid4());digest='a'*64
        def page(offset,size):
            values=[]
            for i in range(offset,offset+size):
                row=[None]*len(POINT_COLUMNS)
                row[POINT_COLUMNS.index('power_index')]=str(i)
                row[POINT_COLUMNS.index('time_ns')]=str(1790784000000000000+i*7812500)
                values.append({'kind':'prediction','columns':list(POINT_COLUMNS),'values':row})
            return {'ok':True,'data':{'result_id':result_id,'content_sha256':digest,'offset':offset,'limit':1000,'total':1025,'rows':values}}
        prefix=f'/api/production-inversion/results/{result_id}'
        def client(second=None):
            return ScriptHttp([('GET',prefix,{'ok':True,'data':{'id':result_id,'content_sha256':digest}}),
                ('GET',prefix+'/points?kind=prediction&offset=0&limit=1000',page(0,1000)),
                ('GET',prefix+'/points?kind=prediction&offset=1000&limit=1000',second or page(1000,25))])
        points=module.InversionSession(client()).read_all_points(result_id)
        self.assertEqual(len(points),1025)
        self.assertEqual({p['power_index'] for p in points},{str(i) for i in range(1025)})
        self.assertEqual(points[-1]['time_ns'],'1790784008000000000')
        for mutate in ('sha','total','offset','duplicate','short','column'):
            bad=page(1000,25)
            if mutate=='sha':bad['data']['content_sha256']='b'*64
            if mutate=='total':bad['data']['total']=1024
            if mutate=='offset':bad['data']['offset']=999
            if mutate=='duplicate':bad['data']['rows'][0]['values'][POINT_COLUMNS.index('power_index')]='999'
            if mutate=='short':bad['data']['rows'].pop()
            if mutate=='column':bad['data']['rows'][0]['columns'][0]='forged'
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):
                module.InversionSession(client(bad)).read_all_points(result_id)

    def test_negative_not_approved_success(self):
        module=api_module(self)
        outcome=module.InversionOutcome(case_id='v2-noise',assertion_status='passed',
            expected_outcome='refused',observed_outcome='refused',phase='budget',reason_code='insufficient_coverage',
            approved_execution_status='refused',facts={})
        self.assertEqual(outcome.assertion_status,'passed')
        self.assertEqual(outcome.approved_execution_status,'refused')
        with self.assertRaises(ValueError):
            module.InversionOutcome(case_id='v2-noise',assertion_status='passed',expected_outcome='refused',
                observed_outcome='refused',phase='budget',reason_code='insufficient_coverage',
                approved_execution_status='passed',facts={})

    def test_wrong_lab_pack_process_identity(self):
        try:module=importlib.import_module('tools.testpack.owned_v2_verify')
        except ModuleNotFoundError:self.fail('owned v2 verification entry point is absent')
        lab=SimpleNamespace(root=Path('C:/owned'),review_root=Path('C:/owned/review'),monitoring_root=Path('C:/owned/monitoring'))
        pack=SimpleNamespace(root=Path('C:/pack'))
        with patch.object(module,'checked_pack',side_effect=ValueError('wrong pack')):
            with self.assertRaises(ValueError):module.verify_owned_runtime(lab,pack)
        expected={'pid':17,'created':'owned','executable':'python.exe','command':'worker-owned'}
        marker={'state':'running','processes':{'http':expected,'inversion_worker':expected}}
        with patch.object(module,'checked_pack',return_value=pack),patch.object(module,'component_marker',return_value=marker),patch.object(module,'inspect_pid',return_value=expected|{'created':'reused'}):
            with self.assertRaises(ValueError):module.verify_owned_runtime(lab,pack)

    def test_reader_ports_are_protected(self):
        for port in (18882,18883):
            ports=deepcopy(DEFAULT_PORTS);ports['review']['http']=port
            with self.assertRaises(ValueError):validate_lab_ports(ports,probe=False)

    def test_owned_runtime_requires_worker_not_just_http(self):
        module=importlib.import_module('tools.testpack.owned_v2_verify')
        lab=SimpleNamespace(monitoring_root=Path('C:/owned/m'),review_root=Path('C:/owned/r'))
        process={'pid':17,'created':'owned','executable':'python.exe','command':'worker-owned'}
        with patch.object(module,'checked_pack',return_value=object()),patch.object(module,'component_marker',return_value={'state':'running','processes':{'http':process}}),patch.object(module,'inspect_pid',return_value=process):
            with self.assertRaises(ValueError):module.verify_owned_runtime(lab,object())

    def test_explicit_live_configuration_cannot_skip(self):
        try:module=importlib.import_module('tools.testpack.owned_v2_verify')
        except ModuleNotFoundError:self.fail('owned live configuration absent')
        with self.assertRaises(ValueError):module.live_paths({'OILWELL_TESTPACK_INVERSION_V2_LIVE':'1'})
        self.assertIsNone(module.live_paths({}))

    def test_explicit_ports_file_is_used_by_cli(self):
        from tools.testpack.__main__ import main
        from tools.testpack.lab import V2_PORTS
        from tempfile import TemporaryDirectory
        from contextlib import redirect_stdout,redirect_stderr
        from io import StringIO
        with TemporaryDirectory() as folder:
            path=Path(folder)/'ports.json';path.write_text(json.dumps(V2_PORTS))
            lab=SimpleNamespace(lab_id='owned',monitoring_root=Path(folder)/'m',review_root=Path(folder)/'r')
            with patch('tools.testpack.__main__.load_pack',return_value=object()),patch('tools.testpack.lab.initialize_lab',return_value=lab) as initialize,patch('tools.review_deploy.launch'),redirect_stdout(StringIO()),redirect_stderr(StringIO()):
                try:status=main(['lab-init','--pack',folder,'--root',folder+'/new','--source',folder,'--ports-file',str(path)])
                except SystemExit as error:status=error.code
                self.assertEqual(status,0,'explicit owned port mapping not accepted')
                self.assertEqual(initialize.call_args.kwargs['ports'],V2_PORTS)

    def test_direct_fixture_rejects_existing_reader_port(self):
        from tools.review_deploy_config import checked_fixture
        from tests.test_testpack_lab import fixture
        for port in (18882,18883):
            with self.assertRaises(ValueError):checked_fixture(fixture()|{'modbus_port':port})

    def test_v2_run_context_fingerprints_actual_v2_runner(self):
        from tools.testpack.lab import run_context
        from tools.testpack.contracts import PackIndex
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'manifest.json').write_bytes(b'{}')
            pack=PackIndex(root,{'pack_id':'tp-'+'a'*24,'configuration':{'schema':'owp.testpack.config/2'}})
            lab=SimpleNamespace(root=root,lab_id=str(uuid4()))
            with patch('tools.testpack.lab.deploy._marker',return_value={'source':{'commit':'b'*40}}):
                context=run_context(lab,pack)
            self.assertEqual(context['runner']['version'],'owp.testpack.generator/2')


class BoundedDownloadTests(unittest.TestCase):
    def test_declared_length_is_checked_against_actual_stream(self):
        from io import BytesIO
        class Response(BytesIO):
            status=200
            def getheader(self,name,default=None):return '9' if name=='Content-Length' else default
        connection=SimpleNamespace(request=lambda *a,**kw:None,getresponse=lambda:Response(b'abc'),close=lambda:None)
        client=object.__new__(LabHttp);client.port=19999
        with patch('tools.testpack.lab_http.http.client.HTTPConnection',return_value=connection):
            with self.assertRaises(ValueError):client._send('GET','/api/production-inversion/exports/'+str(uuid4()),None,{})


class BindingWorkflowTests(unittest.TestCase):
    def workflow(self):
        try:return importlib.import_module('tools.testpack.inversion_run_v2')
        except ModuleNotFoundError:self.fail('v2 waveform and inversion workflow absent')

    def test_channel_pins_use_real_sync_and_producer(self):
        module=self.workflow();capture={'gateway_id':'G','device_id':'D','capture_id':str(uuid4())}
        pin={'id':str(uuid4()),'version':'7','content_sha256':'a'*64}
        selected={'sync':pin,'channels':[dict(role=role,capture=capture,stream_id='native',channel_id=role,
            first_index='0',last_index='1024',sampling_config=pin,calibration=pin) for role in ('active_power','crank_angle','load','displacement')]}
        archive={'manifest_sha256':'b'*64,'manifest':{'streams':[{'source':{'gateway_id':'PRODUCER','device_id':'SENSOR'}}]}}
        pins=module.channel_pins(selected,archive)
        self.assertEqual(pins['active_power']['sync_pin'],{'id':pin['id'],'content_sha256':'a'*64,'review_version':7})
        self.assertEqual(pins['active_power']['producer_endpoint'],{'gateway_id':'PRODUCER','device_id':'SENSOR'})
        self.assertEqual(pins['active_power']['last_index'],1024)

    def test_native_budget_gate_rejects_missing_proof_over_budget_and_invalid_load(self):
        module=self.workflow();check=getattr(module,'check_native_budget',None)
        self.assertTrue(callable(check),'native proof acceptance gate absent')
        from backend.inversion.result_codec_v2 import PROOF_COLUMNS
        prediction=[dict(power_index=str(i),time_ns=str(i),status='valid',reason_codes=[],load_N='40000',s_m='2',error_bound_N='20') for i in range(1025)]
        proof=[dict(power_index=str(i),time_ns=str(i),status='valid',reason_codes=[],motion_status='valid',motion_reason_codes=[],
                    angle_indices=['0','1','2','3','4','5','6'],exact_center=['1','1','0'],base=['0','0','0'],physical=['0','0','0'],numerical=['0','0','0'],
                    **{k:'0' for k in PROOF_COLUMNS[-5:]}) for i in range(1025)]
        body={'source_proof':{'status':'verified','reason_codes':[]}}
        check(body,prediction,proof)
        for mutation in ('source','physical','numerical','base','null','invalid','motion'):
            b,p,q=deepcopy(body),deepcopy(prediction),deepcopy(proof)
            if mutation=='source':b['source_proof']['status']='refused'
            if mutation=='physical':q[22]['physical_N_error_Nm']='101'
            if mutation=='numerical':q[22]['numerical_s_error_m']='1/1000000'
            if mutation=='base':q[22]['base'][1]='1/10000'
            if mutation=='null':p[22]['load_N']=None
            if mutation=='invalid':p[22].update(status='input_gap',reason_codes=['input_gap'])
            if mutation=='motion':q[22]['motion_status']='refused'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):check(b,p,q)

    def test_registered_positives_include_all_forty_extended_variants(self):
        module=self.workflow();select=getattr(module,'positive_case_ids',None)
        self.assertTrue(callable(select),'extended HTTP enumeration absent')
        from backend.inversion.synthetic_registry import installed_config,case_ids
        from backend.inversion.contracts import canonical_bytes
        from hashlib import sha256
        config=installed_config('extended');faults={'cycle-noise','cycle-missing','cycle-clipped','cycle-shifted'}
        records=[{'case_id':cid,'facts':{'data_kind':'cycle'},'category':'signal_fault' if cid in faults else 'mechanistic'}
                 for cid in case_ids(sha256(canonical_bytes(config)).hexdigest())]
        pack=SimpleNamespace(manifest={'configuration':config,'cases':records})
        self.assertEqual(len(select(pack)),44);self.assertIn('cycle-variant-39',select(pack))
        records.pop()
        with self.assertRaises(ValueError):select(pack)

    def test_missing_fault_requires_observed_missing_reason(self):
        module=self.workflow();check=getattr(module,'require_waveform_refusal',None)
        self.assertTrue(callable(check),'exact waveform refusal gate absent')
        facts={'dataset':None,'pair':{'body':{'joint_result':{'status':'ineligible','reasons':['missing_samples']}}}}
        check('cycle-missing',facts)
        facts['pair']['body']['joint_result']['reasons']=['config_mismatch']
        with self.assertRaises(ValueError):check('cycle-missing',facts)

    def test_inversion_request_uses_version_not_dataset_draft_id(self):
        module=self.workflow();ids=[str(uuid4()) for _ in range(6)]
        parameter={'id':ids[0],'content_sha256':'a'*64,'review_version':3,'assembly_id':ids[4]}
        model={'id':ids[1],'content_sha256':'b'*64,'review_version':2}
        pair={'id':ids[2],'content_sha256':'c'*64,'version':'9'}
        dataset={'id':ids[3],'dataset_id':ids[5],'content_sha256':'d'*64}
        actual=module.run_request(parameter,model,pair,dataset,{'version':4})
        self.assertEqual(actual['dataset_version_id'],ids[3]);self.assertEqual(actual['pair_review_version'],9)
        from backend.inversion.input_contracts import parse_run_request
        from backend.inversion.contracts import canonical_bytes
        parsed=parse_run_request(canonical_bytes(actual))
        self.assertEqual(parsed.model_review_version,2);self.assertEqual(parsed.selection_version,4)

    def test_waveform_selection_and_review_use_actual_response_versions(self):
        from tools.testpack.waveform_run import WaveformSession
        from tools.testpack.contracts import Channel
        session=object.__new__(WaveformSession)
        session.evidence=lambda *a:{'id':str(uuid4()),'content_sha256':'a'*64,'version':6}
        pin={'id':str(uuid4()),'content_sha256':'b'*64,'review_version':7}
        binding=SimpleNamespace(sampling={'load':pin},calibration={'load':pin|{'review_version':8}})
        archive={'manifest':{'streams':[{'source':{},'channels':[]}],'cycle':{'cycle_id':str(uuid4())}},
            'capture':{},'provenance':{},'import_id':str(uuid4()),'manifest_sha256':'a'*64,'capture_pk':str(uuid4())}
        cycle=SimpleNamespace(channels=(Channel('load','N','0.01','0',(1,2),(0,0)),))
        selected,_=session.selection(archive,cycle,binding,'2026-09-30T16:00:00.000000Z',{'id':str(uuid4())})
        self.assertEqual(selected['channels'][0]['sampling_config']['version'],'7')
        self.assertEqual(selected['channels'][0]['calibration']['version'],'8')
        pair={'id':str(uuid4()),'version':9,'body':{'joint_result':{'status':'eligible'}},'left_quality_run_id':str(uuid4())}
        calls=[];replies=iter(({},pair,{}, {'id':str(uuid4()),'revision':1},{'id':str(uuid4()),'revision':2},{'id':str(uuid4()),'revision':3},{}))
        def post(*args):calls.append(args);return next(replies)
        session.post=post
        session.pair_and_freeze(archive,selected,'cycle-base')
        self.assertEqual(calls[2][2],9)

    def test_budget_gate_fails_before_creating_any_approval(self):
        module=self.workflow()
        with self.assertRaises(ValueError):module.require_budget({'passed':False,'source_proof_status':'verified','native_points':1025})
        with self.assertRaises(ValueError):module.require_budget({'passed':True,'source_proof_status':'refused','native_points':1025})

    def test_pdf_summary_uses_parsed_parameter_contract(self):
        module=self.workflow()
        from tests.inversion_v2_support import fixture_body
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
        parameter=parse_parameter_set_v2(canonical_bytes(fixture_body()))
        build=getattr(module,'budget_pdf_summary',None)
        self.assertTrue(callable(build),'parsed parameter PDF summary absent')
        summary=build('cycle-base',{'points':[{'private':'do not embed a megabyte'}],'passed':True},parameter)
        self.assertEqual(summary['policy_sha256'],parameter.policy.raw_sha256)
        self.assertEqual(summary['parameter_core_sha256'],parameter.parameter_core_sha256)
        self.assertEqual(summary['contract_payload_sha256'],parameter.coverage_contract.payload_sha256)
        self.assertNotIn('points',summary)

    def test_actual_pdf_renderer_is_not_renamed_image(self):
        from tools.testpack import evidence_report
        from tempfile import TemporaryDirectory
        renderer=getattr(evidence_report,'render_evidence_pdf',None)
        self.assertTrue(callable(renderer),'real PDF renderer absent')
        with TemporaryDirectory() as folder:
            target=Path(folder)/'finite-budget.pdf'
            renderer({'source_kind':'synthetic','parameter_core_sha256':'a'*64,'contract_payload_sha256':'b'*64,
                      'approval':'not_run','field_accuracy_certified':False},target)
            raw=target.read_bytes()
            self.assertTrue(raw.startswith(b'%PDF-'));self.assertIn(b'%%EOF',raw[-1024:])
            self.assertGreater(len(raw),1000);self.assertLessEqual(len(raw),4194304)
            self.assertTrue(target.with_suffix('.hashes.json').is_file())


class GuardWorkflowTests(unittest.TestCase):
    def guards(self):
        try:return importlib.import_module('tools.testpack.inversion_guards_v2')
        except ModuleNotFoundError:self.fail('owned HTTP negative workflow absent')

    def test_wrong_error_cannot_be_recorded_as_expected_refusal(self):
        module=self.guards()
        def refused():raise LabHttpError(409,'inversion.self_review')
        result=module.expect_refusal('self-review','parameter_review',409,'inversion.self_review',refused)
        self.assertEqual(result.assertion_status,'passed');self.assertEqual(result.approved_execution_status,'refused')
        for call in (lambda:None,lambda:(_ for _ in ()).throw(LabHttpError(403,'forbidden'))):
            with self.assertRaises(ValueError):module.expect_refusal('self-review','parameter_review',409,'inversion.self_review',call)

    def test_temporary_role_change_restored_even_when_guard_fails(self):
        module=self.guards()
        before={'id':3,'code':'algorithm_engineer','version':4,'status':'active','permissions':['inversion.read','inversion.run']}
        changed=before|{'version':5,'permissions':['inversion.read','inversion.run','inversion.parameters.review']}
        restored=before|{'version':6}
        http=ScriptHttp([('GET','/api/roles',{'data':[before]}),('PATCH','/api/roles/3',{'data':changed}),
            ('GET','/api/roles/3',{'data':changed}),('PATCH','/api/roles/3',{'data':restored}),('GET','/api/roles/3',{'data':restored})])
        with self.assertRaisesRegex(RuntimeError,'guard failed'):
            with module.temporary_permissions(http,add={'inversion.parameters.review'}):raise RuntimeError('guard failed')
        self.assertEqual(http.calls[3][2],{'version':5,'permissions':before['permissions']})
        self.assertTrue(all(c[3]['actor']=='administrator' for c in http.calls))

    def test_role_patch_is_scoped_not_an_arbitrary_mutation_method(self):
        from tools.testpack.lab_http import validate_request
        try:validate_request('PATCH','/api/roles/3',{})
        except ValueError:self.fail('scoped role test mutation unavailable')
        for path in ('/api/users/3','/api/roles/../3','/api/roles/3/extra','/api/roles/3?all=true'):
            with self.assertRaises(ValueError):validate_request('PATCH',path,{})


if __name__=='__main__':unittest.main()
