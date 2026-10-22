"""Pure governance branches; database transactions remain an integration gate."""
from dataclasses import asdict
from hashlib import sha256
from types import SimpleNamespace as N
from unittest.mock import patch
from uuid import UUID,uuid4
import unittest

from backend.waveforms.commands import Actor
from backend.inversion.contracts import canonical_bytes,InversionCommand,VersionPrecondition,utc
from backend.inversion.parameters import ParameterService,ParameterRecord
from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
from backend.inversion.transactions import InversionError
from backend.inversion.errors import InversionContractError
from tests.inversion_v2_support import fixture_body,declared_fixture

AUTHOR=Actor('local','author','Test author','algorithm_engineer')
REVIEWER=Actor('local','reviewer','Test reviewer','reviewer')


def command(actor=AUTHOR,version=1):
    return InversionCommand(actor,uuid4(),VersionPrecondition('match',version),'Pure governance branch test')


def record(body=None,state='submitted'):
    body=body or fixture_body(); raw=canonical_bytes(body)
    return ParameterRecord(uuid4(),UUID(body['assembly_id']),1,1,sha256(raw).hexdigest(),body,
        asdict(AUTHOR),2 if state=='submitted' else 1,state,
        ({'action':'submit','actor':asdict(AUTHOR)},) if state=='submitted' else (),
        utc(body['valid_from']),utc(body['valid_to']))


def service(saved,authorize=lambda *_:None):
    def forbidden(): raise AssertionError('No database connection permitted')
    item=ParameterService(forbidden,read_connect=forbidden,authorize=authorize,accounting_bucket='test-v2')
    item.get=lambda identifier:saved
    item._get=lambda cur,identifier:saved
    # Only the transaction boundary is doubled; real permission, parsing,
    # geometry, transition, expected-version and self-review checks run.
    item._run=lambda action,target,body,cmd,apply,**kwargs:apply(None,cmd)
    return item


