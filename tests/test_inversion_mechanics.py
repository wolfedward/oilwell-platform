"""Catch gravity signs, missing variable inertia, and duplicated upstream mass."""

from dataclasses import replace
from decimal import Decimal
import math
import unittest

from backend.inversion import geometry as geometry_kernel, mechanics as kernel
from backend.inversion.parameter_contracts import parse_parameter_set
from tests.inversion_vectors import parameter_wire
from tests import inversion_geometry_oracle as oracle

class MechanicsTests(unittest.TestCase):
    def setUp(self):
        self.body = oracle.synthetic_body()

    def terms(self, theta, body=None):
        p = parse_parameter_set(parameter_wire(body or self.body))
        return kernel.energy_terms(geometry_kernel.build_geometry(p), theta, p)

    def zero_bodies(self):
        for key in ('crank_inertia_kg_m2', 'beam_inertia_kg_m2', 'pitman_mass_kg',
                    'pitman_inertia_kg_m2', 'crank_first_moment_x_kg_m',
                    'crank_first_moment_y_kg_m', 'beam_first_moment_x_kg_m',
                    'beam_first_moment_y_kg_m'):
            self.body['bodies'][key] = '0'
        self.body['bodies']['gravity_m_s2'] = '10'

    def test_no_double_counted_energy(self):
        self.zero_bodies()
        self.body['bodies'].update(crank_inertia_kg_m2='7', crank_first_moment_x_kg_m='2',
                                  crank_first_moment_y_kg_m='3')
        for rotation in (-1, 1):
            self.body['geometry']['rotation_sign'] = rotation
            for upstream in ('0', '1000000'):
                self.body['drive']['upstream_inertia_kg_m2'] = upstream
                for theta, want in ((0, -20*rotation), (math.pi/2, -30), (math.pi, 20*rotation)):
                    e = self.terms(theta)
                    self.assertAlmostEqual(e.U_prime_Nm, want, delta=1e-12)
                    self.assertAlmostEqual(e.M_kg_m2, 7, delta=1e-12)
                    self.assertAlmostEqual(e.M_prime_kg_m2, 0, delta=1e-12)

    def test_single_pitman_mass_at_crank_pin_has_hand_calculated_energy(self):
        self.zero_bodies()
        self.body['bodies'].update(pitman_mass_kg='8', pitman_com_fraction='0')
        for theta, gravity in ((0, 0), (math.pi/2, -40), (math.pi, 0), (3*math.pi/2, 40)):
            e = self.terms(theta)
            self.assertAlmostEqual(e.U_prime_Nm, gravity, delta=1e-12)
            self.assertAlmostEqual(e.M_kg_m2, 2, delta=1e-12)
            self.assertAlmostEqual(e.M_prime_kg_m2, 0, delta=1e-12)

    def test_independent_rigid_body_energy_over_full_rotation_and_deadpoints(self):
        self.body['bodies'].update(crank_inertia_kg_m2='7', crank_first_moment_x_kg_m='2',
                                  crank_first_moment_y_kg_m='3', beam_inertia_kg_m2='11',
                                  beam_first_moment_x_kg_m='4', beam_first_moment_y_kg_m='-2',
                                  pitman_mass_kg='8', pitman_com_fraction='0.37', pitman_inertia_kg_m2='5')
        for branch in (-1, 1):
            for rotation in (-1, 1):
                self.body['geometry'].update(branch=branch, rotation_sign=rotation)
                angles = [(i+0.31)*math.tau/72 for i in range(72)]
                angles += [root+offset for root in oracle.deadpoints(self.body) for offset in (-1e-5, 0, 1e-5)]
                p = parse_parameter_set(parameter_wire(self.body))
                g = geometry_kernel.build_geometry(p)
                for theta in angles:
                    e = kernel.energy_terms(g, theta, p)
                    u, m, mp = oracle.energy_derivatives(self.body, theta)
                    self.assertAlmostEqual(e.U_prime_Nm, u, delta=1e-7)
                    self.assertAlmostEqual(e.M_kg_m2, m, delta=1e-7)
                    self.assertAlmostEqual(e.M_prime_kg_m2, mp, delta=2e-6)
                    # Work-energy torque for both uniform and accelerating crank motion.
                    for speed, acceleration in ((2.0, 0.0), (1.7, -0.4)):
                        torque = e.U_prime_Nm+e.M_kg_m2*acceleration+0.5*e.M_prime_kg_m2*speed**2
                        self.assertAlmostEqual(torque, u+m*acceleration+0.5*mp*speed**2, delta=5e-6)
                    self.assertGreaterEqual(e.M_kg_m2, 0)

    def test_single_beam_and_pitman_rotation_inertia(self):
        self.zero_bodies()
        for field, angle in (('beam_inertia_kg_m2', 'beta'), ('pitman_inertia_kg_m2', 'psi')):
            self.zero_bodies()
            self.body['bodies'][field] = '3'
            for theta in (0.2, 1.2, 3.5, 5.4):
                expected = oracle.derivatives(self.body, theta)
                e = self.terms(theta)
                self.assertAlmostEqual(e.M_kg_m2, 3*expected[angle+'_prime']**2, delta=1e-7)
                self.assertAlmostEqual(e.M_prime_kg_m2,
                                       6*expected[angle+'_prime']*expected[angle+'_second'], delta=2e-5)
                self.assertEqual(e.U_prime_Nm, 0)

    def test_beam_mass_moment_and_inertia_hand_vector(self):
        self.zero_bodies()
        self.body['geometry'].update(pivot_x_m='3', pivot_y_m='0')
        self.body['bodies'].update(beam_inertia_kg_m2='3', beam_first_moment_x_kg_m='8')
        # theta=pi/2: rear arm x=-1/4, beta'=1/5, beta''=-2.16/sqrt(63).
        e = self.terms(math.pi/2)
        self.assertAlmostEqual(e.U_prime_Nm, -2, delta=1e-12)
        self.assertAlmostEqual(e.M_kg_m2, 0.12, delta=1e-12)
        self.assertAlmostEqual(e.M_prime_kg_m2, -2.592/math.sqrt(63), delta=1e-12)

    def test_numerical_torque_estimate_encloses_high_precision_oracle(self):
        self.body['bodies'].update(crank_first_moment_x_kg_m='2', beam_first_moment_y_kg_m='-3')
        p = parse_parameter_set(parameter_wire(self.body))
        g = geometry_kernel.build_geometry(p)
        for i in range(72):
            theta = (i+0.37)*math.tau/72
            e = kernel.energy_terms(g, theta, p)
            up, mass, mass_prime = oracle.high_precision_energy(self.body, theta)
            torque_error = abs((e.U_prime_Nm-up)+10*(e.M_kg_m2-mass)+50*(e.M_prime_kg_m2-mass_prime))
            self.assertLessEqual(torque_error, e.numerical_torque_error_Nm)

    def test_aggregates_are_not_multiplied_by_component_ledger_count(self):
        before = self.terms(1.1)
        self.body['bodies']['component_ledger'].append(
            {'id': 'beam_attachment', 'name': 'Beam attachment already in aggregate', 'owner': 'beam'})
        self.assertEqual(before, self.terms(1.1))

    def test_incomplete_mismatched_and_nonfinite_parameters_refused(self):
        p = parse_parameter_set(parameter_wire(self.body))
        g = geometry_kernel.build_geometry(p)
        for invalid in (replace(p, is_draft=True), replace(p, missing=('bodies',)),
                        replace(p, bodies=None),
                        replace(p, geometry=replace(p.geometry, branch=-1))):
            with self.assertRaises(ValueError):
                kernel.energy_terms(g, 0.5, invalid)
        for value in ('NaN', 'Infinity', '-Infinity', '1e400'):
            invalid = replace(p, bodies=replace(p.bodies, gravity_m_s2=Decimal(value)))
            invalid_geometry = geometry_kernel.build_geometry(invalid)
            with self.assertRaises(ValueError):
                kernel.energy_terms(invalid_geometry, 0.5, invalid)

    def test_energy_numerical_budget_gate(self):
        e = self.terms(0.7)
        self.assertGreater(e.numerical_torque_error_Nm, 0)
        self.assertLessEqual(e.numerical_torque_error_Nm, 1e-6)
        self.body['uncertainty']['numerical_budget_Nm'] = '0'
        with self.assertRaises(ValueError):
            self.terms(0.7)
        self.body['uncertainty']['numerical_budget_Nm'] = '0.000001'
        self.body['domain']['speed_rad_s']['max'] = '10000000000'
        with self.assertRaises(ValueError):
            self.terms(0.7)


if __name__ == '__main__':
    unittest.main()
