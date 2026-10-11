"""Independent algebra and analytic energy fixtures for the signed solver."""
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
import math
import unittest

from backend.inversion.mechanics import EnergyTerms
from backend.inversion.motion import MotionPoint
from backend.inversion.parameter_contracts import parse_parameter_set
from tests.inversion_vectors import parameter_body, parameter_wire
from tests.inversion_geometry_oracle import synthetic_body, derivatives
from tests.test_inversion_motion import motion_inputs


def algebra_parameters():
    body = parameter_body()
    body['domain']['load_N'] = {'min': '-1000', 'max': '1000'}
    body['domain']['speed_rad_s'] = {'min': '0', 'max': '10'}
    body['domain']['acceleration_rad_s2'] = {'min': '-10', 'max': '10'}
    body['uncertainty'].update(torque_bound_Nm='1', factor_bound_m='0.1')
    return parse_parameter_set(parameter_wire(body))


def motion(omega=2., alpha=0.):
    return MotionPoint(3, Fraction(123), 'valid', theta_rad=1., omega_rad_s=omega,
                       alpha_rad_s2=alpha, speed_error_bound_rad_s=0.)


def conservation_case(variable=False):
    """Analytic crank energy plus independent Cartesian finite-difference J."""
    body = synthetic_body()
    body['phase']['unwrap_kind'] = 'unwrapped'
    body['bodies'].update(crank_inertia_kg_m2='2', crank_first_moment_x_kg_m='3',
        beam_inertia_kg_m2='0', pitman_mass_kg='0', pitman_inertia_kg_m2='0', gravity_m_s2='10')
    body['drive'].update(upstream_inertia_kg_m2='3', crank_friction_Nm='4', viscous_friction_Nm_s='0.5')
    body['drive']['loss_grid'].update(power_W=['-10000', '10000'], motor_speed_rad_s=['1', '200'],
        loss_W=[['7', '7'], ['7', '7']], error_W=[['0.001', '0.001'], ['0.001', '0.001']])
    body['domain'].update(power_W={'min': '-10000', 'max': '10000'},
        speed_rad_s={'min': '0', 'max': '10'}, acceleration_rad_s2={'min': '-10', 'max': '10'},
        load_N={'min': '-1000', 'max': '1000'}, window_ns={'min': '1', 'max': '1000000'})
    body['uncertainty'].update(torque_bound_Nm='0.01', factor_bound_m='0.00001')
    parameters = parse_parameter_set(parameter_wire(body))
    policy = replace(parameters.policy, motion_max_angle_span_rad=Decimal('0.8'),
                     motion_max_residual_rad=Decimal('0.000001'))
    parameters = replace(parameters, policy=policy)
    times = tuple(Fraction(i, 80) for i in range(81))
    tau = Fraction.from_float(math.tau)
    angles = tuple(tau*(Fraction(4, 5)*t+Fraction(1, 5)*t*t) if variable else tau*t for t in times)
    inputs = motion_inputs(times, angles, targets=times[1:-1], cycle=(0, 1))
    powers = []
    for sample, t, angle in zip(inputs.power.samples, times[1:-1], angles[1:-1]):
        theta = float(angle)
        omega = math.tau*(.8+.4*float(t)) if variable else math.tau
        alpha = .4*math.tau if variable else 0.
        force = 100+10*math.sin(theta)
        J = 2*derivatives(body, theta)['beta_prime']
        # U'=-30*cos(theta), total upstream+downstream I=5.
        power = (-30*math.cos(theta)+5*alpha+4+.5*omega+force*J)*omega+7
        powers.append(replace(sample, value_si=Fraction(power)))
    return replace(inputs, power=replace(inputs.power, samples=tuple(powers))), parameters, policy


