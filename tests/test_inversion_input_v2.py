"""Audited native extraction, actual calibration and strict origin mapping."""
from dataclasses import replace
from fractions import Fraction as Q
from hashlib import sha256
from importlib import import_module
import json
import unittest

from backend.inversion.contracts import canonical_bytes
from backend.inversion.transactions import InversionError
from backend.waveforms.errors import WaveformError
from tests.inversion_archive_v2_support import archive_fixture,acquire,EPOCH
from tests.waveform_pairing_support import evidence


class InputV2Tests(unittest.TestCase):
    def module(self):
        try: return import_module('backend.inversion.input_contracts_v2')
        except ModuleNotFoundError: self.fail('v2 archived input contract missing')

    def alter_origin(self,f,change):
        body=json.loads(canonical_bytes(f.origin.body));change(body)
        updated=evidence('origin',body,id=f.origin.id); f.records[str(updated.id)]=updated
        refs=f.selection['captures'][0]['origins']
        refs[0].update(content_sha256=updated.content_sha256)

    def test_actual_archive_to_evidence(self):
        f=archive_fixture(); prepared=acquire(f)
        self.assertTrue(hasattr(prepared,'prediction_evidence'),'v2 acquisition returned legacy input')
        self.assertIs(type(prepared),self.module().PreparedInputV2)
        ev=prepared.prediction_evidence
        self.assertEqual(ev.power.raw,f.raw_values[0])
        self.assertEqual(ev.power.quality,f.qualities[0])
        self.assertEqual(ev.power.raw[400],0)  # missing-quality raw code is retained
        self.assertIsNone(prepared.prediction_inputs.power.samples[400].value_si)
        self.assertEqual(ev.power.pin.manifest_sha256,f.frame.raw_sha256)
        self.assertEqual(ev.power.bound_file_sha256,sha256(f.raw).hexdigest())
        self.assertEqual(ev.power.scale,Q(1,100))
        self.assertEqual(prepared.snapshot.body['schema'],'inversion.input/2')
        mapping=prepared.snapshot.body['source_binding'][0]
        self.assertEqual(mapping['archive_file_sha256'],sha256(f.raw).hexdigest())
        self.assertEqual(mapping['original_identity_basis'],'approved_origin_mapping')
        self.assertEqual(self.module().parse_prediction_evidence(canonical_bytes(prepared.snapshot.body['prediction_evidence'])),ev)
        reads=[i for i,v in enumerate(f.events) if isinstance(v,tuple) and v[0]=='audited_raw']
        self.assertEqual(len(reads),len(f.facts))
        self.assertTrue(all(f.events[i+1]=='commit' for i in reads))

    def test_absolute_time_and_same_cycle_is_not_independent(self):
        f=archive_fixture(); prepared=acquire(f)
        for samples in (prepared.prediction_inputs.power.samples,prepared.prediction_inputs.crank_angle.samples,
                        prepared.reference_inputs.load.samples,prepared.reference_inputs.displacement.samples):
            self.assertEqual(samples[128].time_ns,EPOCH+1_000_000_000)
        self.assertEqual(prepared.prediction_inputs.cycle.start_ns,EPOCH)
        self.assertFalse(prepared.snapshot.body['applicability']['independent_comparison'])
        self.assertIn('calibration_cycle_overlap',prepared.snapshot.body['applicability']['independence_reasons'])

    def test_approved_nonzero_offset_is_retained_not_replaced(self):
        f=archive_fixture(power_offset='1'); prepared=acquire(f)
        self.assertTrue(hasattr(prepared,'prediction_evidence'),'v2 native evidence missing')
        self.assertEqual(prepared.prediction_evidence.power.offset,Q(1))
        self.assertEqual(prepared.prediction_inputs.power.samples[100].value_si,11)
        self.assertEqual(prepared.prediction_evidence.power.raw[100],1000)

    def test_two_capture_offsets_are_aligned_and_utc_added_only_once(self):
        f=archive_fixture(two_capture=True);prepared=acquire(f);ev=prepared.prediction_evidence
        expected=tuple(Q(EPOCH+i*7812500) for i in range(1025))
        self.assertNotEqual(ev.power.pin.capture_id,ev.angle.pin.capture_id)
        self.assertNotEqual(ev.power.bound_file_sha256,ev.angle.bound_file_sha256)
        self.assertEqual(ev.power.time_ns,expected);self.assertEqual(ev.angle.time_ns,expected)
        self.assertEqual(prepared.prediction_inputs.time_bound_ns,0)
        for series in (prepared.prediction_inputs.power,prepared.prediction_inputs.crank_angle,
                prepared.reference_inputs.load,prepared.reference_inputs.displacement):
            self.assertEqual(tuple(s.time_ns for s in series.samples),expected)
        self.assertEqual(prepared.prediction_inputs.cycle.start_ns,EPOCH)
        self.assertEqual(prepared.prediction_inputs.cycle.end_ns,EPOCH+8*10**9)
        self.assertEqual(Q(prepared.snapshot.body['observation']['start_ns']),EPOCH)
        self.assertEqual(Q(prepared.snapshot.body['observation']['end_ns']),EPOCH+8*10**9)

    def test_complete_file_hash_includes_final_lf(self):
        f=archive_fixture();f.descriptor['file_sha256']=sha256(f.raw[:-1]).hexdigest()
        with self.assertRaises((InversionError,WaveformError)): acquire(f)

    def test_missing_mismatched_and_ambiguous_origin_mapping_refused(self):
        for mode in ('missing','bound_mismatch','ambiguous','unapproved'):
            f=archive_fixture()
            if mode=='missing': self.alter_origin(f,lambda b:b.update(verification_basis=None))
            elif mode=='bound_mismatch':
                def change(body):
                    mapping=json.loads(body['verification_basis']);mapping['bound_file_sha256']='f'*64
                    body['verification_basis']=canonical_bytes(mapping).decode()
                self.alter_origin(f,change)
            elif mode=='unapproved':
                f.records[str(f.origin.id)]=replace(f.origin,review_state='revoked')
            else:
                extra=evidence('origin',json.loads(canonical_bytes(f.origin.body)))
                f.records[str(extra.id)]=extra
                ref=dict(id=str(extra.id),content_sha256=extra.content_sha256,version=str(extra.version))
                f.selection['captures'][0]['origins'].append(ref)
                f.member.certificate['refs']=(*f.member.certificate['refs'],ref)
            with self.subTest(mode=mode):
                with self.assertRaises(InversionError) as caught: acquire(f)
                self.assertEqual(caught.exception.code,'inversion.input_origin_mapping')

    def test_field_input_rejected(self):
        with self.assertRaises(InversionError) as caught: acquire(archive_fixture(purpose='field_pair_input'))
        self.assertEqual(caught.exception.code,'inversion.v2_purpose_ineligible')

    def test_client_contract_cannot_replace_archived_codes_or_pins(self):
        from backend.inversion.coverage_contracts import parameter_core_bytes
        from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
        for change in ('raw_sha256','channel_sha256','pin'):
            f=archive_fixture();body=json.loads(f.parameters.raw)
            target=body['coverage_contract']['inputs']['active_power']
            if change=='pin':
                target['pin']['manifest_sha256']='d'*64
                next(b for b in body['provenance']['channel_bindings'] if b['role']=='active_power')['channel_pin']['manifest_sha256']='d'*64
            else: target[change]='d'*64
            body['coverage_contract']['binding']['parameter_core_sha256']=sha256(parameter_core_bytes(body)).hexdigest()
            f.parameters=parse_parameter_set_v2(canonical_bytes(body))
            with self.subTest(change=change),self.assertRaises(InversionError) as caught: acquire(f)
            self.assertEqual(caught.exception.code,'inversion.input_evidence_mismatch')

    def test_evidence_wire_unknown_fields_and_changed_raw_are_refused(self):
        from backend.inversion.errors import InversionContractError
        f=archive_fixture();ev=acquire(f).prediction_evidence;m=self.module()
        for change in ('unknown','raw','noncanonical'):
            body=json.loads(m.prediction_evidence_bytes(ev))
            if change=='unknown': body['reference']=[1,2]
            elif change=='raw': body['power']['raw'][20]+=1
            else: body['power']['time_ns'][0]='01'
            with self.subTest(change=change),self.assertRaises(InversionContractError):
                m.parse_prediction_evidence(canonical_bytes(body))


