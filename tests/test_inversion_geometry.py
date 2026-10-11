"""Catch closure/branch/sign mistakes with an independent Cartesian oracle."""

from dataclasses import replace
from decimal import Decimal
import math
import unittest

from backend.inversion import geometry as kernel
from backend.inversion.parameter_contracts import parse_parameter_set
from tests.inversion_vectors import parameter_wire
from tests import inversion_geometry_oracle as oracle

class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.body = oracle.synthetic_body()

    def build(self, body=None):
        return kernel.build_geometry(parse_parameter_set(parameter_wire(body or self.body)))

    def test_fourbar_closure_and_direction(self):
        for branch in (-1, 1):
            self.body['geometry']['branch'] = branch
            self.body['geometry']['rotation_sign'] = 1
            forward = self.build()
            self.body['geometry']['rotation_sign'] = -1
            reverse = self.build()
            for i in range(73):
                theta = i*math.tau/72
                g = reverse.at(theta)
                want = oracle.position(self.body, theta)
                self.assertAlmostEqual(math.dist(g.R, g.P), 3.0, delta=1e-10)
                self.assertAlmostEqual(math.dist(g.R, (3.0, 2.0)), 2.0, delta=1e-10)
                for key in ('P', 'R', 'C'):
                    self.assertLess(math.dist(getattr(g, key), want[key]), 1e-10)
                same = forward.at(-theta)
                self.assertAlmostEqual(g.s_m, same.s_m, delta=1e-11)
                self.assertAlmostEqual(g.J_m, -same.J_m, delta=1e-11)
                self.assertAlmostEqual(g.beta_second, same.beta_second, delta=1e-11)
                self.assertLess(g.closure_residual_m, 1e-10)
                self.assertGreaterEqual(g.derivative_condition, 1)

    def test_hand_calculated_circle_intersection(self):
        # At theta=0 the radical-axis offset is 5/18 of B->P; the
        # perpendicular term is sqrt(2255)/90 times that vector's normal.
        g = self.build().at(0)
        self.assertEqual(g.P, (0, 0.5))
        self.assertAlmostEqual(g.R[0], 13/6+math.sqrt(2255)/60, delta=1e-12)
        self.assertAlmostEqual(g.R[1], 19/12-math.sqrt(2255)/30, delta=1e-12)

    def test_virtual_work_derivatives(self):
        for branch in (-1, 1):
            for rotation in (-1, 1):
                self.body['geometry'].update(branch=branch, rotation_sign=rotation)
                geometry = self.build()
                for i in range(72):
                    theta = (i+0.23)*math.tau/72
                    g, want = geometry.at(theta), oracle.derivatives(self.body, theta)
                    self.assertAlmostEqual(g.J_m, 2*want['beta_prime'], delta=1e-7)
                    self.assertAlmostEqual(g.beta_prime, want['beta_prime'], delta=1e-7)
                    self.assertAlmostEqual(g.beta_second, want['beta_second'], delta=1e-5)
                    self.assertAlmostEqual(g.psi_prime, want['psi_prime'], delta=1e-7)
                    self.assertAlmostEqual(g.psi_second, want['psi_second'], delta=1e-5)
                    h, force = 1e-5, 137.0
                    work = force*(geometry.at(theta+h).s_m-geometry.at(theta-h).s_m)/(2*h)
                    self.assertAlmostEqual(force*g.J_m, work, delta=2e-6)

    def test_stroke_zero_deadpoints_and_displacement_sign(self):
        positive = self.build()
        roots = oracle.deadpoints(self.body)
        self.assertEqual(len(roots), 2)
        beta = [oracle.position(self.body, t)['beta'] for t in roots]
        expected_stroke = 2*abs(oracle.angle_difference(beta[0], beta[1]))
        self.assertAlmostEqual(positive.stroke_m, expected_stroke, delta=1e-10)
        self.assertAlmostEqual(positive.at(positive.bottom_theta_rad).s_m, 0, delta=1e-12)
        self.assertAlmostEqual(positive.at(positive.top_theta_rad).s_m, expected_stroke, delta=1e-10)
        self.body['geometry']['displacement_sign'] = -1
        negative = self.build()
        for theta in [i*math.tau/72 for i in range(72)] + [t+offset for t in roots for offset in (-1e-5, 0, 1e-5)]:
            p, n = positive.at(theta), negative.at(theta)
            self.assertGreaterEqual(p.s_m, -1e-12)
            self.assertAlmostEqual(n.s_m, expected_stroke-p.s_m, delta=1e-10)
            self.assertAlmostEqual(n.J_m, -p.J_m, delta=1e-12)
            self.assertAlmostEqual(p.beta_prime, oracle.derivatives(self.body, theta)['beta_prime'], delta=1e-7)
        for root in roots:
            self.assertLess(abs(positive.at(root).J_m), 1e-9)
            self.assertGreater(positive.at(root-1e-5).J_m*positive.at(root+1e-5).J_m*-1, 0)

    def test_continuous_branch_is_independent_of_call_order_and_angle_wrap(self):
        for bx, by in [('3', '2'), ('-3', '2'), ('-3', '-2'), ('3', '-2')]:
            self.body['geometry'].update(pivot_x_m=bx, pivot_y_m=by)
            geometry = self.build()
            values = [geometry.at(i*math.tau/360) for i in range(361)]
            self.assertLess(max(abs(a.beta_rad-b.beta_rad) for a, b in zip(values, values[1:])), 0.02)
            for index in (360, 40, 150, 0, 270, 2):
                self.assertEqual(geometry.at(index*math.tau/360), values[index])
            self.assertAlmostEqual(values[0].beta_rad, values[-1].beta_rad, delta=1e-12)

    def test_degenerate_assembly_refused(self):
        # Tangent, separated, contained, and a tangency that could permit branch switching.
        for overrides in (
            {'pivot_x_m': '4.5', 'pivot_y_m': '0'},
            {'pivot_x_m': '5', 'pivot_y_m': '0'},
            {'pivot_x_m': '0.8', 'pivot_y_m': '0'},
            {'pivot_x_m': '1.5', 'pivot_y_m': '0'},
            {'pivot_x_m': '0.2', 'pivot_y_m': '0', 'pitman_length_m': '2'},
        ):
            body = oracle.synthetic_body()
            body['geometry'].update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.build(body)
        # Directly constructed invalid typed inputs must still fail closed.
        p = parse_parameter_set(parameter_wire(self.body))
        for value in (Decimal('NaN'), Decimal('Infinity'), Decimal('-Infinity')):
            with self.assertRaises(ValueError):
                kernel.build_geometry(replace(p, geometry=replace(p.geometry, pivot_x_m=value)))
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                self.build().at(value)

    def test_incomplete_parameters_and_insufficient_numerical_budget_refused(self):
        p = parse_parameter_set(parameter_wire(self.body))
        for invalid in (replace(p, is_draft=True), replace(p, missing=('geometry',)), replace(p, geometry=None)):
            with self.assertRaises(ValueError):
                kernel.build_geometry(invalid)
        self.body['uncertainty']['numerical_factor_budget_m'] = '0'
        with self.assertRaises(ValueError):
            self.build()

    def test_numerical_error_budget_and_conditioning_gate(self):
        p = parse_parameter_set(parameter_wire(self.body))
        g = kernel.build_geometry(p).at(0.7)
        self.assertGreater(g.numerical_factor_error_m, 0)
        self.assertLessEqual(g.numerical_factor_error_m, float(p.uncertainty.numerical_factor_budget_m))
        with self.assertRaises(ValueError):
            kernel.build_geometry(replace(p, policy=replace(p.policy, numerical_margin=Decimal('0.99'))))

    def test_reported_error_encloses_independent_high_precision_factor(self):
        geometry = self.build()
        # Software enclosure checks on this fixture do not certify platform libm.
        for i in range(72):
            theta = (i+0.17)*math.tau/72
            point = geometry.at(theta)
            expected = 2*oracle.derivatives(self.body, theta, h=1e-15)['beta_prime']
            self.assertLessEqual(abs(point.J_m-expected), point.numerical_factor_error_m)

    def test_range_reduction_is_budgeted_and_unresolved_angles_refused(self):
        geometry = self.build()
        base = geometry.at(0.7)
        shifted = geometry.at(0.7+10*math.tau)
        self.assertAlmostEqual(shifted.J_m, base.J_m, delta=1e-12)
        for theta in (1e20, 1e200):
            with self.assertRaises(ValueError):
                geometry.at(theta)
        self.assertTrue(getattr(geometry, 'numerical_assumptions', None))


if __name__ == '__main__':
    unittest.main()
