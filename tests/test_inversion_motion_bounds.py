"""Exact polynomial identities and adversarial motion-error propagation."""
from dataclasses import replace
from fractions import Fraction as Q
from importlib import import_module
from math import factorial
import unittest

from backend.inversion.motion import MotionPoint
from backend.inversion.series import Sample


class MotionBoundsTests(unittest.TestCase):
    def module(self):
        try: return import_module('backend.inversion.motion_bounds')
        except ModuleNotFoundError: self.fail('exact motion bounds not implemented')

    def case(self, epoch=Q(0), *, center=3, errors=None, common=Q(0), differential=None, residual=0.):
        times=tuple(epoch+Q(j*10**9,128) for j in range(7))
        t=times[center]
        samples=tuple(Sample(times[j],j,Q(j,128),0) for j in range(7))
        nominal=MotionPoint(9,t,'valid',float(Q(center,128)),1.,0.,tuple(range(7)),times[-1]-times[0],residual,3.,0.,Q(0))
        return dict(power_index=9,center_ns=t,samples=samples,nominal=nominal,
            angle_error_rad=errors or (Q(1,200000000),)*7,
            differential_time_s=differential or (Q(0),)*7,common_time_s=common,
            derivative_bounds=(Q(1),Q(2),Q(3),Q(0)),
            derivative_support_ns=(epoch-10**9,epoch+10**9))

    def test_polynomial_reproduction_all_positions(self):
        module=self.module()
        windows=[tuple(Q(j-center,128) for j in range(7)) for center in range(7)]
        windows += [tuple(Q(j*2-5,256) for j in range(7)),tuple(Q(j*j-9,1024) for j in range(7))]
        for offsets in windows:
            weights=module.exact_weights(offsets)
            for order in range(3):
                for degree in range(4):
                    self.assertEqual(sum(w*x**degree for w,x in zip(weights[order],offsets)),factorial(order) if degree==order else 0)

    def test_uniform_goldens(self):
        module=self.module()
        for center,speed,acceleration in ((3,Q(448,3),Q(163840,21)),(0,Q(9728,21),Q(1064960,21))):
            weights=module.exact_weights(tuple(Q(j-center,128) for j in range(7)))
            self.assertEqual(sum(map(abs,weights[1])),speed)
            self.assertEqual(sum(map(abs,weights[2])),acceleration)

    def test_zero_residual_linear_quadratic_errors(self):
        module=self.module()
        for power in (1,2):
            case=self.case(center=0,errors=(Q(1,1000000),)*7)
            samples=tuple(replace(s,value_si=s.value_si+Q(j,6)**power*Q(1,1000000)) for j,s in enumerate(case['samples']))
            case['samples']=samples
            result=module.bound_window(**case)
            self.assertGreater(result.base[1],Q(1,100000))
            self.assertEqual(result.status,'refused')
            self.assertIn('motion_base_speed_budget_exceeded',result.reason_codes)
            self.assertGreater(result.base[2],0)

    def test_epoch_translation(self):
        module=self.module(); first=module.bound_window(**self.case())
        second=module.bound_window(**self.case(epoch=Q(10**18)))
        self.assertEqual(first.status,'valid')
        for field in ('exact_center','base','physical','numerical','angle_indices'):
            self.assertEqual(getattr(first,field),getattr(second,field))

    def test_common_clock_not_jitter(self):
        module=self.module(); plain=module.bound_window(**self.case())
        common=Q(1,1000000)
        shifted=module.bound_window(**self.case(common=common))
        self.assertEqual(shifted.base,plain.base)
        self.assertEqual(tuple(x-y for x,y in zip(shifted.physical,plain.physical)),(common,2*common,3*common))
        jitter=module.bound_window(**self.case(differential=(common,)*7))
        self.assertGreater(jitter.base[1],plain.base[1])

    def test_gap_and_support_refused(self):
        module=self.module()
        for kind in ('gap','quality','outside','no_derivative_support','short_derivative_support','nominal_indices'):
            case=self.case()
            if kind=='gap': case['samples']=tuple(replace(s,index=s.index+1) if i>=3 else s for i,s in enumerate(case['samples']))
            if kind=='quality': case['samples']=tuple(replace(s,quality=1,value_si=None) if i==3 else s for i,s in enumerate(case['samples']))
            if kind=='outside': case['center_ns']=Q(-1)
            if kind=='no_derivative_support': case['derivative_support_ns']=None
            if kind=='short_derivative_support':
                case['derivative_support_ns']=(case['samples'][0].time_ns,case['samples'][-1].time_ns)
                case['common_time_s']=Q(1,1000000)
            if kind=='nominal_indices': case['nominal']=replace(case['nominal'],angle_indices=(0,1,2,3,4,5,7))
            with self.subTest(kind=kind):
                result=module.bound_window(**case)
                self.assertEqual(result.status,'refused')
                self.assertIsNone(result.physical)
                self.assertIsNone(result.numerical)

    def test_numeric_difference_is_separate_and_physical_speed_fixed(self):
        module=self.module(); case=self.case()
        case['nominal']=replace(case['nominal'],omega_rad_s=1.0000001)
        result=module.bound_window(**case)
        self.assertEqual(result.exact_center,(Q(3,128),Q(1),Q(0)))
        self.assertEqual(result.physical[1],Q(1,100000))
        self.assertEqual(result.numerical[1],abs(Q.from_float(1.0000001)-1))

    def test_nonzero_fourth_derivative_taylor_bound(self):
        module=self.module(); case=self.case()
        case['derivative_bounds']=(Q(1),Q(2),Q(3),Q(1,20))
        result=module.bound_window(**case)
        offsets=tuple(Q(j-3,128) for j in range(7))
        weights=module.exact_weights(offsets)
        for order in range(3):
            expected=sum(abs(w)*Q(1,200000000) for w in weights[order])
            expected+=Q(1,480)*sum(abs(w)*abs(x)**4 for w,x in zip(weights[order],offsets))
            self.assertEqual(result.base[order],expected)

    def test_reversed_encoder_zero_offset_real_qr_mapping(self):
        from decimal import Decimal
        from backend.inversion.motion import estimate_motion, _angle_runs
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.version_dispatch import parse_parameters
        from tests.inversion_v2_support import fixture_body
        from tests.test_inversion_motion import motion_inputs
        parameters=parse_parameters(canonical_bytes(fixture_body()))
        parameters=replace(parameters,phase=replace(parameters.phase,encoder_direction=-1,zero_reading_rad=Decimal('7')))
        times=tuple(Q(j,128) for j in range(7))
        inputs=motion_inputs(times,tuple(7-t for t in times),targets=(Q(3,128),))
        nominal=estimate_motion(inputs,parameters,parameters.policy)[0]
        self.assertEqual(nominal.status,'valid')
        native,mapped,error=_angle_runs(inputs,parameters)[0]
        self.assertIsNone(error)
        case=self.case()
        case.update(power_index=0,nominal=nominal,
                    samples=tuple(replace(s,value_si=v) for s,v in zip(native,mapped)))
        result=self.module().bound_window(**case)
        self.assertEqual(result.status,'valid')
        self.assertEqual(result.exact_center,(Q(3,128),Q(1),Q(0)))
        self.assertLess(result.numerical[1],Q(1,10**10))

    def test_limits_and_cache_are_bounded(self):
        module=self.module()
        for center in range(140):
            module.exact_weights(tuple(Q(j*200-center,10000) for j in range(7)))
        self.assertLessEqual(module.weights_cache_info().currsize,128)
        with self.assertRaises(RuntimeError): module.exact_weights(tuple(Q(j,2**4096) for j in range(7)))


if __name__=='__main__': unittest.main()
