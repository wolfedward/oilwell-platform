"""Opt-in projection of a genuinely acquired, verified historical snapshot."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import unittest
from unittest.mock import patch
from backend.inversion.contracts import canonical_bytes
from backend.inversion.input_contracts import InputSnapshot
from backend.inversion.queries import result_view
from backend.inversion.transactions import InversionError
from tests.inversion_result_support import ResultCase, ACTOR


class InputMetadataRouteTests(unittest.TestCase):
    def test_only_explicit_true_on_result_detail(self):
        from backend.inversion.routes import query
        from backend.inversion.errors import InversionContractError
        self.assertEqual(query('include_input_metadata=true', 'results.get'), {'include_input_metadata': True})
        for action, raw in [('results.points', 'include_input_metadata=true'),
                            ('results.get', 'include_input_metadata=false'),
                            ('results.get', 'include_input_metadata=1'),
                            ('results.get', 'include_input_metadata=true&include_input_metadata=true')]:
            with self.assertRaises(InversionContractError): query(raw, action)


class InputMetadataShapeTests(unittest.TestCase):
    """Pure historical-shape checks: no DB fixture or service acquisition."""
    def valid_body(self):
        from backend.inversion.input_metadata import ROLES, ABSOLUTE_TERMS
        identifier='11111111-1111-4111-8111-111111111111'
        request={key:identifier for key in ('dataset_version_id','pair_id','assembly_id','parameter_id','model_id')}
        request.update({key:'a'*64 for key in ('dataset_sha256','pair_sha256','parameter_sha256','model_sha256')})
        request.update(member_ordinal=0,pair_review_version=1,parameter_review_version=1,model_review_version=1,selection_version=1,operating_condition_ids=['condition'])
        locator=dict(gateway_id='gateway',device_id='device',capture_id=identifier)
        pin=dict(**locator,stream_id='stream',channel_id='channel',manifest_sha256='b'*64,first_index=0,last_index=1,
            producer_endpoint=dict(gateway_id='physical',device_id='sensor'),calibration_pin=dict(id=identifier,content_sha256='c'*64,review_version=1))
        condition=dict(id='condition',valid_from='2026-01-01',valid_to='2027-01-01')
        for key in ('temperature_K','supply_voltage_V','supply_frequency_Hz'):condition[key]=dict(min='1',max='2')
        return dict(request=request,channel_pins={role:deepcopy(pin) for role in ROLES},
            observation=dict(relative_timing_terms=dict(mapping_and_calibration_ns='1',noncommon_capture_utc_ns='2',total_bound_ns='3',capture_utc_bounds=[dict(capture=locator,utc_uncertainty_ns='2')]),
                absolute_timing_terms=[dict(role=role,**{key:'1' for key in ABSOLUTE_TERMS}) for role in ROLES]),
            applicability=dict(parameter_body=dict(provenance=dict(channel_bindings=[dict(role=role,source='native_waveform') for role in ROLES])),
                selection=dict(id=identifier,version=1,body=deepcopy(request)),registry=dict(well=dict(id='historical-well')),
                operating_conditions=[condition],assignment={},mechanical={}))

    def project(self,body):
        from backend.inversion.input_metadata import project_input_metadata
        return project_input_metadata(body,result_id='result',content_sha256='d'*64,snapshot_sha256='e'*64,
            request_sha256=sha256(canonical_bytes(self.valid_body()['request'])).hexdigest())

    def test_valid_pure_fixture_and_optional_historical_absence(self):
        body=self.valid_body()
        self.assertEqual(self.project(body)['applicability']['well_id'],'historical-well')
        body['applicability'].pop('registry')
        body['observation'].pop('relative_timing_terms');body['observation'].pop('absolute_timing_terms')
        metadata=self.project(body)
        self.assertIsNone(metadata['applicability']['well_id'])
        self.assertIsNone(metadata['observation']['relative_timing_terms'])

    def test_malformed_present_nested_shapes_are_controlled_409(self):
        paths=[(),('request',),('applicability',),('applicability','parameter_body'),
            ('applicability','parameter_body','provenance'),('applicability','parameter_body','provenance','channel_bindings'),
            ('applicability','parameter_body','provenance','channel_bindings',0),('channel_pins',),('channel_pins','load'),
            ('channel_pins','load','producer_endpoint'),('channel_pins','load','calibration_pin'),
            ('applicability','selection'),('applicability','selection','body'),('applicability','registry'),('applicability','registry','well'),
            ('applicability','assignment'),('applicability','mechanical'),('applicability','operating_conditions'),('applicability','operating_conditions',0),
            ('applicability','operating_conditions',0,'temperature_K'),('applicability','operating_conditions',0,'supply_voltage_V'),('applicability','operating_conditions',0,'supply_frequency_Hz'),
            ('observation',),('observation','relative_timing_terms'),('observation','relative_timing_terms','capture_utc_bounds'),
            ('observation','relative_timing_terms','capture_utc_bounds',0),('observation','relative_timing_terms','capture_utc_bounds',0,'capture'),
            ('observation','absolute_timing_terms'),('observation','absolute_timing_terms',0)]
        nullable={'producer_endpoint','calibration_pin','relative_timing_terms','absolute_timing_terms'}
        for path in paths:
            for malformed in (None,[],1,'bad'):
                if path and path[-1] in nullable and malformed is None:continue
                with self.subTest(path=path,malformed=malformed):
                    body=self.valid_body()
                    if not path:body=malformed
                    else:
                        target=body
                        for key in path[:-1]:target=target[key]
                        target[path[-1]]=malformed
                    with self.assertRaises(InversionError) as error:self.project(body)
                    self.assertEqual((error.exception.code,error.exception.status),('inversion.input_metadata_invalid',409))


class InputMetadataTests(ResultCase):
    def metadata(self):
        return result_view(self.results.get(self.published.id, actor=ACTOR, include_input_metadata=True))

    def test_verified_snapshot_exact_times_identity_and_historical_revoke(self):
        before=self.metadata()
        metadata=before['input_snapshot_metadata']
        snapshot=self.prepared_result.snapshot.body
        self.assertEqual((metadata['result_id'],metadata['content_sha256'],metadata['snapshot_sha256']),
            (str(self.published.id),before['content_sha256'],before['snapshot_sha256']))
        self.assertEqual(metadata['hash_scope'],'original_full_artifacts')
        self.assertEqual(metadata['request'],json.loads(canonical_bytes(snapshot['request'])))
        for key in ('start_ns','end_ns','relative_start_ns','relative_end_ns','uncertainty_ns','relative_alignment_ns'):
            self.assertEqual(metadata['observation'][key],snapshot['observation'][key])
        self.assertEqual(set(metadata['channel_pins']),{'active_power','crank_angle','load','displacement'})
        self.assertEqual(len(metadata['observation']['absolute_timing_terms']),4)
        self.assertEqual(metadata['applicability']['assignment']['well_id'],snapshot['applicability']['assignment']['well_id'])
        self.revoke_parameter()
        after=self.metadata()
        self.assertEqual(after['input_snapshot_metadata'],metadata)
        self.assertEqual(after['current_applicability']['state'],'restricted')
        self.assertEqual(before['content_sha256'],after['content_sha256'])

    def test_default_read_compatible_and_fresh_locked_opt_in_authority(self):
        self.denied.add('waveform.read')
        self.assertNotIn('input_snapshot_metadata',result_view(self.results.get(self.published.id,actor=ACTOR)))
        with self.assertRaises(InversionError) as error: self.metadata()
        self.assertEqual(error.exception.status,403)
        self.denied.clear()
        original=self.results._load
        def revoke_after_load(identifier):
            stored=original(identifier)
            self.denied.add('waveform.read')
            return stored
        with patch.object(self.results,'_load',side_effect=revoke_after_load):
            with self.assertRaises(InversionError) as error: self.metadata()
            self.assertEqual(error.exception.status,403)

    def project_changed(self, modify):
        from backend.inversion.input_metadata import project_input_metadata
        stored=self.results._load(self.published.id)
        body=json.loads(stored.prepared.snapshot.canonical)
        modify(body)
        raw=canonical_bytes(body)
        snapshot=InputSnapshot(body,raw,sha256(raw).hexdigest())
        # The helper accepts an already verified snapshot; result digest remains
        # an identity supplied by the owning StoredResult, never a projection hash.
        return project_input_metadata(body,result_id=str(stored.id),content_sha256=stored.content_sha256,
            snapshot_sha256=snapshot.content_sha256,request_sha256=stored.request_sha256)

    def test_nested_allowlist_redaction_exact_indices_and_physical_producer(self):
        def modify(body):
            pin=body['channel_pins']['load']
            pin['first_index']=9007199254741001; pin['last_index']=9007199254741002
            pin['producer_endpoint']={'gateway_id':'physical-gateway','device_id':'physical-sensor','password':'PRIVATE'}
            pin['calibration_pin']['authority']={'token':'PRIVATE'}
            body['observation']['relative_timing_terms']['secret']='PRIVATE'
            body['applicability']['assignment']['authority']='PRIVATE'
            body['applicability']['operating_conditions'][0]['source_description']='PRIVATE'
            body['actor']={'token':'PRIVATE'}
        metadata=self.project_changed(modify)
        pin=metadata['channel_pins']['load']
        self.assertEqual(pin['first_index'],'9007199254741001')
        self.assertEqual(pin['physical_endpoint'],{'gateway_id':'physical-gateway','device_id':'physical-sensor'})
        self.assertNotEqual(pin['device_id'],pin['physical_endpoint']['device_id'])
        text=json.dumps(metadata)
        for forbidden in ('PRIVATE','samples','parameter_body','model_body','accounting_bucket','source_description'):
            self.assertNotIn(forbidden,text)
        self.assertLessEqual(len(canonical_bytes(metadata)),128*1024)

    def test_bounds_duplicates_and_missing_optional_terms(self):
        mutations=[lambda b:b['channel_pins'].update(extra=b['channel_pins']['load']),
            lambda b:b['channel_pins']['load'].update(last_index=b['channel_pins']['load']['first_index']+32768),
            lambda b:b['observation']['relative_timing_terms']['capture_utc_bounds'].extend(
                [b['observation']['relative_timing_terms']['capture_utc_bounds'][0]]*2),
            lambda b:b['observation']['absolute_timing_terms'].append(b['observation']['absolute_timing_terms'][0]),
            lambda b:b['applicability']['operating_conditions'].extend([b['applicability']['operating_conditions'][0]]*64),
            lambda b:b['observation'].update(relative_alignment_ns='9'*257),
            lambda b:b['observation']['relative_timing_terms']['capture_utc_bounds'][0]['capture'].update(capture_id='11111111-1111-4111-8111-111111111111'),
            lambda b:b['request'].update(selection_version=b['request']['selection_version']+1)]
        for mutation in mutations:
            with self.subTest(mutation=mutation),self.assertRaises(InversionError): self.project_changed(mutation)
        metadata=self.project_changed(lambda b:[b['observation'].pop(k,None) for k in ('relative_timing_terms','absolute_timing_terms')])
        self.assertIsNone(metadata['observation']['relative_timing_terms'])
        self.assertIsNone(metadata['observation']['absolute_timing_terms'])
        from backend.inversion.input_metadata import project_input_metadata
        stored=self.results._load(self.published.id)
        arguments=dict(result_id=str(stored.id),content_sha256=stored.content_sha256,
            snapshot_sha256=stored.prepared.snapshot.content_sha256,request_sha256=stored.request_sha256)
        baseline=project_input_metadata(stored.prepared.snapshot.body,**arguments)
        with patch('backend.inversion.input_metadata.MAX_METADATA_BYTES',len(canonical_bytes(baseline))-1):
            with self.assertRaises(InversionError) as error:
                project_input_metadata(stored.prepared.snapshot.body,**arguments)
            self.assertEqual((error.exception.code,error.exception.status),('inversion.input_metadata_size_limit',409))
