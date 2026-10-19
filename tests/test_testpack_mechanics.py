import ast
import math
from pathlib import Path
import unittest

from tools.testpack.mechanics import base_model, position, forward_state, window_power


class TestPackMechanics(unittest.TestCase):
    def test_circle_closure_and_independent_energy(self):
        model = base_model()
        for theta in [i*math.tau/24 for i in range(24)]:
            p = position(model, theta)
            x, y = p['joint_m']
            crank = (0.5*math.sin(theta), 0.5*math.cos(theta))
            self.assertAlmostEqual(math.hypot(x-3, y-2), 2, delta=1e-9)
            self.assertAlmostEqual(math.hypot(x-crank[0], y-crank[1]), 3, delta=1e-9)
            h = 1e-4
            slope = (position(model, theta+h)['displacement_m']-
                     position(model, theta-h)['displacement_m'])/(2*h)
            self.assertAlmostEqual(p['J_m'], slope, delta=1e-5)
        for t in (0.25, 1, 3, 4.5, 6.5):
            s = forward_state(model, t)
            p = position(model, s['theta_rad'])
            omega = math.tau/8
            # Hand energy law for this simplified baseline: constant inertia, alpha=0.
            force = 46000+14000*math.tanh(p['J_m']*omega/0.05)
            power = (-9.80665*1800*math.cos(s['theta_rad']) + 300 + 100*omega + force*p['J_m'])*omega+1200
            self.assertAlmostEqual(s['power_W'], power, delta=0.002)

    def test_window_mean_is_integrated(self):
        model = base_model()
        differences = []
        for t in (0, .1, 1.5, 3.9, 6, 7.999):
            mean16 = window_power(model, t, panels=16)
            mean32 = window_power(model, t, panels=32)
            self.assertLessEqual(abs(mean16-mean32), .02)
            differences.append(abs(mean32-forward_state(model, t)['power_W']))
        self.assertGreater(max(differences), 1e-6)

    def test_speed_case_accounts_for_upstream_inertia(self):
        model = base_model()
        model['motion']['epsilon'] = .1
        s = forward_state(model, 2)
        self.assertAlmostEqual(s['alpha_rad_s2'], -.1*(math.tau/8)**2, places=12)
        altered = base_model()
        altered['motion']['epsilon'] = .1
        altered['drive']['upstream_inertia_kg_m2'] = 0
        delta = s['power_W']-forward_state(altered, 2)['power_W']
        self.assertAlmostEqual(delta, 30*s['alpha_rad_s2']*s['omega_rad_s'], places=7)

    def test_oracle_does_not_import_production_kernels(self):
        root = Path(__file__).resolve().parents[1]/'tools/testpack'
        for name in ('mechanics.py', 'cycles.py'):
            tree = ast.parse((root/name).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                modules = ([node.module or ''] if isinstance(node, ast.ImportFrom)
                           else [n.name for n in node.names] if isinstance(node, ast.Import) else [])
                self.assertFalse(any(m == 'tests' or m.startswith(('tests.', 'backend.inversion')) for m in modules))


if __name__ == '__main__':
    unittest.main()
