"""The server cannot trust a client's self-signed error bounds."""
from importlib import import_module
from copy import deepcopy
from hashlib import sha256
import unittest


class CoverageSourceTests(unittest.TestCase):
    def test_reference_not_accepted_by_source_checker(self):
        try: module=import_module('backend.inversion.coverage_sources')
        except ModuleNotFoundError: self.fail('independent installed source checker missing')
        from backend.inversion.coverage_contracts import PredictionEvidence
        self.assertTrue(set(PredictionEvidence.__dataclass_fields__).isdisjoint({'load','displacement','reference'}))
        self.assertTrue(callable(module.verify_sources))

    def fixture(self,case_id='cycle-base'):
        try: import_module('backend.inversion.coverage_sources')
        except ModuleNotFoundError: self.fail('independent installed source checker missing')
        from tests.inversion_v2_support import registered_fixture
        return registered_fixture(case_id)

    def proof(self,body,evidence):
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
        from backend.inversion.coverage_sources import verify_sources
        return verify_sources(evidence,parse_parameter_set_v2(canonical_bytes(body)))

    def test_independent_full_native_sources_verified(self):
        body,evidence=self.fixture()
        proof=self.proof(body,evidence)
        self.assertEqual(proof.status,'verified',proof.reason_codes)
        self.assertEqual(len(proof.angle_error_rad),1025)
        self.assertEqual(len(proof.power_error_W),1025)
        self.assertEqual(proof.common_time_s,0)
        self.assertEqual(len(proof.derivative_bounds),4)

    def test_rehash_cannot_shrink_bounds(self):
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.coverage_contracts import parameter_core_bytes,coverage_payload_bytes
        for case,section,field,value in (
            ('cycle-speed','motion','fourth_derivative_max_rad_s4','0'),
            ('cycle-noise','measurement','angle_noise_error_rad','0'),
            ('cycle-noise','measurement','power_noise_error_W','0'),
            ('cycle-base','power_encoding','pre_encoding_error_bound','0.001')):
            with self.subTest(case=case,field=field):
                body,evidence=self.fixture(case)
                target=body['coverage_contract']['inputs']['active_power']['encoding'] if section=='power_encoding' else body['coverage_contract'][section]
                target[field]=value
                binding=body['coverage_contract']['binding']
                binding['parameter_core_sha256']=sha256(parameter_core_bytes(body)).hexdigest()
                binding['budget_attachment_pin']['content_sha256']=sha256(coverage_payload_bytes(body['coverage_contract'])).hexdigest()
                proof=self.proof(body,evidence)
                self.assertEqual(proof.status,'refused')
                self.assertIn('coverage_basis_mismatch',proof.reason_codes)
                self.assertIsNone(proof.power_error_W)

    def test_rehash_native_code_quality_time_and_model_cannot_pass(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.coverage_contracts import ChannelEvidence,channel_evidence_bytes,parameter_core_bytes
        body,evidence=self.fixture()
        for field in ('raw','quality','time_ns'):
            with self.subTest(field=field):
                b=deepcopy(body); value=evidence.power
                fields={name:getattr(value,name) for name in value.__dataclass_fields__}
                values=list(fields[field]); values[123]+=1; fields[field]=tuple(values)
                fields['channel_sha256']=sha256(channel_evidence_bytes(SimpleNamespace(**fields))).hexdigest()
                channel=ChannelEvidence(**fields); altered=replace(evidence,power=channel)
                descriptor=b['coverage_contract']['inputs']['active_power']
                descriptor['channel_sha256']=channel.channel_sha256
                descriptor['raw_sha256']=sha256(canonical_bytes(channel.raw)).hexdigest()
                descriptor['quality_sha256']=sha256(canonical_bytes(channel.quality)).hexdigest()
                descriptor['time_sha256']=sha256(canonical_bytes([str(t) for t in channel.time_ns])).hexdigest()
                proof=self.proof(b,altered)
                self.assertEqual(proof.status,'refused')
        altered=deepcopy(body); altered['bodies']['crank_inertia_kg_m2']='201'
        altered['coverage_contract']['binding']['parameter_core_sha256']=sha256(parameter_core_bytes(altered)).hexdigest()
        self.assertEqual(self.proof(altered,evidence).status,'refused')


if __name__=='__main__': unittest.main()