class SolverTests(unittest.TestCase):
    def solve(self, drive=10., J=.5, energy=None, moving=None, parameters=None):
        from backend.inversion.solver import solve_load
        return solve_load(drive_Nm=drive, energy=energy or EnergyTerms(0., 0., 0., 0.),
                          motion=moving or motion(), J_m=J,
                          parameters=parameters or algebra_parameters())

    def test_deadpoint_error_bound(self):
        point = self.solve()
        self.assertEqual(point.status, 'valid')
        self.assertAlmostEqual(point.load_N, 20.)
        self.assertAlmostEqual(point.error_bound_N, 7.5)
        for J in (.1, 0.):
            point = self.solve(J=J)
            self.assertEqual(point.status, 'unobservable')
            self.assertIsNone(point.load_N)
        negative = self.solve(J=-.5)
        self.assertEqual(negative.load_N, -20.)
        self.assertAlmostEqual(negative.error_bound_N, 7.5)

    def test_absolute_load_no_static_offset(self):
        # N = 100 - 3 - 4*2 - .5*5*2^2 - (2 + 3*2) = 71.
        parameters = algebra_parameters()
        parameters = replace(parameters, drive=replace(parameters.drive,
            crank_friction_Nm=Decimal(2), viscous_friction_Nm_s=Decimal(3)))
        point = self.solve(100., energy=EnergyTerms(3., 4., 5., 0.),
                           moving=motion(alpha=2.), parameters=parameters)
        self.assertEqual(point.load_N, 142.)
        self.assertEqual(point.intermediates['N_Nm'], 71.)

    def test_range_error_and_speed_gates_keep_diagnostics(self):
        point = self.solve(drive=1000.)
        self.assertEqual(point.status, 'outside_model_domain')
        self.assertIsNone(point.load_N)
        self.assertEqual(point.intermediates['nominal_load_N'], 2000.)
        parameters = algebra_parameters()
        parameters = replace(parameters, policy=replace(parameters.policy, max_load_error_N=Decimal(7)))
        point = self.solve(parameters=parameters)
        self.assertEqual(point.status, 'unobservable')
        self.assertIsNone(point.load_N)
        self.assertEqual(point.error_bound_N, 7.5)
        point = self.solve(moving=replace(motion(), speed_error_bound_rad_s=2.))
        self.assertEqual(point.status, 'unobservable')
        self.assertIsNone(point.load_N)

    def test_numerical_budgets_are_separate_and_enforced(self):
        parameters = algebra_parameters()
        parameters = replace(parameters, uncertainty=replace(parameters.uncertainty,
            numerical_budget_Nm=Decimal('.5'), numerical_factor_budget_m=Decimal('.05')))
        point = self.solve(parameters=parameters)
        self.assertAlmostEqual(point.error_bound_N, 4.5/.35)
        refused = self.solve(energy=EnergyTerms(0., 0., 0., .6), parameters=parameters)
        self.assertEqual(refused.status, 'motion_unreliable')
        self.assertIsNone(refused.load_N)

    def test_ideal_rounding_error_cannot_pass_zero_numerical_budget(self):
        # 1/3 is not exactly representable: even with otherwise exact algebra,
        # a claimed zero independent numerical budget cannot cover its output.
        point = self.solve(drive=1., J=3.)
        self.assertEqual(point.status, 'motion_unreliable')
        self.assertIn('solver_numerical_budget_exceeded', point.reason_codes)

    def test_oversize_and_nonfinite_refused(self):
        for drive, J in [(math.nan, .5), (math.inf, .5), (10., math.nan), (1e308, 1e-308)]:
            with self.subTest(drive=drive, J=J):
                point = self.solve(drive=drive, J=J)
                self.assertNotEqual(point.status, 'valid')
                self.assertIsNone(point.load_N)


