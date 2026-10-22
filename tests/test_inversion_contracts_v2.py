"""Reject a self-consistent digest masquerading as a complete v2 contract."""
from copy import deepcopy
from fractions import Fraction as Q
from dataclasses import replace
from hashlib import sha256
from importlib import import_module
import unittest

from backend.inversion.contracts import canonical_bytes, strict_json
from backend.inversion.parameter_contracts import ParameterSet
from backend.inversion.policy import AlgorithmPolicy
from tests.inversion_v2_support import fixture_body


class V2ContractTests(unittest.TestCase):
    def module(self, name):
        try:
            return import_module('backend.inversion.'+name)
        except ModuleNotFoundError:
            self.fail('v2 contract API not implemented: '+name)

    def parse(self, body, **kwargs):
        return self.module('version_dispatch').parse_parameters(canonical_bytes(body), **kwargs)

    def test_v2_identity_preserved(self):
        body=fixture_body(); raw=canonical_bytes(body); value=self.parse(body)
        self.assertIsInstance(value, ParameterSet)
        self.assertEqual(value.raw, raw)
        self.assertEqual(value.raw_sha256, sha256(raw).hexdigest())
        self.assertEqual(value.schema, 'inversion.parameter-set/2')
        self.assertIs(type(value.policy), AlgorithmPolicy)
        self.assertEqual(value.policy.schema, 'inversion.algorithm-policy/2')
        self.assertIsNone(value.provenance.coverage)
        self.assertFalse(value.missing)
        with self.assertRaises(TypeError):
            value.coverage_contract.body['source_kind']='field_measurement'

    def test_core_digest_dag(self):
        module=self.module('coverage_contracts')
        before=fixture_body(); after=deepcopy(before)
        after['coverage_contract']['binding']['budget_attachment_pin']['content_sha256']='7'*64
        self.assertEqual(module.parameter_core_bytes(before),module.parameter_core_bytes(after))
        self.assertEqual(module.coverage_payload_bytes(before['coverage_contract']),module.coverage_payload_bytes(after['coverage_contract']))
        self.assertNotEqual(sha256(canonical_bytes(before)).digest(),sha256(canonical_bytes(after)).digest())
        left=self.parse(before); right=self.parse(after)
        self.assertEqual(left.parameter_core_sha256,right.parameter_core_sha256)
        self.assertEqual(left.coverage_contract.payload_sha256,right.coverage_contract.payload_sha256)
        self.assertNotEqual(left.coverage_contract.raw_sha256,right.coverage_contract.raw_sha256)

    def test_core_all_numeric_fields(self):
        module=self.module('coverage_contracts'); original=fixture_body()
        base=module.parameter_core_bytes(original)
        changes=(('geometry','pivot_x_m'),('drive','ratio'),('uncertainty','speed_bound_rad_s'),
                 ('phase','zero_reading_rad'),('domain','load_N','max'),
                 ('provenance','operating_conditions',0,'temperature_K','min'),
                 ('provenance','channel_bindings',0,'channel_pin','manifest_sha256'),
                 ('geometry','evidence_refs',0,'content_sha256'))
        for path in changes:
            changed=deepcopy(original); node=changed
            for key in path[:-1]: node=node[key]
            node[path[-1]]='9'*64 if 'sha256' in path[-1] else '2'
            with self.subTest(path=path):
                self.assertNotEqual(base,module.parameter_core_bytes(changed))

    def test_unknown_statistical_or_missing_bounds(self):
        for kind in ('unknown','negative','sigma','missing','field','reference','passed'):
            body=fixture_body(); contract=body['coverage_contract']
            if kind=='unknown': contract['unexpected']='x'
            if kind=='negative': contract['measurement']['angle_noise_error_rad']='-0.1'
            if kind=='sigma': contract['measurement']['angle_noise_sigma_rad']='0'
            if kind=='missing': del contract['motion']['fourth_derivative_max_rad_s4']
            if kind=='field': contract['purpose']='field_pair_input'
            if kind=='reference': contract['inputs']['load']=deepcopy(contract['inputs']['active_power'])
            if kind=='passed': contract['passed']=True
            with self.subTest(kind=kind),self.assertRaises(ValueError): self.parse(body)
        raw=canonical_bytes(fixture_body()).replace(b'"scope":',b'"scope":"duplicate","scope":',1)
        with self.assertRaises(ValueError): self.module('version_dispatch').parse_parameters(raw)

    def test_pdf_pin_cannot_hide_physical_evidence(self):
        body=fixture_body(); module=self.module('coverage_contracts')
        body['coverage_contract']['binding']['budget_attachment_pin']=deepcopy(body['uncertainty']['evidence_refs'][0])
        with self.assertRaises(ValueError): self.parse(body)
        with self.assertRaises(ValueError): module.parameter_core_bytes(body)

    def test_missing_draft_does_not_become_computable(self):
        body=fixture_body(); del body['geometry']['pitman_length_m']
        value=self.parse(body,draft=True)
        self.assertIn('geometry.pitman_length_m',value.missing)
        self.assertIsNone(value.geometry)
        self.assertEqual(self.module('version_dispatch').missing_parameters(value),value.missing)
        with self.assertRaises(ValueError): self.parse(body)
        body['drive']['ratio']='-1'
        with self.assertRaises(ValueError): self.parse(body,draft=True)

    def test_draft_nested_pin_missing_is_reported(self):
        body=fixture_body()
        del body['coverage_contract']['inputs']['active_power']['pin']['sampling_pin']['review_version']
        value=self.parse(body,draft=True)
        self.assertIn('coverage_contract.inputs.active_power.pin.sampling_pin.review_version',value.missing)
        self.assertIsNone(value.coverage_contract)
        with self.assertRaises(ValueError): self.parse(body)

    def test_draft_budget_pin_missing_is_reported(self):
        body=fixture_body()
        del body['coverage_contract']['binding']['budget_attachment_pin']['source_description']
        value=self.parse(body,draft=True)
        self.assertIn('coverage_contract.binding.budget_attachment_pin.source_description',value.missing)
        self.assertIsNone(value.coverage_contract)

    def test_channel_digest_covers_all_native_evidence(self):
        module=self.module('coverage_contracts')
        from backend.inversion.series import ChannelPin
        wire=deepcopy(fixture_body()['coverage_contract']['inputs']['crank_angle']['pin'])
        wire.update(first_index=0,last_index=2,sync_pin=None,producer_endpoint=None)
        payload={'role':'crank_angle','raw':[1,2,3],'time_ns':['1','2','3'], 'quality':[0,0,0],
                 'scale':'1/100000000','offset':'0','pin':wire,
                 'original_file_sha256':'a'*64,'bound_file_sha256':'b'*64}
        channel=module.ChannelEvidence('crank_angle',(1,2,3),(Q(1),Q(2),Q(3)),(0,0,0),
            Q(1,100000000),Q(0),ChannelPin.from_wire(wire),'a'*64,'b'*64,sha256(canonical_bytes(payload)).hexdigest())
        self.assertEqual(module.channel_evidence_bytes(channel),canonical_bytes(payload))
        for changed in ({'raw':(2,2,3)},{'time_ns':(Q(0),Q(2),Q(3))},{'quality':(1,0,0)},
                        {'scale':Q(1,10000000)},{'offset':Q(1)},{'bound_file_sha256':'c'*64}):
            with self.subTest(changed=changed),self.assertRaises(ValueError): replace(channel,**changed)

    def test_rehash_cannot_expand_fixed_domain(self):
        module=self.module('coverage_contracts')
        for group,key in (('power_W','max'),('speed_rad_s','min'),('load_N','max')):
            body=fixture_body(); body['domain'][group][key]='999999' if key=='max' else '0'
            body['coverage_contract']['binding']['parameter_core_sha256']=sha256(module.parameter_core_bytes(body)).hexdigest()
            with self.subTest(group=group),self.assertRaises(ValueError): self.parse(body)

    def test_core_and_policy_mismatch_refused(self):
        for kind in ('core','policy','angle_pin'):
            body=fixture_body()
            if kind=='core': body['geometry']['pivot_x_m']='1.1'
            elif kind=='policy': body['coverage_contract']['binding']['policy_sha256']='0'*64
            else: body['coverage_contract']['inputs']['crank_angle']['pin']['manifest_sha256']='0'*64
            with self.subTest(kind=kind),self.assertRaises(ValueError): self.parse(body)

    def test_frozen_policy_and_budget_not_loosened(self):
        for section,key,value in (('policy','max_load_error_N','2000'),('uncertainty','torque_bound_Nm','101')):
            body=fixture_body()
            if section=='policy':
                body['policy']['body'][key]=value
                body['policy']['content_sha256']=sha256(canonical_bytes(body['policy']['body'])).hexdigest()
            else: body[section][key]=value
            module=self.module('coverage_contracts')
            body['coverage_contract']['binding']['parameter_core_sha256']=sha256(module.parameter_core_bytes(body)).hexdigest()
            body['coverage_contract']['binding']['policy_sha256']=body['policy']['content_sha256']
            with self.subTest(section=section),self.assertRaises(ValueError): self.parse(body)

    def test_byte_and_numeric_resource_limits(self):
        module=self.module('coverage_contracts')
        self.assertEqual(module.exact_number('0.'+'0'*125+'1'),Q(1,10**126))  # 128 chars
        for value in ('0.'+'0'*126+'1','1e3','1.0','-0','nan'):
            with self.subTest(value=value),self.assertRaises(ValueError): module.exact_number(value)
        raw=canonical_bytes(fixture_body())
        self.module('version_dispatch').parse_parameters(raw+b' '*(262144-len(raw)))
        with self.assertRaises(ValueError):
            self.module('version_dispatch').parse_parameters(raw+b' '*(262145-len(raw)))
        envelope=canonical_bytes({'command':{'key':'x'*36,'reason':'x'*512},'body':strict_json(raw)})
        with self.assertRaises(ValueError): strict_json(envelope+b' '*(262145-len(envelope)))
        with self.assertRaises(RuntimeError): module.exact_fraction(str(2**4096))

    def test_only_prediction_channels_in_evidence(self):
        module=self.module('coverage_contracts')
        self.assertTrue(set(module.PredictionEvidence.__dataclass_fields__).isdisjoint({'load','displacement','reference'}))
        from backend.inversion.series import ChannelPin
        pin=ChannelPin.from_wire(fixture_body()['coverage_contract']['inputs']['crank_angle']['pin'])
        with self.assertRaises(ValueError):
            module.ChannelEvidence('load',(1,),(Q(1),),(0,),Q(1),Q(0),pin,'a'*64,'b'*64,'c'*64)

    def test_v1_dispatch_preserves_exact_type_and_bytes(self):
        from tests.inversion_vectors import parameter_wire
        raw=parameter_wire(); value=self.module('version_dispatch').parse_parameters(raw)
        self.assertIs(type(value),ParameterSet)
        self.assertEqual(value.raw,raw)
        for schema in ('inversion.parameter-set/3','inversion.input/2'):
            body=fixture_body();body['schema']=schema
            with self.assertRaises(ValueError):self.parse(body)


if __name__=='__main__': unittest.main()
