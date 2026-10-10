"""Hand-calculated continuous-time card comparisons, never sample averages."""
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
import hashlib
import math
from types import MappingProxyType
import unittest

from backend.inversion.policy import parse_policy
from backend.inversion.series import ChannelPin, CycleWindow, ReferenceInputs, Sample, SampleSeries
from backend.inversion.solver import PredictionPoint, PredictionSeries
from tests.inversion_vectors import CHANNEL, policy_wire


def policy():
    return replace(parse_policy(policy_wire()), reference_max_gap_ns=10**10,
                   fill_max_time_span_ns=4*10**9, fill_max_angle_span_rad=Decimal('1'))


def prediction(times=(0, 1, 3), loads=(12., 12., 12.), positions=None, speeds=None):
    positions = positions if positions is not None else tuple(float(t) for t in times)
    speeds = speeds if speeds is not None else (1.,)*len(times)
    points = tuple(PredictionPoint(Fraction(t)*10**9, i, s, f, 0., 'valid', (),
        MappingProxyType(dict(J_m=v, omega_rad_s=1., theta_rad=i*.1, speed_error_bound_rad_s=0.)))
        for i, (t, s, f, v) in enumerate(zip(times, positions, loads, speeds)))
    cycle = CycleWindow(Fraction(0), Fraction(max(times) or 1)*10**9, 'cycle-1')
    return PredictionSeries(points, cycle, 'applicable', (), Fraction(1),
        MappingProxyType(dict(full_cycle=MappingProxyType(dict(reason='full_cycle_consistent')),
                             field_accuracy_certified=False)))


def reference(times=(0, 1, 3), loads=(10, 10, 10), positions=None):
    positions = positions if positions is not None else times
    source = ChannelPin.from_wire({**CHANNEL, 'last_index': 32767})
    def series(role, values):
        return SampleSeries(role, tuple(Sample(Fraction(t)*10**9, i,
            None if v is None else Fraction(v), 1 if v is None else 0)
            for i, (t, v) in enumerate(zip(times, values))), source)
    return ReferenceInputs(series('load', loads), series('displacement', positions))


def prediction_bytes(pred):
    # Audit all original values and metadata, including exact time and diagnostics.
    return repr(pred).encode('utf-8')


