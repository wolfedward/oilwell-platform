"""Exact quadrature regression and refusal tests; no services or field evidence."""
from fractions import Fraction as Q
from contextlib import contextmanager
from hashlib import sha256
import unittest
from unittest.mock import patch

from backend.inversion import coverage_sources as checker
from backend.inversion.contracts import canonical_bytes
from backend.inversion.interval_runtime import (
    MAX_EVALUATIONS, ProofBudget, RationalInterval, ball, enclose, proof_context,
)
from backend.inversion.synthetic_registry import case_definition, installed_config
from backend.inversion.transactions import InversionError
from tools.testpack import forward_v2 as generator


class QuadratureV2Tests(unittest.TestCase):
    @contextmanager
    def assertRefused(self, code):
        with self.assertRaises(InversionError) as caught:
            yield
        self.assertEqual(caught.exception.code, 'inversion.'+code)

    def case(self, case_id='cycle-variant-00'):
        digest = sha256(canonical_bytes(installed_config('extended'))).hexdigest()
        return case_definition(digest, case_id)

    def assertContains(self, interval, lo, hi=None):
        self.assertLessEqual(interval.lo, lo)
        self.assertGreaterEqual(interval.hi, lo if hi is None else hi)

    def helper(self, module):
        result = getattr(module, '_panel_quadrature', None)
        self.assertTrue(callable(result), 'independent panel quadrature is missing')
        return result

    def test_known_extended_rounding_boundary(self):
        case = self.case()
        generated = generator.forward_enclosures(case, Q(99, 64))['power_W']
        checked = checker._expected(case.config_sha256, case.case_id, 198)[0]
        for interval, encode in ((generated, generator.quantize_interval), (checked, checker._code)):
            with self.subTest(encoder=encode.__module__):
                try:
                    code = encode(interval, Q(1, 100))
                except InversionError as error:
                    self.fail(f'known integral remains undecidable: {error.code}')
                self.assertEqual(code, 4382705)
                self.assertLessEqual(interval.width, Q(2, 100))
        self.assertLessEqual(max(generated.lo, checked.lo), min(generated.hi, checked.hi))

    def test_exact_polynomials_and_whole_panel_remainder(self):
        for module in (generator, checker):
            with self.subTest(module=module.__name__), proof_context() as flint:
                helper = self.helper(module)
                previous = flint.ctx.cap
                try:
                    flint.ctx.cap = 6
                    mid = flint.arb_series([0, 1], prec=6)
                    whole = flint.arb_series([flint.arb(0, 1), 1], prec=6)
                    for degree, exact in ((0,Q(2)), (1,Q(0)), (2,Q(2,3)), (3,Q(0))):
                        total, radius = helper(mid**degree, whole**degree, Q(2))
                        self.assertContains(enclose(total), exact)
                        self.assertEqual(radius, 0)
                    total, radius = helper(1+mid+mid**2+mid**3, 1+whole+whole**2+whole**3, Q(2))
                    self.assertContains(enclose(total), Q(8,3))
                    self.assertEqual(radius, 0)
                    for sign in (-1, 1):
                        total, radius = helper(sign*mid**4, sign*whole**4, Q(2))
                        self.assertGreaterEqual(radius, Q(2,5))
                        self.assertContains(enclose(total+flint.arb(0,ball(radius))), sign*Q(2,5))
                    total, radius = helper(mid**5, whole**5, Q(2))
                    self.assertGreaterEqual(radius, 2)  # center c4=0 is NOT a panel bound
                    self.assertContains(enclose(total+flint.arb(0,ball(radius))), Q(0))
                finally:
                    flint.ctx.cap = previous

    def test_uncertain_center_and_root_are_not_dropped(self):
        for module in (generator, checker):
            with self.subTest(module=module.__name__), proof_context() as flint:
                helper = self.helper(module)
                previous = flint.ctx.cap
                try:
                    flint.ctx.cap = 6
                    r = flint.arb(ball(Q(3,2)), ball(Q(1,2)))
                    mid = flint.arb_series([0,1], prec=6)
                    whole = flint.arb_series([flint.arb(0,1),1], prec=6)
                    center = flint.arb_series([r,0,r], prec=6)
                    total, radius = helper(center, center, Q(2))
                    self.assertContains(enclose(total), Q(8,3), Q(16,3))
                    self.assertEqual(radius, 0)
                    total, radius = helper((1+r)*mid**4+r*mid**2+r,
                                           (1+r)*whole**4+r*whole**2+r, Q(2))
                    self.assertContains(enclose(total+flint.arb(0,ball(radius))), Q(52,15), Q(98,15))
                finally:
                    flint.ctx.cap = previous

    def test_registered_root_and_129_full_state_evaluations(self):
        case = self.case()
        for module in (generator, checker):
            with self.subTest(module=module.__name__):
                root = (generator.reversal_brackets(case)[0] if module is generator
                        else checker._root(case.config_sha256,case.case_id))
                original = module._state
                observed = []
                def state(*args):
                    observed.append(args[2])
                    return original(*args)
                checker._expected.cache_clear()
                with patch.object(module, '_state', side_effect=state):
                    if module is generator:
                        budget = ProofBudget()
                        module.forward_enclosures(case, Q(99,64), budget=budget)
                        self.assertEqual(budget.used,129)
                    else:
                        module._expected(case.config_sha256,case.case_id,198)
                self.assertEqual(observed,[root]*129)
                with module._context(series_cap=6) as flint:
                    def power(bracket):
                        args = (case.body,ball(Q(99,64)),bracket,flint)
                        return (original(*args,ProofBudget())['power_W'] if module is generator
                                else original(*args)[0])
                    whole = power(root)
                    self.assertGreater(whole.prec,4)
                    for endpoint in (root.lo,root.hi):
                        single = power(RationalInterval(endpoint,endpoint))
                        for index in (0,4):
                            a,b = enclose(whole[index]),enclose(single[index])
                            self.assertLessEqual(max(a.lo,b.lo),min(a.hi,b.hi))

    def test_truncated_nonfinite_and_domain_refused(self):
        for module in (generator,checker):
            with self.subTest(module=module.__name__), proof_context() as flint:
                helper = self.helper(module)
                previous = flint.ctx.cap
                try:
                    flint.ctx.cap = 6
                    valid = flint.arb_series([1,2,3,4,5],prec=6)
                    truncated = flint.arb_series([1,2],prec=4)
                    for mid,whole in ((truncated,valid),(valid,truncated)):
                        with self.assertRefused('proof_series_truncated'):
                            helper(mid,whole,Q(2))
                    for invalid in ('nan','inf','-inf'):
                        for index,is_mid in ((0,True),(2,True),(4,False)):
                            coefficients=[flint.arb(0) for _ in range(5)]
                            coefficients[index]=flint.arb(invalid)
                            bad=flint.arb_series(coefficients,prec=6)
                            with self.assertRefused('proof_nonfinite'):
                                helper(bad if is_mid else valid,valid if is_mid else bad,Q(2))
                    geometry=dict(self.case().body['geometry'])
                    for change in ({'pivot_x_m':'0','pivot_y_m':'0.5'},
                                   {'rear_arm_m':'0.01','pitman_length_m':'0.01'}):
                        with self.assertRefused('coverage_basis_mismatch'):
                            theta=flint.arb_series([0,1],prec=6)
                            if module is generator: module._joint(geometry|change,theta)
                            else: module._beta(geometry|change,theta,flint)
                finally:
                    flint.ctx.cap=previous

    def test_quantization_policy_unchanged(self):
        for encode in (generator.quantize_interval,checker._code):
            for value,want in ((Q(1,2),0),(Q(-1,2),0),(Q(3,2),2),(Q(-3,2),-2)):
                self.assertEqual(encode(RationalInterval(value,value),Q(1)),want)
            with self.assertRefused('proof_rounding_ambiguous'):
                encode(RationalInterval(Q(49,100),Q(51,100)),Q(1))
            for value in (-(2**31)-1,2**31):
                with self.assertRaises(InversionError): encode(RationalInterval(Q(value),Q(value)),Q(1))
            for value in (-(2**31),2**31-1):
                self.assertEqual(encode(RationalInterval(Q(value),Q(value)),Q(1)),value)

    def test_polar_domain_guard_preserves_scalar_budget_geometry(self):
        # enclose_terms passes the bottom angle as an Arb ball, not a series.
        geometry=self.case().body['geometry']
        with checker._context() as flint:
            scalar=checker._beta(geometry,ball(Q(0)),flint)
            series=checker._beta(geometry,flint.arb_series([0,1],prec=4),flint)
            a,b=enclose(scalar),enclose(series[0])
            self.assertLessEqual(max(a.lo,b.lo),min(a.hi,b.hi))
            invalid=dict(geometry, rear_arm_m='0.01',pitman_length_m='0.01')
            with self.assertRefused('coverage_basis_mismatch'):
                checker._beta(invalid,ball(Q(0)),flint)

    def test_context_and_resources_restored(self):
        import flint
        for module in (generator,checker):
            with self.subTest(module=module.__name__):
                previous=(flint.ctx.cap,flint.ctx.prec,flint.ctx.threads)
                for throws in (False,True):
                    try:
                        with module._context(series_cap=6):
                            self.assertEqual((flint.ctx.cap,flint.ctx.prec,flint.ctx.threads),(6,128,1))
                            if throws: raise ValueError('test exit')
                    except ValueError:
                        self.assertTrue(throws)
                    self.assertEqual((flint.ctx.cap,flint.ctx.prec,flint.ctx.threads),previous)
                with self.assertRaises(ValueError), module._context(series_cap=5): pass
                with module._context(series_cap=6):
                    helper=self.helper(module)
                    zero=flint.arb_series([0],prec=6)
                    for h in (Q(0),Q(-1),1,0.5):
                        with self.assertRaises(ValueError): helper(zero,zero,h)
                    # Valid input under4096 bits, but h**5 crosses the limit.
                    with self.assertRefused('proof_resource_limit'):
                        helper(zero,zero,Q(1,2**900))
                    with self.assertRefused('proof_resource_limit'):
                        helper(flint.arb_series([flint.arb(2)**4096],prec=6),zero,Q(1))
        with self.assertRefused('proof_resource_limit'):
            generator.forward_enclosures(self.case(),Q(99,64),budget=ProofBudget(MAX_EVALUATIONS))

    def test_cached_source_proof_still_charges_budget(self):
        from tests.inversion_v2_support import registered_fixture
        from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
        body,evidence=registered_fixture()
        checker._expected(evidence.config_sha256,'cycle-base',0)
        budget=ProofBudget(MAX_EVALUATIONS-128)
        with patch.object(checker,'ProofBudget',return_value=budget):
            proof=checker.verify_sources(evidence,parse_parameter_set_v2(canonical_bytes(body)))
        self.assertEqual(proof.status,'refused')
        self.assertEqual(proof.reason_codes,('proof_resource_limit',))


if __name__ == '__main__': unittest.main()