class GovernanceV2Tests(unittest.TestCase):
    def test_create_v2_stores_original_schema_and_full_digest(self):
        body=declared_fixture();saved=record(body,state='draft');item=service(saved);writes=[]
        cur=N(execute=lambda sql,args=():writes.append((sql,args)),fetchone=iter([(1,),(None,)]).__next__)
        item._run=lambda action,target,body,cmd,apply,**kwargs:apply(cur,cmd)
        cmd=InversionCommand(AUTHOR,uuid4(),VersionPrecondition('absent',None),'Create synthetic v2 parameter')
        result=item.create_version(body,cmd)
        inserted=writes[-1][1]
        self.assertEqual(inserted[6].obj['schema'],'inversion.parameter-set/2')
        self.assertEqual(inserted[7],sha256(canonical_bytes(body)).hexdigest())
        self.assertEqual(result.version,1)

    def test_submit_and_independent_approve_attest_contract_not_native_proof(self):
        for action,state,actor,version in (('submit','draft',AUTHOR,1),('approve','submitted',REVIEWER,2)):
            body=declared_fixture();saved=record(body,state=state);item=service(saved);writes=[]
            cur=N(execute=lambda sql,args=():writes.append((sql,args)))
            item._run=lambda action,target,body,cmd,apply,**kwargs:apply(cur,cmd)
            # _validate's real declaration/attachment path is exercised above;
            # this test isolates its persisted review payload and identity.
            item._validate=lambda *_:parse_parameter_set_v2(canonical_bytes(body))
            result=(item.submit(saved.id,command(actor,version)) if action=='submit'
                else item.review(saved.id,action,command(actor,version)))
            attestation=writes[-1][1][-1].obj
            self.assertEqual(result.version,version+1)
            self.assertEqual(attestation['parameter_sha256'],saved.content_sha256)
            self.assertEqual(attestation['parameter_core_sha256'],body['coverage_contract']['binding']['parameter_core_sha256'])
            self.assertEqual(attestation['coverage_contract'],body['coverage_contract'])
            self.assertEqual(attestation['native_source_verification'],'required_at_execution')
            self.assertNotIn('verified',attestation)

    def validate(self,body,alignment=0):
        saved=record(body); item=service(saved)
        cur=N(execute=lambda *_:None,fetchone=lambda:({'source_kind':'synthetic','installation_evidence':[]},saved.valid_from,saved.valid_to))
        # The database-backed evidence service is outside this pure test; retain
        # the actual v2 parser, installed declaration check and attachment calls.
        attachments=[]
        item._attachments=lambda cur,pins,purpose:attachments.append((pins,purpose))
        def evidence(cur,pin,kind,endpoint,source,start,end,observation,uncertainty):
            binding=next(b for b in parse_parameter_set_v2(canonical_bytes(body)).provenance.channel_bindings
                if b.channel_pin.calibration_pin==pin or b.channel_pin.sampling_pin==pin)
            return dict(sampling_config_version_id=str(binding.channel_pin.sampling_pin.id),
                sampling_config_sha256=binding.channel_pin.sampling_pin.content_sha256,
                **{key:'fixture' for key in ('scale','offset','calibrated_range','measurement_uncertainty','time_correction_ns','residual_ns','method','basis')})
        item._evidence=evidence; item._sync_evidence=lambda *_:None
        return item._validate(cur,saved,alignment_ns=alignment),attachments

    def test_complete_registered_declaration_reaches_budget_attachment(self):
        body=declared_fixture()
        try: parsed,attachments=self.validate(body)
        except TypeError: self.fail('v2 governance still expects legacy provenance.coverage')
        self.assertEqual(parsed.schema,'inversion.parameter-set/2')
        budget=body['coverage_contract']['binding']['budget_attachment_pin']
        self.assertTrue(any(purpose=='uncertainty' and any(str(pin['id'])==budget['id'] for pin in pins)
            for pins,purpose in attachments if pins and isinstance(pins[0],dict)))
        self.assertEqual(parsed.parameter_core_sha256,body['coverage_contract']['binding']['parameter_core_sha256'])

    def test_unregistered_or_shrunken_declaration_refused_without_raw_read(self):
        from backend.inversion.coverage_contracts import parameter_core_bytes
        for field,value in (('case_id','unregistered-case'),('omega_max_rad_s','0.7'),('checker_sha256','a'*64)):
            body=declared_fixture();coverage=body['coverage_contract']
            target=coverage['motion'] if field=='omega_max_rad_s' else coverage['binding'] if field=='checker_sha256' else coverage
            target[field]=value
            coverage['binding']['parameter_core_sha256']=sha256(parameter_core_bytes(body)).hexdigest()
            with self.subTest(field=field):
                with self.assertRaises(InversionError) as caught:
                    try: self.validate(body)
                    except TypeError: self.fail('v2 declaration is not governed')
                self.assertEqual(caught.exception.code,'inversion.coverage_basis_mismatch')

    def test_v2_alignment_is_not_invented_from_absolute_utc_uncertainty(self):
        with self.assertRaises(InversionError) as caught:
            try: self.validate(declared_fixture(),alignment=1)
            except TypeError: self.fail('v2 timing admission missing')
        self.assertEqual(caught.exception.code,'inversion.timing_envelope')

    def test_v2_author_cannot_self_review(self):
        saved=record()
        with self.assertRaises(InversionError) as caught:
            try: service(saved).review(saved.id,'approve',command(version=2))
            except InversionContractError: self.fail('Governance cannot parse a complete v2 parameter')
        self.assertEqual(caught.exception.code,'inversion.self_review')

    def test_stale_review_version_refused(self):
        saved=record()
        with self.assertRaises(InversionError) as caught:
            try: service(saved).review(saved.id,'approve',command(REVIEWER,version=1))
            except InversionContractError: self.fail('Governance cannot parse a complete v2 parameter')
        self.assertEqual(caught.exception.code,'inversion.version_conflict')

    def test_missing_permission_stops_before_reads(self):
        saved=record()
        def deny(actor,capability): raise InversionError('forbidden')
        item=service(saved,deny)
        item.get=lambda _:self.fail('Denied actor reached parameter body')
        with self.assertRaises(InversionError) as caught:
            item.review(saved.id,'approve',command(REVIEWER,version=2))
        self.assertEqual(caught.exception.code,'inversion.forbidden')

    def test_missing_contract_bound_cannot_be_submitted(self):
        body=fixture_body();del body['coverage_contract']['motion']['fourth_derivative_max_rad_s4']
        saved=record(body,state='draft')
        from backend.inversion.errors import InversionContractError
        with self.assertRaises(InversionContractError): service(saved).submit(saved.id,command())

    def test_schema_model_mismatch_even_with_matching_algorithm(self):
        from backend.inversion.history import approved_with_cursor
        from backend.inversion.models import ModelService
        saved=record(); parsed=parse_parameter_set_v2(canonical_bytes(saved.body))
        saved=ParameterRecord(**(saved.__dict__|{'review_state':'approved'}))
        model=N(id=uuid4(),installed_id='beam_energy_balance/2',content_sha256='f'*64,review_version=2,
            review_state='approved',body={'artifact':{'algorithm':'beam_energy_balance/2','parameter_schema':'inversion.parameter-set/1'}})
        req=N(assembly_id=saved.assembly_id,parameter_id=saved.id,parameter_sha256=saved.content_sha256,
            parameter_review_version=saved.review_version,model_id=model.id,model_sha256=model.content_sha256,
            model_review_version=2,selection_version=1)
        selected={key:str(value) if isinstance(value,UUID) else value for key,value in req.__dict__.items()}
        selected['policy_sha256']=saved.body['policy']['content_sha256']
        cur=N(execute=lambda *_:None,fetchone=lambda:(uuid4(),1,selected))
        with patch.object(ParameterService,'_get',return_value=saved),patch.object(ParameterService,'_validate',return_value=parsed),\
                patch.object(ModelService,'_get',return_value=model),patch.object(ModelService,'_validate',return_value=None):
            with self.assertRaises(InversionError) as caught:
                approved_with_cursor(cur,req,'test-v2',start=1,end=2,uncertainty=0,alignment=0)
            self.assertEqual(caught.exception.code,'inversion.selection_incompatible')
