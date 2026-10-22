from dataclasses import replace
from fractions import Fraction as Q
from importlib import import_module
from time import perf_counter
from unittest.mock import patch
import unittest

from backend.inversion.contracts import canonical_bytes
from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
from backend.inversion.comparison import compare
from tests.inversion_v2_support import registered_fixture


class SolverV2Tests(unittest.TestCase):
    def module(self):
        try: return import_module('backend.inversion.solver_v2')
        except ModuleNotFoundError: self.fail('v2 solver missing')

    def fixture(self,case='cycle-base'):
        from tests.inversion_v2_support import prediction_fixture
        body,evidence=registered_fixture(case)
        parameters=parse_parameter_set_v2(canonical_bytes(body))
        inputs,reference=prediction_fixture(evidence,parameters)
        return parameters,evidence,inputs,reference

    def test_four_positive_cases_original_coverage_and_rmse(self):
        module=self.module()
        for case in ('cycle-base','cycle-balance','cycle-friction','cycle-speed'):
            with self.subTest(case=case):
                parameters,evidence,inputs,reference=self.fixture(case)
                start=perf_counter()
                actual=module.predict_v2(inputs,parameters,evidence)
                comparison=compare(actual.prediction,reference,parameters.policy)
                self.assertEqual(len(actual.evidence),1025)
                self.assertEqual(actual.prediction.status,'applicable',actual.prediction.reason_codes)
                self.assertGreaterEqual(actual.prediction.coverage_fraction,Q(4,5))
                self.assertGreaterEqual(comparison.coverage['joint_fraction'].value,.8)
                self.assertIsNotNone(comparison.primary['load_rmse'].value)
                self.assertLessEqual(comparison.primary['load_rmse'].value,500)
                from tools.testpack.budget_v2 import report_prediction
                report=report_prediction(actual,perf_counter()-start)
                print(case,'coverage',float(actual.prediction.coverage_fraction),'joint',comparison.coverage['joint_fraction'].value,
                    'RMSE',comparison.primary['load_rmse'].value,'max_errors',report['maximum_errors'],'seconds',report['elapsed_s'],flush=True)

    def test_fault_inputs_keep_refused_native_points(self):
        module=self.module()
        for case in ('cycle-noise','cycle-missing','cycle-clipped','cycle-shifted'):
            with self.subTest(case=case):
                parameters,evidence,inputs,_=self.fixture(case)
                actual=module.predict_v2(inputs,parameters,evidence)
                self.assertEqual(len(actual.prediction.points),1025)
                self.assertEqual(actual.source_proof.status,'verified',actual.source_proof.reason_codes)
                refused=[p for p in actual.prediction.points[1:-1] if p.status!='valid']
                self.assertTrue(refused)
                self.assertTrue(all(p.load_N is None and p.reason_codes for p in refused))
                if case in ('cycle-noise','cycle-shifted'):
                    self.assertEqual(actual.prediction.status,'not_applicable')
                if case=='cycle-missing':
                    self.assertTrue(all(p.load_N is None for p in actual.prediction.points[400:420]))
                if case=='cycle-clipped':
                    clipped=[p for p,q in zip(actual.prediction.points,evidence.power.quality) if q==2]
                    self.assertTrue(clipped)
                    self.assertTrue(all(p.load_N is None and 'invalid_power_quality' in p.reason_codes for p in clipped))
                reasons={reason for p in refused for reason in p.reason_codes}
                if case=='cycle-noise': self.assertIn('motion_base_speed_budget_exceeded',reasons)
                if case=='cycle-shifted': self.assertIn('physical_factor_budget_exceeded',reasons)
                print(case,'status',actual.prediction.status,'coverage',float(actual.prediction.coverage_fraction),
                    'refusal_reasons',sorted({reason for p in refused for reason in p.reason_codes}),flush=True)

    def test_admission_exact_limits_with_other_conditions_valid(self):
        module=self.module(); parameters,evidence,inputs,_=self.fixture()
        output=module.predict_v2(inputs,parameters,evidence)
        from backend.inversion import coverage_physics as physics
        from backend.inversion.interval_runtime import RationalInterval as I
        from backend.inversion.motion import estimate_motion
        moving=estimate_motion(inputs,parameters,parameters.policy)[128]
        bounds=output.evidence[128].motion
        self.assertEqual(output.evidence[128].status,'valid')
        # Independent rational enclosures isolate each exact threshold; all
        # source, domain, support and native-input checks above remain real.
        limits=[Q(100),Q(1,10000),Q(1,100),Q(1,10**7),Q(1,10**7)]
        for changed in (None,0,1,2,3,4):
            values=limits.copy()
            if changed is not None: values[changed]+=Q(1,10**30)
            center={'N':I(Q(0),Q(0)),'J':I(Q(1),Q(1)),'s':I(Q(0),Q(0))}
            box={'N':I(-values[0],values[0]),'J':I(1-values[1],1+values[1]),'s':I(Q(0),Q(0))}
            nominal=physics.NominalState(values[2],1+values[3],values[4],{})
            with patch.object(physics,'enclose_terms',side_effect=(center,box)),patch.object(physics,'nominal_state',return_value=nominal):
                actual=physics.admit_point(inputs,parameters,moving,bounds,output.source_proof)
            self.assertEqual(actual.status,'valid' if changed is None else 'refused',actual.reason_codes)
        unknown=replace(bounds,physical=None)
        self.assertEqual(physics.admit_point(inputs,parameters,moving,unknown,output.source_proof).status,'refused')

    def test_reference_mutation_only_changes_comparison_and_original_support(self):
        module=self.module(); parameters,evidence,inputs,reference=self.fixture()
        before=module.predict_v2(inputs,parameters,evidence)
        altered=replace(reference,load=replace(reference.load,samples=tuple(replace(p,value_si=p.value_si+2000) for p in reference.load.samples)))
        after=module.predict_v2(inputs,parameters,evidence)
        self.assertEqual(before,after)
        self.assertNotEqual(compare(before.prediction,reference,parameters.policy),compare(after.prediction,altered,parameters.policy))
        points=before.prediction.points
        duration=sum((b.time_ns-a.time_ns for a,b in zip(points,points[1:]) if a.status==b.status=='valid' and b.power_index==a.power_index+1),Q(0))
        self.assertEqual(before.prediction.coverage_fraction,duration/(inputs.cycle.end_ns-inputs.cycle.start_ns))
        self.assertLess(before.prediction.coverage_fraction,1)
        self.assertTrue(any(p.load_N is None for p in points))

    def test_prediction_input_must_match_verified_native_evidence(self):
        module=self.module(); parameters,evidence,inputs,_=self.fixture()
        points=list(inputs.power.samples); points[7]=replace(points[7],value_si=points[7].value_si+1)
        changed=replace(inputs,power=replace(inputs.power,samples=tuple(points)))
        with self.assertRaises(ValueError): module.predict_v2(changed,parameters,evidence)

    def test_point_admission_cannot_reuse_proof_for_other_inputs(self):
        module=self.module(); parameters,evidence,inputs,_=self.fixture()
        output=module.predict_v2(inputs,parameters,evidence)
        from backend.inversion.coverage_physics import admit_point
        from backend.inversion.motion import estimate_motion
        moving=estimate_motion(inputs,parameters,parameters.policy)[128]
        points=list(inputs.power.samples); points[128]=replace(points[128],value_si=points[128].value_si+1000)
        changed=replace(inputs,power=replace(inputs.power,samples=tuple(points)))
        actual=admit_point(changed,parameters,moving,output.evidence[128].motion,output.source_proof)
        self.assertEqual(actual.status,'refused')
        self.assertIn('coverage_input_mismatch',actual.reason_codes)