class PredictionTests(unittest.TestCase):
    def test_signed_energy_balance(self):
        from backend.inversion.solver import predict
        for variable in (False, True):
            inputs, parameters, policy = conservation_case(variable)
            result = predict(inputs, parameters, policy)
            self.assertEqual(result.status, 'applicable')
            self.assertTrue(any(sample.value_si < 0 for sample in inputs.power.samples))
            self.assertTrue(any(sample.value_si > 0 for sample in inputs.power.samples))
            self.assertEqual(len(result.points), len(inputs.power.samples))
            for source, point in zip(inputs.power.samples, result.points):
                self.assertEqual((point.time_ns, point.power_index), (source.time_ns, source.index))
                if point.status == 'valid':
                    data = point.intermediates
                    self.assertAlmostEqual(point.load_N, 100+10*math.sin(data['theta_rad']), delta=.002)
                    want_drive = (float(source.value_si)-7)/data['omega_rad_s']-3*data['alpha_rad_s2']
                    self.assertAlmostEqual(data['drive_Nm'], want_drive, delta=1e-10)
                    self.assertEqual(data['loss_error_W'], .001)
            self.assertTrue(result.metadata['external_evidence_admission_required'])
            self.assertFalse(result.metadata['field_accuracy_certified'])
            self.assertTrue(result.metadata['numerical_assumptions'])

    def test_reference_cannot_affect_prediction(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        with self.assertRaises(TypeError):
            predict(inputs, parameters, policy, reference=object())

    def test_full_cycle_requires_original_endpoints_and_one_turn(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        variants = [replace(inputs, crank_angle=replace(inputs.crank_angle, samples=inputs.crank_angle.samples[1:])),
                    replace(inputs, crank_angle=replace(inputs.crank_angle, samples=tuple(
                        replace(s, value_si=s.value_si/2) for s in inputs.crank_angle.samples))),
                    replace(inputs, crank_angle=replace(inputs.crank_angle, samples=tuple(
                        replace(s, value_si=None, quality=1) if i==40 else s
                        for i, s in enumerate(inputs.crank_angle.samples))))]
        for variant in variants:
            result = predict(variant, parameters, policy)
            self.assertEqual(result.status, 'not_applicable')
            self.assertIn('full_cycle_unconfirmed', result.reason_codes)
            self.assertTrue(all(p.load_N is None for p in result.points))
        loose = replace(policy, motion_max_residual_rad=Decimal('2'))
        result = predict(inputs, replace(parameters, policy=loose), loose)
        self.assertEqual(result.status, 'not_applicable')

    def test_cycle_angular_consistency_uses_explicit_policy_ceiling(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        for delta, accepted in [(Fraction(1, 10**6), True), (Fraction(3, 10**6), False)]:
            angles = tuple(replace(s, value_si=s.value_si+delta*s.time_ns/10**9) for s in inputs.crank_angle.samples)
            result = predict(replace(inputs, crank_angle=replace(inputs.crank_angle, samples=angles)), parameters, policy)
            self.assertEqual('full_cycle_unconfirmed' not in result.reason_codes, accepted)

    def test_quality_domain_and_coverage_preserve_native_points(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        samples = tuple(replace(s, quality=1, value_si=None) if i<70 else s for i,s in enumerate(inputs.power.samples))
        result = predict(replace(inputs, power=replace(inputs.power, samples=samples)), parameters, policy)
        self.assertEqual(result.status, 'not_applicable')
        self.assertIn('insufficient_coverage', result.reason_codes)
        self.assertEqual(len(result.points), 79)
        self.assertEqual(result.points[0].status, 'input_gap')
        samples = tuple(replace(s, value_si=Fraction(20000)) for s in inputs.power.samples)
        result = predict(replace(inputs, power=replace(inputs.power, samples=samples)), parameters, policy)
        self.assertEqual(result.status, 'not_applicable')
        self.assertIn('no_valid_points', result.reason_codes)
        self.assertTrue(all(p.status=='outside_model_domain' for p in result.points))

    def test_isolated_and_gapped_centers_have_no_supported_duration(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        for retained in ((10,), (10, 60)):
            sparse = replace(inputs, power=replace(inputs.power,
                samples=tuple(inputs.power.samples[i] for i in retained)))
            for minimum in (Decimal('.8'), Decimal(0)):
                selected_policy = replace(policy, min_coverage_fraction=minimum)
                result = predict(sparse, replace(parameters, policy=selected_policy), selected_policy)
                self.assertTrue(all(p.status == 'valid' for p in result.points))
                self.assertEqual(result.status, 'not_applicable')
                self.assertEqual(result.coverage_fraction, Fraction(0))
                self.assertIn('insufficient_coverage', result.reason_codes)

    def test_native_gaps_split_supported_duration(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        samples = tuple(inputs.power.samples[i] for i in (10, 11, 60, 61))
        result = predict(replace(inputs, power=replace(inputs.power, samples=samples)), parameters, policy)
        self.assertTrue(all(p.status == 'valid' for p in result.points))
        # Two 1/80-second segments, divided by the declared one-second cycle.
        self.assertEqual(result.coverage_fraction, Fraction(1, 40))
        self.assertEqual(result.status, 'not_applicable')

    def test_irregular_centers_weight_time_and_invalid_points_break_support(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        # Native adjacent samples at .1375, .1875, .7625 seconds: the invalid
        # third point cannot contribute its much longer .575-second interval.
        samples = tuple(replace(inputs.power.samples[source], index=index)
                        for index, source in enumerate((10, 14, 60)))
        samples = samples[:2]+(replace(samples[2], value_si=None, quality=1),)
        sparse = replace(inputs, power=replace(inputs.power, samples=samples))
        for minimum, status in [(Decimal('.5'), 'not_applicable'), (Decimal('.04'), 'applicable')]:
            selected_policy = replace(policy, min_coverage_fraction=minimum)
            result = predict(sparse, replace(parameters, policy=selected_policy), selected_policy)
            self.assertEqual(tuple(p.status for p in result.points), ('valid', 'valid', 'input_gap'))
            self.assertEqual(result.coverage_fraction, Fraction(1, 20))
            self.assertEqual(result.status, status)
        samples = (samples[0], replace(samples[1], value_si=None, quality=1),
                   replace(inputs.power.samples[60], index=2))
        result = predict(replace(sparse, power=replace(sparse.power, samples=samples)), parameters, policy)
        self.assertEqual(result.coverage_fraction, Fraction(0))

    def test_unknown_envelope_and_oversize_refused(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        for parameters in (replace(parameters, source_kind='unknown'),
                           replace(parameters, uncertainty=replace(parameters.uncertainty, evidence_refs=()))):
            with self.assertRaises(ValueError):
                predict(inputs, parameters, policy)
        with self.assertRaises(ValueError):
            replace(inputs.power, samples=tuple(replace(inputs.power.samples[0], index=i, time_ns=Fraction(i)) for i in range(32769)))

    def test_wrapped_motion_and_large_epoch_preserve_predictions(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        baseline = predict(inputs, parameters, policy)
        epoch = Fraction(1_800_000_000_000_000_000)+Fraction(1, 3)
        tau = Fraction.from_float(math.tau)
        angles = tuple(replace(s, time_ns=s.time_ns+epoch, value_si=s.value_si % tau) for s in inputs.crank_angle.samples)
        shifted = replace(inputs,
            crank_angle=replace(inputs.crank_angle, samples=angles),
            power=replace(inputs.power, samples=tuple(replace(s, time_ns=s.time_ns+epoch) for s in inputs.power.samples)),
            cycle=replace(inputs.cycle, start_ns=epoch, end_ns=epoch+10**9))
        parameters = replace(parameters, phase=replace(parameters.phase, unwrap_kind='wrapped_2pi'))
        result = predict(shifted, parameters, policy)
        self.assertEqual(result.cycle, shifted.cycle)
        self.assertEqual(result.cycle.start_ns, epoch)
        self.assertEqual(result.cycle.end_ns, epoch+10**9)
        self.assertEqual(result.cycle.physical_cycle_id, 'cycle-1')
        self.assertEqual(baseline.cycle, inputs.cycle)
        self.assertEqual(result.status, baseline.status)
        for point, original in zip(result.points, baseline.points):
            self.assertEqual(point.time_ns-original.time_ns, epoch)
            self.assertEqual(point.load_N, original.load_N)

    def test_loss_uncertainty_coverage_not_added_twice(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        baseline = predict(inputs, parameters, policy)
        grid = replace(parameters.drive.loss_grid, error_W=((Decimal('.002'),)*2,)*2)
        changed = replace(parameters, drive=replace(parameters.drive, loss_grid=grid))
        result = predict(inputs, changed, policy)
        self.assertEqual(result.status, 'applicable')
        for point, original in zip(result.points, baseline.points):
            self.assertEqual(point.error_bound_N, original.error_bound_N)
        grid = replace(grid, error_W=((Decimal('10'),)*2,)*2)
        result = predict(inputs, replace(parameters, drive=replace(parameters.drive, loss_grid=grid)), policy)
        self.assertEqual(result.status, 'not_applicable')
        self.assertTrue(all('loss_error_exceeds_torque_envelope' in p.reason_codes for p in result.points))

    def test_power_window_loss_domain_and_policy_mismatch_refused(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        result = predict(replace(inputs, power_window_ns=2_000_000), parameters, policy)
        self.assertTrue(all('power_window_outside_domain' in p.reason_codes for p in result.points))
        grid = replace(parameters.drive.loss_grid, motor_speed_rad_s=(Decimal(1), Decimal(2)))
        result = predict(inputs, replace(parameters, drive=replace(parameters.drive, loss_grid=grid)), policy)
        self.assertTrue(all('loss_outside_domain' in p.reason_codes for p in result.points))
        with self.assertRaises(ValueError):
            predict(inputs, parameters, replace(policy, max_load_error_N=Decimal(1)))

    def test_loss_estimate_overflow_and_motion_refusal_are_not_valid_loads(self):
        from backend.inversion.solver import predict
        inputs, parameters, policy = conservation_case()
        huge = Decimal('1e1000')
        grid = replace(parameters.drive.loss_grid, loss_W=((huge,)*2,)*2)
        result = predict(inputs, replace(parameters, drive=replace(parameters.drive, loss_grid=grid)), policy)
        self.assertEqual(result.status, 'not_applicable')
        self.assertTrue(all(p.load_N is None for p in result.points))
        strict = replace(policy, motion_max_time_span_ns=1)
        result = predict(inputs, replace(parameters, policy=strict), strict)
        self.assertTrue(all(p.status=='motion_unreliable' and 'motion_time_span_exceeded' in p.reason_codes for p in result.points))

    def test_unknown_motion_status_fails_closed(self):
        # Future motion statuses must gain an explicit mapping before use.
        from backend.inversion.solver import solve_load
        point = solve_load(drive_Nm=1., energy=EnergyTerms(0., 0., 0., 0.),
            motion=replace(motion(), status='future_status'), J_m=.5, parameters=algebra_parameters())
        self.assertIsNone(point.load_N)
        self.assertEqual(point.status, 'motion_unreliable')


if __name__ == '__main__':
    unittest.main()
