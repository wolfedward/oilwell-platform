"""Catch inward rounding, unavailable native proof, and unbounded operands."""
from fractions import Fraction as Q
from importlib import import_module
from unittest.mock import patch
import unittest


class IntervalRuntimeTests(unittest.TestCase):
    def runtime(self):
        try:
            return import_module('backend.inversion.interval_runtime')
        except ModuleNotFoundError:
            self.fail('strict inversion v2 interval runtime not implemented')

    def test_outward_endpoints(self):
        runtime = self.runtime()
        with runtime.proof_context() as flint:
            third = runtime.enclose(flint.arb(1)/3)
            root = runtime.enclose(flint.arb(2).sqrt())
        self.assertLessEqual(third.lo, Q(1, 3))
        self.assertGreaterEqual(third.hi, Q(1, 3))
        self.assertLessEqual(root.lo * root.lo, 2)
        self.assertGreaterEqual(root.hi * root.hi, 2)
        self.assertLess(root.hi-root.lo, Q(1, 10**35))

    def test_exact_rational_conversion(self):
        runtime = self.runtime()
        exact = Q(2**100+1, 2**103)
        with runtime.proof_context():
            result = runtime.enclose(runtime.ball(exact))
        self.assertEqual((result.lo, result.hi), (exact, exact))

    def test_context_fixed_and_restored(self):
        runtime = self.runtime()
        flint = import_module('flint')
        before = (flint.ctx.prec, flint.ctx.threads)
        with runtime.proof_context():
            self.assertEqual((flint.ctx.prec, flint.ctx.threads), (128, 1))
        self.assertEqual((flint.ctx.prec, flint.ctx.threads), before)

    def test_missing_dependency_is_unavailable(self):
        runtime = self.runtime()
        with patch.object(runtime, 'import_module', side_effect=ModuleNotFoundError('flint')):
            with self.assertRaises(RuntimeError) as caught:
                runtime.require_runtime()
        self.assertEqual(caught.exception.code, 'inversion.proof_runtime_unavailable')
        # Existing readers are not imported through or upgraded by the v2 runtime.
        import_module('backend.inversion.result_codec')

    def test_bit_limit(self):
        runtime = self.runtime()
        self.assertEqual(runtime.check_fraction(Q(2**4095)), Q(2**4095))
        for value in (Q(2**4096), Q(1, 2**4096)):
            with self.subTest(value_bits=max(value.numerator.bit_length(),value.denominator.bit_length())):
                with self.assertRaises(RuntimeError) as caught:
                    runtime.check_fraction(value)
                self.assertEqual(caught.exception.code, 'inversion.proof_resource_limit')

    def test_nonfinite_and_wrong_numeric_types_refused(self):
        runtime = self.runtime()
        with runtime.proof_context() as flint:
            for value in (flint.arb('nan'), flint.arb('+inf')):
                with self.assertRaises(RuntimeError):
                    runtime.enclose(value)
            for value in (0.1, True, '1/3'):
                with self.assertRaises((TypeError, RuntimeError)):
                    runtime.ball(value)

    def test_inexact_ball_preserves_radius(self):
        runtime = self.runtime()
        with runtime.proof_context() as flint:
            result = runtime.enclose(flint.arb(3, Q(1, 8).numerator/8))
        self.assertLessEqual(result.lo, Q(23, 8))
        self.assertGreaterEqual(result.hi, Q(25, 8))

    def test_counted_budget_refuses_next_evaluation(self):
        runtime = self.runtime()
        budget = runtime.ProofBudget()
        budget.consume(524288)
        with self.assertRaises(RuntimeError) as caught:
            budget.consume()
        self.assertEqual(caught.exception.code, 'inversion.proof_resource_limit')


if __name__ == '__main__':
    unittest.main()
