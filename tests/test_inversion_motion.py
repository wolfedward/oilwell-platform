"""Analytic local motion and refusal cases on native, exact timestamps."""

from dataclasses import replace
from decimal import Decimal, localcontext
from fractions import Fraction
import math
import unittest

from backend.inversion.parameter_contracts import parse_parameter_set
from backend.inversion.motion import estimate_motion
from backend.inversion.series import ChannelPin, CycleWindow, PredictionInputs, Sample, SampleSeries
from backend.inversion.small_linalg import cubic_fit
from tests.inversion_vectors import CHANNEL, parameter_body, parameter_wire


def motion_parameters():
    body = parameter_body()
    body['phase']['unwrap_kind'] = 'unwrapped'
    body['domain']['speed_rad_s'] = {'min': '0', 'max': '20'}
    body['domain']['acceleration_rad_s2'] = {'min': '-20', 'max': '20'}
    return parse_parameter_set(parameter_wire(body))


def motion_inputs(times=None, values=None, *, targets=(0,), epoch=0, indices=None,
                  bad=(), cycle=(-1, 1), window_ns=1, time_bound=Fraction(0)):
    times = tuple(Fraction(i, 10) for i in range(-3, 4)) if times is None else tuple(map(Fraction, times))
    values = tuple(1 + 2*t + 3*t*t + 4*t*t*t for t in times) if values is None else values
    indices = tuple(range(len(times))) if indices is None else indices
    source = ChannelPin.from_wire(CHANNEL | {'last_index': max(10, *indices, len(targets))})
    angles = SampleSeries('crank_angle', tuple(
        Sample(Fraction(epoch) + t*10**9, index, None if i in bad else Fraction(value), 1 if i in bad else 0)
        for i, (t, value, index) in enumerate(zip(times, values, indices))), source)
    power = SampleSeries('active_power', tuple(
        Sample(Fraction(epoch) + Fraction(t)*10**9, i, Fraction(-10), 0)
        for i, t in enumerate(targets)), source)
    return PredictionInputs(power, angles,
        CycleWindow(Fraction(epoch) + Fraction(cycle[0])*10**9,
                    Fraction(epoch) + Fraction(cycle[1])*10**9, 'cycle-1'),
        window_ns, time_bound)