class ComparisonTests(unittest.TestCase):
    def compare(self, pred=None, ref=None, selected_policy=None):
        from backend.inversion.comparison import compare
        return compare(pred or prediction(), ref or reference(), selected_policy or policy())

    def test_constant_reference_keeps_absolute_error(self):
        report = self.compare()
        for key in ('load_mae', 'load_rmse', 'load_bias', 'load_max_absolute_error'):
            self.assertEqual(report.primary[key].value, 2.)
            self.assertEqual(report.primary[key].unit, 'N')
        self.assertIsNone(report.primary['load_correlation'].value)
        self.assertEqual(report.primary['load_correlation'].reason, 'constant_reference')

    def test_time_weighting_not_sample_average(self):
        report = self.compare(prediction(loads=(10., 12., 12.)))
        self.assertAlmostEqual(report.primary['load_bias'].value, 5/3)
        self.assertAlmostEqual(report.primary['load_rmse'].value, math.sqrt(28/9))

    def test_absolute_error_splits_zero_crossing(self):
        report = self.compare(prediction((0, 3), (8., 14.)), reference((0, 3), (10, 10)))
        self.assertAlmostEqual(report.primary['load_mae'].value, 5/3)
        self.assertEqual(report.primary['load_bias'].value, 1.)

    def test_work_preserves_traversal(self):
        for positions, loads, want in [((0., 1., 1., 0., 0.), (10., 10., 0., 0., 10.), 10.),
                                      ((0., 0., 1., 1., 0.), (10., 0., 0., 10., 10.), -10.)]:
            times = (0, 1, 2, 3, 4)
            report = self.compare(prediction(times, loads, positions), reference(times, loads, positions))
            self.assertEqual(report.work['prediction_complete_cycle'].value, want)
            self.assertEqual(report.work['reference_complete_cycle'].value, want)
            self.assertEqual(report.work['prediction_valid_segments'].unit, 'J')

    def test_gap_never_contributes_work_or_coverage(self):
        pred = prediction((0, 1, 2, 3), (10.,)*4)
        pred = replace(pred, points=pred.points[:2]+(replace(pred.points[2], status='input_gap',
            load_N=None, reason_codes=('invalid_power_quality',)),)+pred.points[3:])
        report = self.compare(pred, reference((0, 1, 2, 3), (10,)*4))
        self.assertEqual(report.coverage['joint_fraction'].value, Fraction(1, 3))
        self.assertEqual(report.work['prediction_valid_segments'].value, 10.)
        self.assertIsNone(report.work['prediction_complete_cycle'].value)
        self.assertEqual(report.invalid_reasons['invalid_power_quality'], 1)

    def test_hidden_bad_reference_or_missing_indices_break_interval(self):
        pred = prediction((0, 3), (12., 12.))
        ref = reference((0, 1, 2, 3), (10, None, 10, 10))
        for bad in (ref, replace(ref, load=replace(ref.load, samples=(ref.load.samples[0],)+ref.load.samples[2:]))):
            report = self.compare(pred, bad)
            self.assertEqual(report.coverage['joint_fraction'].value, 0)
            self.assertIsNone(report.primary['load_mae'].value)
            self.assertIsNone(report.work['prediction_valid_segments'].value)
            self.assertEqual([e.reference_edge_from_previous for e in report.evaluations], [False, False])
            self.assertTrue(all(e.load.reason is None and e.displacement.reason is None for e in report.evaluations))
        pred = replace(pred, points=(pred.points[0], replace(pred.points[1], power_index=2)))
        self.assertEqual(self.compare(pred).coverage['joint_fraction'].value, 0)

    def test_exact_interpolation_provenance_and_gap_ceiling(self):
        pred = prediction((Fraction(1, 3), Fraction(2, 3)), (11., 12.))
        pred = replace(pred, cycle=replace(pred.cycle, end_ns=Fraction(10**9)))
        ref = reference((0, 1), (10, 13))
        report = self.compare(pred, ref)
        pin = report.evaluations[0].load
        self.assertEqual(pin.indices, (0, 1))
        self.assertEqual(pin.weights, (Fraction(2, 3), Fraction(1, 3)))
        self.assertEqual(pin.source, ref.load.source)
        self.assertEqual(report.primary['load_mae'].value, 0.)
        report = self.compare(pred, ref, replace(policy(), reference_max_gap_ns=10**8))
        self.assertIsNone(report.primary['load_mae'].value)

    def test_measured_edges_survive_prediction_deadpoints_and_gap_ceiling(self):
        pred=prediction((0,1,2),(10.,10.,10.))
        pred=replace(pred,points=(pred.points[0],replace(pred.points[1],status='unobservable',
            load_N=None,reason_codes=('deadpoint',)),pred.points[2]))
        ref=reference((0,1,2),(10,20,30))
        report=self.compare(pred,ref)
        self.assertEqual([e.reference_edge_from_previous for e in report.evaluations],[False,True,True])
        self.assertEqual(report.coverage['joint_fraction'].value,0)
        report=self.compare(pred,ref,replace(policy(),reference_max_gap_ns=10**8))
        self.assertEqual([e.reference_edge_from_previous for e in report.evaluations],[False,False,False])

    def test_no_cycle_extrapolation_or_implicit_closure(self):
        pred = prediction((1, 2), (12., 12.))
        report = self.compare(pred, reference((0, 3), (10, 10)))
        # A bracket outside the chosen [0,2] cycle cannot support a target.
        self.assertEqual(report.coverage['joint_fraction'].value, 0)
        report = self.compare(pred, reference((0, 1, 2), (10, 10, 10)))
        self.assertEqual(report.coverage['joint_fraction'].value, Fraction(1, 2))
        self.assertIsNone(report.work['prediction_complete_cycle'].value)

    def test_strokes_split_at_j_omega_zero(self):
        report = self.compare(prediction((0, 2), (10., 14.), speeds=(1., -1.)),
                              reference((0, 2), (10, 10)))
        self.assertEqual(report.by_stroke['up']['load_bias'].value, 1.)
        self.assertEqual(report.by_stroke['down']['load_bias'].value, 3.)
        self.assertEqual(report.by_stroke['up']['duration'].value, 1.)
        self.assertEqual(report.by_stroke['down']['sample_count'].value, 1)

    def test_isolated_maximum_but_no_duration_metrics(self):
        report = self.compare(prediction((0,), (12.,)), reference((0,), (10,)))
        self.assertEqual(report.primary['load_max_absolute_error'].value, 2.)
        self.assertIsNone(report.primary['load_mae'].value)
        self.assertEqual(report.primary['load_mae'].reason, 'no_valid_duration')
        self.assertEqual(report.coverage['joint_duration'].value, 0.)

    def test_relative_denominator_and_constant_prediction(self):
        report = self.compare(prediction(loads=(2.,)*3), reference(loads=(0,)*3))
        self.assertEqual(report.primary['load_mae'].value, 2.)
        self.assertIsNone(report.primary['load_relative_mae_percent'].value)
        self.assertEqual(report.primary['load_relative_mae_percent'].reason, 'reference_denominator_below_threshold')
        report = self.compare(prediction(loads=(12.,)*3), reference(loads=(0, 1, 3)))
        self.assertEqual(report.primary['load_correlation'].reason, 'constant_prediction')

    def test_large_epoch_and_reference_changes_preserve_prediction(self):
        pred = prediction()
        before = prediction_bytes(pred)
        digest = hashlib.sha256(before).hexdigest()
        first = self.compare(pred)
        second = self.compare(pred, reference(loads=(20, 20, 20), positions=(3, 4, 6)))
        self.assertNotEqual(first.primary, second.primary)
        self.assertEqual(before, prediction_bytes(pred))
        self.assertEqual(digest, hashlib.sha256(prediction_bytes(pred)).hexdigest())
        epoch = Fraction(1800000000000000000)+Fraction(1, 3)
        shifted = replace(pred, points=tuple(replace(p, time_ns=p.time_ns+epoch) for p in pred.points),
            cycle=replace(pred.cycle, start_ns=pred.cycle.start_ns+epoch, end_ns=pred.cycle.end_ns+epoch))
        ref = reference()
        shifted_ref = ReferenceInputs(*(replace(s, samples=tuple(replace(p, time_ns=p.time_ns+epoch)
            for p in s.samples)) for s in (ref.load, ref.displacement)))
        self.assertEqual(first.primary, self.compare(shifted, shifted_ref).primary)

    def test_no_field_independence_or_applicability_promotion(self):
        pred = replace(prediction(), status='not_applicable', reason_codes=('insufficient_coverage',))
        report = self.compare(pred)
        self.assertEqual(report.prediction_status, 'not_applicable')
        self.assertEqual(report.independence['status'], 'unknown')
        self.assertFalse(report.independence['field_accuracy_certified'])
        with self.assertRaises(TypeError):
            report.primary['fake'] = 1

    def test_stroke_extrema_include_interpolated_transition(self):
        report = self.compare(prediction((0, 2), (10., 14.), speeds=(1., -1.)),
                              reference((0, 2), (10, 10)))
        self.assertEqual(report.by_stroke['up']['load_max_absolute_error'].value, 2.)
        self.assertEqual(report.by_stroke['up']['sample_count'].value, 1)

    def test_finite_extreme_correlation_does_not_overflow(self):
        report = self.compare(prediction((0, 1), (0., 1e200)), reference((0, 1), (0, 10**200)))
        self.assertAlmostEqual(report.primary['load_correlation'].value, 1.)
        self.assertIsNotNone(report.primary['load_rmse'].value)

    def test_tiny_representable_correlation_does_not_underflow(self):
        for sign in (1, -1):
            report = self.compare(prediction((0, 1, 2), (0., float(sign), 0.)),
                                  reference((0, 1, 2), (-1, Fraction(1, 10**200), 1)))
            self.assertAlmostEqual(report.primary['load_correlation'].value / 5e-201, sign)

    def test_relative_mae_uses_absolute_reference_integral(self):
        report = self.compare(prediction((0, 2), (-8., 12.)), reference((0, 2), (-10, 10)))
        self.assertEqual(report.primary['load_relative_mae_percent'].value, 40.)

    def test_hidden_displacement_gap_and_interval_gap_ceiling(self):
        ref = reference((0, 1, 2, 3), (10,)*4, positions=(0, None, 2, 3))
        report = self.compare(prediction((0, 3), (12., 12.)), ref)
        self.assertIsNone(report.primary['load_mae'].value)
        report = self.compare(selected_policy=replace(policy(), reference_max_gap_ns=10**9))
        self.assertEqual(report.coverage['joint_fraction'].value, Fraction(1, 3))

    def test_reference_changes_do_not_change_real_solver_bytes(self):
        from backend.inversion.solver import predict
        from tests.test_inversion_solver import conservation_case
        inputs, parameters, selected_policy = conservation_case()
        pred = predict(inputs, parameters, selected_policy)
        snapshot = prediction_bytes(pred)
        one = self.compare(pred, reference((0, 1), (10, 10)), policy())
        two = self.compare(pred, reference((0, 1), (20, 20), (5, 6)), policy())
        self.assertNotEqual(one.primary, two.primary)
        self.assertEqual(snapshot, prediction_bytes(pred))

    def test_empty_support_and_zero_signed_work_are_distinct(self):
        pred = prediction((0, 1, 2), (10.,)*3, (0., 1., 0.))
        report = self.compare(pred, reference((0, 1, 2), (10,)*3, (0, 1, 0)))
        self.assertEqual(report.work['prediction_complete_cycle'].value, 0.)
        empty = replace(pred, points=(), coverage_fraction=Fraction(0), status='not_applicable')
        report = self.compare(empty)
        self.assertIsNone(report.work['prediction_complete_cycle'].value)
        self.assertIsNone(report.primary['load_max_absolute_error'].value)
        self.assertEqual(report.coverage['joint_fraction'].value, 0)

    def test_time_correlation_preserves_negative_sign(self):
        report = self.compare(prediction((0, 1, 3), (3., 2., 0.)), reference((0, 1, 3), (0, 1, 3)))
        self.assertEqual(report.primary['load_correlation'].value, -1.)


if __name__ == '__main__':
    unittest.main()