class InputMetadataV2Tests(unittest.TestCase):
    def body(self):
        from tests.test_inversion_input_metadata import InputMetadataShapeTests
        f=archive_fixture();prepared=acquire(f)
        body=InputMetadataShapeTests().valid_body()
        body.update(schema='inversion.input/2',purpose='software_pair_test',parameter_core_sha256=f.parameters.parameter_core_sha256,
            source_binding=json.loads(canonical_bytes(prepared.snapshot.body['source_binding'])))
        body['applicability']['parameter_body']=json.loads(f.parameters.raw)
        body['request']['parameter_sha256']=f.parameters.raw_sha256
        body['applicability']['selection']['body']['parameter_sha256']=f.parameters.raw_sha256
        return body

    def project(self,body):
        from backend.inversion.input_metadata import project_input_metadata
        return project_input_metadata(body,result_id='result',content_sha256='d'*64,snapshot_sha256='e'*64,
            request_sha256=sha256(canonical_bytes(body['request'])).hexdigest())

    def test_v2_mapping_is_explicit_bounded_and_never_raw_data(self):
        body=self.body();body['source_binding'][0]['secret']='PRIVATE'
        result=self.project(body)
        self.assertIn('v2',result,'v2 public mapping and core identity missing')
        self.assertEqual(result['v2']['parameter_core_sha256'],body['parameter_core_sha256'])
        self.assertEqual(result['v2']['source_binding'][0]['original_identity_basis'],'approved_origin_mapping')
        self.assertEqual(result['v2']['verification_scope'],'synthetic_software_verification')
        self.assertFalse(result['v2']['field_accuracy_certified'])
        for private in ('PRIVATE','raw','prediction_evidence','parameter_body'):
            self.assertNotIn('"'+private+'"',json.dumps(result))

    def test_v2_mapping_conflict_or_missing_core_is_refused(self):
        for mode in ('missing_core','changed_core','missing_mapping','too_many','changed_bound','wrong_case'):
            body=self.body()
            if mode=='missing_core':del body['parameter_core_sha256']
            elif mode=='changed_core':body['parameter_core_sha256']='d'*64
            elif mode=='missing_mapping':body.pop('source_binding')
            elif mode=='too_many':body['source_binding']*=3
            elif mode=='changed_bound':body['source_binding'][0]['archive_file_sha256']='d'*64
            else:body['source_binding'][0]['case_id']='not-this-case'
            with self.subTest(mode=mode),self.assertRaises(InversionError):self.project(body)
