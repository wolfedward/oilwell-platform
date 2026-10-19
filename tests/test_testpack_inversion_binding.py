from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
import unittest
from uuid import uuid4

from tools.testpack.contracts import PackConfig
from tools.testpack.cycles import generate_cycles
from tools.testpack.inversion_binding import bind_inversion_parameters, prediction_inputs, software_budget
from backend.inversion.parameter_contracts import parse_parameter_set
from backend.inversion.contracts import strict_json


def pins():
    pin=lambda:{'id':str(uuid4()),'content_sha256':'a'*64,'review_version':2}
    channels={role:{'gateway_id':'REVIEW-GW','device_id':'TESTPACK-PUMP-001','capture_id':str(uuid4()),
        'stream_id':'native','channel_id':role,'manifest_sha256':'b'*64,'calibration_pin':pin(),'sampling_pin':pin(),
        'sync_pin':pin(),'first_index':0,'last_index':1024,
        'producer_endpoint':{'gateway_id':'REVIEW-GW','device_id':'TESTPACK-PUMP-001'}}
        for role in ('active_power','crank_angle','load','displacement')}
    return {'assembly_id':str(uuid4()),'assembly_version':1,'channels':channels,
        'attachment':{'id':str(uuid4()),'content_sha256':'c'*64},'valid_from':'2026-09-30T15:59:00.000000Z',
        'valid_to':'2026-09-30T16:01:00.000000Z','cycle_start_ns':1790784000000000000,
        'cycle_end_ns':1790784008000000000,'physical_cycle_id':'d'*64}


class TestPackInversionBinding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.cycles=generate_cycles(PackConfig())

    def test_binding_is_exact_existing_contract_and_fixed_policy(self):
        p=pins();before=deepcopy(p)
        raw=bind_inversion_parameters(self.cycles[0].model,p)
        parsed=parse_parameter_set(raw)
        self.assertEqual(p,before)
        self.assertEqual(str(parsed.uncertainty.torque_bound_Nm),'100')
        self.assertEqual(parsed.policy.reference_max_gap_ns,20000000)
        self.assertEqual(parsed.policy.fill_max_time_span_ns,20000000)
        self.assertEqual(parsed.phase.zero_reading_rad,0)
        self.assertEqual(parsed.phase.unwrap_kind,'unwrapped')
        self.assertEqual(parsed.geometry.horsehead_radius_m,6)

    def test_reference_not_given_to_predict(self):
        p=pins()
        actual=prediction_inputs(self.cycles[0],p['channels'])
        self.assertEqual(set(actual.__dataclass_fields__),{'power','crank_angle','cycle','power_window_ns','time_bound_ns'})
        changed=deepcopy(p);changed['channels']['load']['manifest_sha256']='f'*64
        self.assertEqual(actual,prediction_inputs(self.cycles[0],changed['channels']))

    def test_predeclared_software_budget_not_tuned_to_prediction(self):
        for cycle in self.cycles[:4]:
            result=software_budget(cycle)
            self.assertFalse(result['passed'],result)
            self.assertIn('declared_domain_torque_budget_exceeded',result['reasons'])
            self.assertGreater(result['domain_speed_torque_bound_Nm'],100)
            self.assertFalse(result['field_validation'])
            self.assertEqual(result['policy_fixed_before_prediction'],True)

    def test_existing_solver_diagnostics_not_approved_validation(self):
        from backend.inversion.solver import predict
        from backend.inversion.comparison import compare
        from backend.inversion.series import ReferenceInputs,SampleSeries,Sample,ChannelPin
        for cycle in self.cycles[:4]:
            p=pins();parameters=parse_parameter_set(bind_inversion_parameters(cycle.model,p))
            prediction=predict(prediction_inputs(cycle,p['channels']),parameters,parameters.policy)
            def reference(role):
                c=next(c for c in cycle.channels if c.role==role)
                scale=Fraction(c.scale)*(1000 if role=='load' else 1)
                return SampleSeries(role,tuple(Sample(Fraction(t),i,Fraction(v)*scale,0) for i,(t,v) in enumerate(zip(cycle.time_ns,c.raw))),ChannelPin.from_wire(p['channels'][role]))
            comparison=compare(prediction,ReferenceInputs(reference('load'),reference('displacement')),parameters.policy)
            self.assertGreaterEqual(float(prediction.coverage_fraction),.8,(cycle.case_id,prediction.status,prediction.reason_codes))
            self.assertIsNotNone(comparison.primary['load_rmse'].value)
            self.assertLessEqual(comparison.primary['load_rmse'].value,500,cycle.case_id)

    def test_variable_speed_has_additional_finite_case_budget_failure(self):
        result=software_budget(self.cycles[3])
        self.assertIn('sampled_speed_budget_exceeded',result['reasons'])
        self.assertGreater(result['uncovered_speed_error_rad_s'],0.00001)


if __name__=='__main__':unittest.main()