class MotionTests(unittest.TestCase):
    def setUp(self):
        self.parameters = motion_parameters()
        self.policy = replace(self.parameters.policy, motion_max_time_span_ns=2_000_000_000,
                              motion_max_angle_span_rad=Decimal('20'),
                              motion_max_residual_rad=Decimal('0.000001'))

    def point(self, inputs=None, parameters=None, policy=None):
        return estimate_motion(inputs or motion_inputs(), parameters or self.parameters,
                               policy or self.policy)[0]

    def test_cubic_motion_goldens(self):
        point = self.point()
        self.assertEqual(point.status, 'valid')
        self.assertAlmostEqual(point.theta_rad, 1.0, delta=1e-9)
        self.assertAlmostEqual(point.omega_rad_s, 2.0, delta=1e-9)
        self.assertAlmostEqual(point.alpha_rad_s2, 6.0, delta=1e-9)
        self.assertEqual(point.power_index, 0)
        self.assertEqual(point.time_ns, Fraction(0))
        self.assertEqual(point.angle_indices, (0, 1, 2, 3, 4, 5, 6))
        self.assertEqual(point.window_span_ns, Fraction(600_000_000))
        self.assertLess(point.residual_rad, 1e-12)

    def test_large_epoch_is_translation_invariant(self):
        # Sub-nanosecond fractions at an epoch where float subtraction loses them.
        times = tuple(Fraction(i, 3_000_000_000) for i in range(-3, 4))
        values = tuple(Fraction(1) + 2*t for t in times)
        first = self.point(motion_inputs(times, values))
        shifted = self.point(motion_inputs(times, values, epoch=1_800_000_000_000_000_000))
        self.assertEqual(first.status, 'valid')
        for field in ('theta_rad', 'omega_rad_s', 'alpha_rad_s2', 'residual_rad', 'condition', 'speed_error_bound_rad_s'):
            self.assertEqual(getattr(first, field), getattr(shifted, field))
        self.assertEqual(shifted.time_ns, Fraction(1_800_000_000_000_000_000))

    def test_large_unwrapped_offset_cannot_discard_a_radian_as_valid(self):
        times = tuple(Fraction(i, 10) for i in range(-3, 4))
        values = tuple(2**54 + 1 + 2*t + 3*t*t + 4*t*t*t for t in times)
        point = self.point(motion_inputs(times, values))
        self.assertEqual(point.status, 'motion_theta_precision_exceeded')
        self.assertGreater(point.theta_error_estimate_rad, Fraction(1))
        self.assertLess(point.residual_rad, 1e-12)

    def test_theta_precision_ceiling_is_inclusive_and_exact(self):
        # Public diagnostics let a caller set a ceiling exactly at the combined
        # error. This tests the gate (including > vs >=), independently of QR.
        point = self.point()
        theta_error = getattr(point, 'theta_error_estimate_rad', None)
        self.assertIsNotNone(theta_error)
        combined = Fraction.from_float(point.residual_rad) + theta_error
        with localcontext() as context:
            context.prec = 300
            ceiling = Decimal(combined.numerator)/Decimal(combined.denominator)
            self.assertEqual(Fraction(ceiling), combined)
            at_ceiling = self.point(policy=replace(self.policy, motion_max_residual_rad=ceiling))
            self.assertEqual(at_ceiling.status, 'valid')
            # This decrement is lost by float conversion: compare exactly.
            beyond = self.point(policy=replace(self.policy, motion_max_residual_rad=ceiling-Decimal('1e-100')))
            self.assertEqual(beyond.status, 'motion_theta_precision_exceeded')

    def test_unwrapped_theta_remains_unwrapped_when_resolved(self):
        times = tuple(Fraction(i, 10) for i in range(-3, 4))
        values = tuple(1001 + 2*t + 3*t*t + 4*t*t*t for t in times)
        point = self.point(motion_inputs(times, values))
        self.assertEqual(point.status, 'valid')
        self.assertAlmostEqual(point.theta_rad, 1001., delta=1e-9)
        self.assertAlmostEqual(point.omega_rad_s, 2., delta=1e-9)
        self.assertAlmostEqual(point.alpha_rad_s2, 6., delta=1e-9)

    def test_seven_points_without_gap_or_extrapolation(self):
        self.assertEqual(self.point().status, 'valid')
        cases = [motion_inputs(times=tuple(Fraction(i, 10) for i in range(-3, 3))),
                 motion_inputs(bad=(3,)),
                 motion_inputs(indices=(0, 1, 2, 4, 5, 6, 7)),
                 motion_inputs(targets=(Fraction(4, 10),)),
                 motion_inputs(cycle=(-1, Fraction(2, 10)))]
        for inputs in cases:
            with self.subTest(inputs=inputs):
                self.assertNotEqual(self.point(inputs).status, 'valid')
        # Duplicate exact timestamps are refused at the typed input boundary.
        with self.assertRaises(ValueError):
            motion_inputs(times=(0, 0, 1, 2, 3, 4, 5))
        # Endpoint fit is a supported one-sided window, not extrapolation.
        point = self.point(motion_inputs(targets=(Fraction(-3, 10),)))
        self.assertEqual(point.status, 'valid')
        self.assertAlmostEqual(point.theta_rad, .562, delta=1e-9)
        self.assertAlmostEqual(point.omega_rad_s, 1.28, delta=1e-9)

    def test_deterministic_interior_and_edge_windows(self):
        times = tuple(Fraction(i, 10) for i in range(-5, 6))
        inputs = motion_inputs(times, tuple(2+t for t in times), targets=(Fraction(-4, 10), 0, Fraction(4, 10)))
        points = estimate_motion(inputs, self.parameters, self.policy)
        self.assertEqual([p.status for p in points], ['valid']*3)
        self.assertEqual([p.angle_indices for p in points],
                         [(0, 1, 2, 3, 4, 5, 6), (2, 3, 4, 5, 6, 7, 8), (4, 5, 6, 7, 8, 9, 10)])

    def test_windows_shift_inside_each_good_run_without_joining_a_gap(self):
        times = tuple(Fraction(i, 10) for i in range(-7, 8))
        for bad_quality in (True, False):
            inputs = motion_inputs(times, tuple(2+t for t in times),
                                   targets=(Fraction(-1, 10), 0, Fraction(1, 10)), bad=(7,))
            if not bad_quality:
                samples = inputs.crank_angle.samples
                inputs = replace(inputs, crank_angle=replace(inputs.crank_angle, samples=samples[:7]+samples[8:]))
            with self.subTest(bad_quality=bad_quality):
                points = estimate_motion(inputs, self.parameters, self.policy)
                self.assertEqual([p.status for p in points], ['valid', 'insufficient_angle_support', 'valid'])
                self.assertEqual(points[0].angle_indices, (0, 1, 2, 3, 4, 5, 6))
                self.assertEqual(points[2].angle_indices, (8, 9, 10, 11, 12, 13, 14))

    def test_mean_support_crosses_cycle(self):
        # Odd window: exact half-nanosecond + half-nanosecond uncertainty.
        base = motion_inputs(window_ns=3, time_bound=Fraction(1, 2))
        for start, end, want in [(-2, 2, 'insufficient_angle_support'),
                                  (-1, 2, 'power_support_outside_cycle'),
                                  (-2, 1, 'power_support_outside_cycle')]:
            inputs = replace(base, cycle=CycleWindow(Fraction(start), Fraction(end), 'cycle-1'))
            self.assertEqual(self.point(inputs).status, want)
        # Angular support brackets the center; full mean support is a cycle gate.
        inputs = motion_inputs(targets=(Fraction(-3, 10),), window_ns=3)
        self.assertEqual(self.point(inputs).status, 'valid')
        inputs = motion_inputs(targets=(Fraction(3, 10),), window_ns=3)
        self.assertEqual(self.point(inputs).status, 'valid')
        inputs = motion_inputs(targets=(Fraction(-3, 10),), cycle=(Fraction(-3, 10), 1))
        self.assertEqual(self.point(inputs).status, 'power_support_outside_cycle')

    def test_noisy_or_reversed_motion_refused(self):
        noisy = motion_inputs(values=(.4, .6, .8, 1.2, 1.2, 1.4, 1.6))
        self.assertEqual(self.point(noisy).status, 'motion_residual_exceeded')
        reversed_motion = motion_inputs(values=(1, .9, .8, .7, .6, .5, .4))
        self.assertNotEqual(self.point(reversed_motion).status, 'valid')
        self.assertEqual(self.point(policy=replace(self.policy, motion_max_condition=Decimal('1'))).status,
                         'motion_condition_exceeded')
        params = replace(self.parameters, uncertainty=replace(self.parameters.uncertainty, speed_bound_rad_s=Decimal('2')))
        self.assertEqual(self.point(parameters=params).status, 'speed_uncertain')

    def test_wrapped_reverse_encoder_and_physical_rotation(self):
        times = tuple(Fraction(i, 10) for i in range(-3, 4))
        values = tuple((.4 - (6.3 + 2*float(t))) % math.tau for t in times)
        phase = replace(self.parameters.phase, unwrap_kind='wrapped_2pi',
                        encoder_direction=-1, zero_reading_rad=Decimal('.4'))
        params = replace(self.parameters, phase=phase,
                         geometry=replace(self.parameters.geometry, rotation_sign=-1))
        point = self.point(motion_inputs(times, values), parameters=params)
        self.assertEqual(point.status, 'valid')
        self.assertAlmostEqual(point.theta_rad, 6.3, delta=1e-9)
        self.assertAlmostEqual(point.omega_rad_s, 2, delta=1e-9)

    def test_multi_revolution_alias_is_refused(self):
        phase = replace(self.parameters.phase, unwrap_kind='wrapped_2pi')
        domain = replace(self.parameters.domain, speed_rad_s=replace(self.parameters.domain.speed_rad_s, max=Decimal('100')))
        point = self.point(parameters=replace(self.parameters, phase=phase, domain=domain))
        self.assertNotEqual(point.status, 'valid')

    def test_span_and_domain_limits_are_enforced(self):
        self.assertEqual(self.point(policy=replace(self.policy, motion_max_time_span_ns=500_000_000)).status,
                         'motion_time_span_exceeded')
        self.assertEqual(self.point(policy=replace(self.policy, motion_max_angle_span_rad=Decimal('.5'))).status,
                         'motion_angle_span_exceeded')
        for field, bound, status in [('speed_rad_s', Decimal('1.98'), 'speed_outside_domain'),
                                     ('acceleration_rad_s2', Decimal('5'), 'acceleration_outside_domain')]:
            domain = replace(self.parameters.domain, **{field: replace(getattr(self.parameters.domain, field), max=bound)})
            # Secant speeds stay below 1.98; the fitted center speed is 2.
            times = tuple(Fraction(i, 10) for i in range(-3, 4))
            inputs = motion_inputs(times, tuple(1+2*t-4*t**3 for t in times)) if field == 'speed_rad_s' else None
            self.assertEqual(self.point(inputs, parameters=replace(self.parameters, domain=domain)).status, status)

    def test_bad_power_keeps_native_index_and_time(self):
        inputs = motion_inputs(targets=(0, Fraction(1, 10)))
        bad = replace(inputs.power.samples[0], value_si=None, quality=2)
        inputs = replace(inputs, power=replace(inputs.power, samples=(bad, inputs.power.samples[1])))
        points = estimate_motion(inputs, self.parameters, self.policy)
        self.assertEqual([(p.power_index, p.time_ns) for p in points], [(0, Fraction(0)), (1, Fraction(100_000_000))])
        self.assertEqual([p.status for p in points], ['invalid_power_quality', 'valid'])

    def test_draft_or_incomplete_parameters_refused(self):
        for params in (replace(self.parameters, is_draft=True), replace(self.parameters, phase=None),
                       replace(self.parameters, missing=('domain.speed_rad_s',))):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.point(parameters=params)

    def test_qr_fit_and_condition_use_full_matrix(self):
        times = (-3., -2., -1., 0., 1., 2., 3.)
        result = cubic_fit(times, tuple(1 + 2*t + 3*t*t + 4*t*t*t for t in times))
        for actual, expected in zip(result.coefficients, (1., 2., 3., 4.)):
            self.assertAlmostEqual(actual, expected, delta=1e-9)
        # Scaled design infinity norm is 4, intercept pseudoinverse row norm 29/21.
        # The largest inverse row is the cubic row: 9/2 (computed by exact algebra).
        self.assertAlmostEqual(result.condition, 18., delta=1e-9)
        for times, values in [((0.,)*7, (1.,)*7), ((0.,)*6, (1.,)*6),
                              ((float('nan'), 1., 2., 3., 4., 5., 6.), (1.,)*7)]:
            with self.subTest(times=times), self.assertRaises(ValueError):
                cubic_fit(times, values)

    def test_speed_envelope_retained_and_timing_uncertainty_added(self):
        params = replace(self.parameters, uncertainty=replace(self.parameters.uncertainty,
                                                               speed_bound_rad_s=Decimal('.25')))
        point = self.point(parameters=params)
        timed = self.point(motion_inputs(time_bound=Fraction(1_000_000)), parameters=params)
        self.assertGreaterEqual(point.speed_error_bound_rad_s, .25)
        self.assertAlmostEqual(timed.speed_error_bound_rad_s-point.speed_error_bound_rad_s, .02, delta=1e-12)

    def test_rank_loss_and_nonfinite_numerics_refused(self):
        with self.assertRaises(ValueError):
            cubic_fit((0., 1e-12, 2e-12, 3e-12, 4e-12, 5e-12, 1.), (0., 1., 2., 3., 4., 5., 6.))
        with self.assertRaises(ValueError):
            cubic_fit((-3., -2., -1., 0., 1., 2., 3.), (0., 1., 2., math.inf, 4., 5., 6.))

    def test_extreme_finite_time_scales_refused(self):
        for scale in (1e200, 1e-200):
            with self.subTest(scale=scale), self.assertRaises(ValueError):
                cubic_fit(tuple(i*scale for i in range(-3, 4)), (0., 1., 2., 3., 4., 5., 6.))


if __name__ == '__main__':
    unittest.main()
