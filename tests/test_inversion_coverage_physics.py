from fractions import Fraction as Q
from importlib import import_module
import unittest

from backend.inversion.interval_runtime import RationalInterval as I
from tests.inversion_v2_support import fixture_body
from tests.inversion_v2_oracle import numerator,jacobian,rational_numerator_at_zero


class CoveragePhysicsTests(unittest.TestCase):
    def module(self):
        try: return import_module('backend.inversion.coverage_physics')
        except ModuleNotFoundError: self.fail('v2 full physical/numerical admission missing')

    def parameters(self):
        from hashlib import sha256
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.coverage_contracts import parameter_core_bytes
        from backend.inversion.synthetic_registry import case_definition,installed_config
        from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
        body=fixture_body(); case=case_definition(sha256(canonical_bytes(installed_config('smoke'))).hexdigest(),'cycle-base')
        body['geometry'].update(dict(case.body['geometry'])); body['bodies'].update(dict(case.body['bodies']))
        body['drive'].update({k:v for k,v in case.body['drive'].items() if k not in ('loss_W','loss_error_W')})
        body['coverage_contract']['binding']['parameter_core_sha256']=sha256(parameter_core_bytes(body)).hexdigest()
        return parse_parameter_set_v2(canonical_bytes(body)),case

    def test_full_numerator_includes_both_inertias(self):
        module=self.module(); parameters,case=self.parameters()
        point=lambda x:I(Q(x),Q(x))
        result=module.enclose_terms(parameters,case,theta=point(0),omega=point(Q(4,5)),alpha=point(Q(1,10)),power=point(10000),loss=point(1200))
        want=rational_numerator_at_zero()
        self.assertLessEqual(result['N'].lo,want); self.assertGreaterEqual(result['N'].hi,want)
        changed=module.enclose_terms(parameters,case,theta=point(0),omega=point(Q(4,5)),alpha=point(Q(11,100)),power=point(10000),loss=point(1200))
        self.assertAlmostEqual(float((changed['N'].lo+changed['N'].hi-result['N'].lo-result['N'].hi)/2),-2.3,places=10)

    def test_physical_box_encloses_independent_perturbations(self):
        module=self.module(); parameters,case=self.parameters()
        result=module.enclose_terms(parameters,case,theta=I(Q(1)-Q(1,100000),Q(1)+Q(1,100000)),
            omega=I(Q('0.79999'),Q('0.80001')),alpha=I(Q('.099'),Q('.101')),power=I(Q(9999),Q(10001)),loss=I(Q('1199.98'),Q('1200.02')))
        for theta in (.99999,1.00001):
            value=jacobian(theta)
            self.assertLessEqual(float(result['J'].lo)-1e-9,value); self.assertGreaterEqual(float(result['J'].hi)+1e-9,value)
            for omega in (.79999,.80001):
                for alpha in (.099,.101):
                    for power,loss in ((9999,1200.02),(10001,1199.98)):
                        expected=numerator(power,theta,omega,alpha,loss=loss)
                        self.assertLessEqual(float(result['N'].lo)-1e-8,expected)
                        self.assertGreaterEqual(float(result['N'].hi)+1e-8,expected)

    def test_physical_vs_numeric_limits(self):
        module=self.module(); limits=(Q(100),Q(1,10000),Q(1,100),Q(1,10**7),Q(1,10**7))
        self.assertEqual(module.budget_status(*limits),('valid',()))
        for index in range(5):
            values=list(limits); values[index]+=Q(1,10**30)
            self.assertEqual(module.budget_status(*values)[0],'refused')
        self.assertEqual(module.budget_status(None,*limits[1:])[0],'refused')

    def test_entire_box_domain(self):
        module=self.module(); parameters,_=self.parameters()
        self.assertEqual(module.domain_status(parameters,power=I(Q(0),Q(1)),omega=I(Q('.11'),Q(1)),alpha=I(Q(-1),Q(1)),window_ns=1000000),())
        for change in ({'power':I(Q(249999),Q(250001))},{'omega':I(Q('.1'),Q('.12'))},{'alpha':I(Q('1.9'),Q('2.1'))}):
            values=dict(power=I(Q(0),Q(1)),omega=I(Q('.11'),Q(1)),alpha=I(Q(-1),Q(1)),window_ns=1000000); values.update(change)
            self.assertTrue(module.domain_status(parameters,**values))
