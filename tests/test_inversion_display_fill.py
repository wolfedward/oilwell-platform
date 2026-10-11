"""Display fill isolation and physical-deadpoint eligibility."""
from dataclasses import replace
from fractions import Fraction
import hashlib
from types import MappingProxyType
import unittest

from tests.test_inversion_comparison import policy, prediction, reference, prediction_bytes


def deadpoint():
    pred = prediction((0, 1, 3), (10., 99., 16.), speeds=(1., 0., -1.))
    return replace(pred, points=(pred.points[0], replace(pred.points[1], status='unobservable',
        load_N=None, reason_codes=('factor_uncertain',)), pred.points[2]), coverage_fraction=Fraction(0))


class DisplayFillTests(unittest.TestCase):
    def fill(self, pred=None, selected_policy=None):
        from backend.inversion.display_fill import fill_deadpoints
        return fill_deadpoints(pred or deadpoint(), selected_policy or policy())

    def test_fill_cannot_change_primary(self):
        from backend.inversion.comparison import compare
        pred = deadpoint()
        before = hashlib.sha256(prediction_bytes(pred)).hexdigest()
        first = compare(pred, reference(), policy())
        filled = self.fill(pred)
        self.assertIs(filled.prediction, pred)
        self.assertEqual(len(filled.fills), 1)
        point = filled.fills[0]
        self.assertEqual(point.load_N, 12.)
        self.assertEqual(point.source_indices, (0, 2))
        self.assertEqual(point.weights, (Fraction(2, 3), Fraction(1, 3)))
        self.assertEqual(point.original_reason_codes, ('factor_uncertain',))
        self.assertFalse(point.periodic_assumption)
        second = compare(pred, reference(), policy())
        self.assertEqual(first.primary, second.primary)
        self.assertEqual(first.coverage, second.coverage)
        self.assertIsNone(first.primary['load_mae'].value)
        self.assertIsNotNone(first.filled_comparison.primary['load_mae'].value)
        self.assertEqual(before, hashlib.sha256(prediction_bytes(pred)).hexdigest())

    def test_ordinary_gaps_and_uncertain_speed_cannot_fill(self):
        pred = deadpoint()
        for status, reasons in [('input_gap', ('invalid_power_quality',)),
                                ('motion_unreliable', ('numerical_failure',)),
                                ('outside_model_domain', ('load_outside_domain',)),
                                ('unobservable', ('speed_uncertain',)),
                                ('unobservable', ('future_reason',))]:
            changed = replace(pred, points=(pred.points[0], replace(pred.points[1], status=status,
                reason_codes=reasons), pred.points[2]))
            self.assertEqual(self.fill(changed).fills, ())
        changed = replace(pred, points=(pred.points[0], pred.points[1], replace(pred.points[2], power_index=3)))
        self.assertEqual(self.fill(changed).fills, ())

    def test_fill_time_angle_limits_and_deadpoint_evidence(self):
        pred = deadpoint()
        self.assertEqual(self.fill(pred, replace(policy(), fill_max_time_span_ns=2*10**9)).fills, ())
        self.assertEqual(self.fill(pred, replace(policy(), fill_max_angle_span_rad=Fraction(1, 10))).fills, ())
        changed = replace(pred, points=tuple(replace(p, intermediates=MappingProxyType(
            dict(p.intermediates, J_m=1.))) for p in pred.points))
        self.assertEqual(self.fill(changed).fills, ())
        changed = replace(pred, points=(pred.points[0], replace(pred.points[1],
            reason_codes=('load_error_exceeded',)), pred.points[2]))
        self.assertEqual(len(self.fill(changed).fills), 1)

    def test_cycle_seam_is_explicit_periodic_assumption(self):
        import math
        pred = prediction((0, 1, 9, 10), (99., 12., 10., 99.), speeds=(0., 1., -1., 0.))
        points = tuple(replace(p, intermediates=MappingProxyType(dict(p.intermediates,
            theta_rad=theta))) for p, theta in zip(pred.points, (0., .1, math.tau-.1, math.tau)))
        points = tuple(replace(p, status='unobservable', load_N=None,
            reason_codes=('factor_uncertain',)) if i in (0, 3) else p for i, p in enumerate(points))
        pred = replace(pred, points=points)
        result = self.fill(pred)
        self.assertEqual(len(result.fills), 2)
        self.assertTrue(all(p.periodic_assumption for p in result.fills))
        self.assertTrue(all(p.load_N == 11. for p in result.fills))
        self.assertTrue(all(p.source_indices == (2, 1) for p in result.fills))
        refused = replace(pred, metadata=MappingProxyType({}))
        self.assertEqual(self.fill(refused).fills, ())

    def test_seam_can_fill_existing_interior_power_centers(self):
        import math
        pred = prediction((0, 1, 9, 10), (99., 12., 10., 99.), speeds=(0., 1., -1., 0.))
        points = tuple(replace(p, time_ns=Fraction(t)*10**9,
            intermediates=MappingProxyType(dict(p.intermediates, theta_rad=theta)))
            for p, t, theta in zip(pred.points, (Fraction(1,10), 1, 9, Fraction(99,10)),
                                   (.01, .1, math.tau-.1, math.tau-.01)))
        points = tuple(replace(p, status='unobservable', load_N=None,
            reason_codes=('factor_uncertain',)) if i in (0, 3) else p for i,p in enumerate(points))
        pred = replace(pred, points=points)
        filled = self.fill(pred)
        self.assertEqual(len(filled.fills), 2)
        self.assertEqual(tuple(p.time_ns for p in filled.fills), (Fraction(10**8), Fraction(99*10**8)))
        self.assertAlmostEqual(filled.fills[0].load_N, 11.1)
        self.assertAlmostEqual(filled.fills[1].load_N, 10.9)
        self.assertTrue(all(p.periodic_assumption for p in filled.fills))
        self.assertIs(filled.prediction, pred)

    def test_missing_speed_error_or_multiple_reversals_never_fill(self):
        pred = deadpoint()
        for error in (None, 1., -1., float('nan')):
            points = tuple(replace(p, intermediates=MappingProxyType(dict(p.intermediates,
                speed_error_bound_rad_s=error))) for p in pred.points)
            self.assertEqual(self.fill(replace(pred, points=points)).fills, ())
        pred = prediction((0, 1, 2, 3, 4), (10.,)*5, speeds=(1., -1., 1., -1., -1.))
        points = tuple(replace(p, status='unobservable', load_N=None,
            reason_codes=('factor_uncertain',)) if 0 < i < 4 else p for i,p in enumerate(pred.points))
        self.assertEqual(self.fill(replace(pred, points=points)).fills, ())

    def test_fill_rejects_points_outside_selected_cycle(self):
        pred = deadpoint()
        pred = replace(pred, cycle=replace(pred.cycle, start_ns=Fraction(10**9)))
        self.assertEqual(self.fill(pred).fills, ())

    def test_derived_points_are_immutable_and_reference_free(self):
        from backend.inversion.display_fill import fill_deadpoints
        filled = self.fill()
        with self.assertRaises(AttributeError):
            filled.fills[0].load_N = 0
        with self.assertRaises(TypeError):
            fill_deadpoints(deadpoint(), policy(), reference=reference())


if __name__ == '__main__':
    unittest.main()
