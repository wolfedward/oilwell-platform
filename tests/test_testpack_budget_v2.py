from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest


class BudgetV2Tests(unittest.TestCase):
    def test_offline_binder_preserves_raw_codes_and_handles_display_floats(self):
        from tools.testpack.inversion_binding_v2 import offline_binding
        from tools.testpack.config_v2 import PackConfigV2
        from tools.testpack.contracts import Channel,Cycle
        from tools.testpack.storage import write_pack,generator_fingerprint_v2
        from tools.testpack.waveforms import encode_cycle
        channels=tuple(Channel(role,unit,scale,'0',tuple(range(1025)),(0,)*1025)
            for role,unit,scale in zip(('active_power','crank_angle','load','displacement'),('W','rad','kN','m'),('0.01','0.00000001','0.001','0.000001')))
        # Binding tests are intentionally nonphysical; source verification must
        # reject these codes. Display metadata can legitimately contain floats.
        cycle=Cycle('cycle-base',128,tuple(i*7812500 for i in range(1025)),channels,512,
            {'display_only':1.5},{'category':'mechanistic'})
        config=PackConfigV2()
        with TemporaryDirectory() as root:
            pack=write_pack(Path(root)/'pack',config,(encode_cycle(cycle,config),),generator_fingerprint_v2())
            bound=offline_binding(pack,'cycle-base')
            self.assertEqual(bound.evidence.power.raw,tuple(range(1025)))
            self.assertEqual(bound.reference.load.samples[128].value_si,128)
            self.assertEqual(bound.evidence.power.time_ns[128],1790784001000000000)

    def test_rehashed_scale_or_offset_cannot_be_silently_replaced(self):
        from dataclasses import replace
        from tools.testpack.inversion_binding_v2 import offline_binding
        from tools.testpack.config_v2 import PackConfigV2
        from tools.testpack.contracts import Channel,Cycle
        from tools.testpack.storage import write_pack,generator_fingerprint_v2,strict_json,canonical_bytes
        from tools.testpack.waveforms import encode_cycle
        channels=tuple(Channel(role,unit,scale,'0',tuple(range(1025)),(0,)*1025)
            for role,unit,scale in zip(('active_power','crank_angle','load','displacement'),('W','rad','kN','m'),('0.01','0.00000001','0.001','0.000001')))
        cycle=Cycle('cycle-base',128,tuple(i*7812500 for i in range(1025)),channels,512,{}, {'category':'mechanistic'})
        config=PackConfigV2();original=encode_cycle(cycle,config)
        for role in range(4):
            for field,value in (('scale','0.1'),('offset','7')):
                with self.subTest(role=role,field=field),TemporaryDirectory() as root:
                    metadata=strict_json(original.artifacts[1].content)
                    metadata['channels'][role][field]=value
                    altered=replace(original,artifacts=(original.artifacts[0],replace(original.artifacts[1],content=canonical_bytes(metadata))))
                    # write_pack recomputes every manifest/artifact hash.
                    pack=write_pack(Path(root)/'pack',config,(altered,),generator_fingerprint_v2())
                    with self.assertRaisesRegex(ValueError,'native metadata mismatch'):
                        offline_binding(pack,'cycle-base')

    def test_budget_is_not_an_approval(self):
        try: module=import_module('tools.testpack.budget_v2')
        except ModuleNotFoundError: self.fail('v2 finite-input budget report missing')
        from tests.inversion_v2_support import registered_fixture
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
        body,evidence=registered_fixture()
        result=module.inspect_v2(evidence,parse_parameter_set_v2(canonical_bytes(body)))
        self.assertEqual(result['scope'],'finite_synthetic_input/1')
        self.assertFalse(result['field_accuracy_certified'])
        self.assertEqual(result['approved_execution_status'],'not_run')
        self.assertEqual(len(result['points']),1025)
        self.assertTrue(result['passed'],result['reasons'])

    def test_offline_native_binding_and_validator(self):
        try: module=import_module('tools.testpack.validate_v2')
        except ModuleNotFoundError: self.fail('v2 offline validation missing')
        from tools.testpack.build_v2 import build_v2
        from tools.testpack.config_v2 import PackConfigV2
        from tools.testpack.inversion_binding_v2 import offline_binding
        from tools.testpack.validate import offline_passed
        from backend.inversion.coverage_sources import verify_sources,verified
        from backend.inversion.comparison import compare
        from backend.inversion.solver_v2 import predict_v2
        from backend.inversion.coverage_sources import inputs_from_evidence
        from tools.testpack.storage import load_pack
        with TemporaryDirectory() as root:
            pack=build_v2(PackConfigV2(),Path(root)/'pack')
            bound=offline_binding(pack,'cycle-base')
            self.assertEqual(len(bound.evidence.power.raw),1025)
            self.assertNotEqual(bound.evidence.power.original_file_sha256,bound.evidence.power.bound_file_sha256)
            self.assertIn('OFFLINE',bound.parameters.provenance.notes)
            self.assertTrue(verified(verify_sources(bound.evidence,bound.parameters)))
            prediction=predict_v2(inputs_from_evidence(bound.evidence,bound.parameters),bound.parameters,bound.evidence)
            self.assertLessEqual(compare(prediction.prediction,bound.reference,bound.parameters.policy).primary['load_rmse'].value,500)
            checks=module.validate_pack_v2(pack)
            self.assertTrue(offline_passed(checks),[(c.check_id,c.status,c.actual) for c in checks if c.layer in ('offline','numeric') and c.status!='passed'])
            finite=[c for c in checks if c.check_id.endswith('-finite-inversion')]
            self.assertEqual(len(finite),8)
            self.assertTrue(all(c.actual['approved_execution_status']=='not_run' for c in finite))
            self.assertFalse(any(c.check_id=='inversion-comparison' for c in checks))
            # Manifest hashes cannot be bypassed by an already-loaded PackIndex.
            path=pack.root/'cycles/cycle-base.owpwave'
            content=path.read_bytes(); path.write_bytes(content+b' ')
            with self.assertRaises(ValueError): module.validate_pack_v2(pack)

    def test_cli_dispatches_by_configuration_without_changing_v1(self):
        try: module=import_module('tools.testpack.validate_v2')
        except ModuleNotFoundError: self.fail('v2 offline validation missing')
        from types import SimpleNamespace
        for version in (1,2):
            pack=SimpleNamespace(manifest={'configuration':{'schema':f'owp.testpack.config/{version}'}})
            target='tools.testpack.validate.validate_pack' if version==1 else 'tools.testpack.validate_v2.validate_pack_v2'
            with patch(target,return_value=('selected',)) as selected:
                self.assertEqual(module.validate_versioned(pack),('selected',))
                selected.assert_called_once_with(pack)

    def test_clipped_assertion_cannot_pass_from_unrelated_dead_points(self):
        from types import SimpleNamespace as N
        from tools.testpack import validate_v2
        points=[N(status='valid',load_N=100,reason_codes=()) for _ in range(1025)]
        points[512]=N(status='unobservable',load_N=None,reason_codes=('load_error_exceeded',))
        quality=[0]*1025;quality[128]=2
        result=N(prediction=N(points=points,status='not_applicable'),source_proof=N(status='verified'))
        evidence=N(power=N(quality=quality))
        self.assertFalse(validate_v2.fault_assertion('cycle-clipped',result,evidence))
        points[128]=N(status='invalid_quality',load_N=None,reason_codes=('invalid_power_quality',))
        self.assertTrue(validate_v2.fault_assertion('cycle-clipped',result,evidence))
