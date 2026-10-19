from dataclasses import replace
import json
import unittest
from tools.testpack.contracts import PackConfig
from tools.testpack.cycles import generate_cycles, generate_shape_cases


class TestPackCycles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cycles = generate_cycles(PackConfig())

    def test_eight_cases_native_counts_and_turn_event(self):
        self.assertEqual(len(self.cycles), 8)
        for cycle in self.cycles:
            self.assertEqual(len(cycle.time_ns), 1025)
            self.assertEqual(set(b-a for a, b in zip(cycle.time_ns, cycle.time_ns[1:])), {7812500})
            self.assertEqual(cycle.time_ns[-1], 8000000000)
            self.assertLessEqual(cycle.diagnostics['event_time_error_s'], 1/256)
            self.assertLessEqual(cycle.diagnostics['window_refinement_error_W'], .02)
            self.assertLessEqual(cycle.diagnostics['derivative_refinement_error_m'], 1e-5)
        base = self.cycles[0]
        self.assertEqual(base.turn_index, max(range(1025), key=lambda i: base.channels[3].raw[i]))
        self.assertNotEqual(base.turn_index, 512)
        self.assertGreater(max(base.channels[3].raw)*1e-6, 2)

    def test_faults_do_not_modify_reference_truth(self):
        by_id = {c.case_id: c for c in self.cycles}
        base = by_id['cycle-base']
        for suffix in ('missing', 'clipped', 'shifted'):
            self.assertEqual(by_id['cycle-'+suffix].channels[2:], base.channels[2:])
        missing = by_id['cycle-missing'].channels[0]
        self.assertEqual([i for i, q in enumerate(missing.quality) if q == 1], list(range(400,420)))
        self.assertTrue(all(v == 0 for v in missing.raw[400:420]))
        clipped = by_id['cycle-clipped'].channels[0]
        self.assertTrue(any(q == 2 for q in clipped.quality))
        self.assertTrue(all(abs(v) == 2000000 for v,q in zip(clipped.raw,clipped.quality) if q == 2))
        shifted = by_id['cycle-shifted'].channels[0]
        self.assertEqual(shifted.quality[-2:], (1,1))
        self.assertEqual(shifted.raw[:-2], base.channels[0].raw[2:])

    def test_noise_reproducible_and_kept_within_declared_bounds(self):
        noise = self.cycles[4]
        self.assertEqual(noise, generate_cycles(PackConfig())[4])
        self.assertNotEqual(noise.channels[0].raw, generate_cycles(replace(PackConfig(),seed=9))[4].channels[0].raw)
        for channel, base, bound in zip(noise.channels,self.cycles[0].channels, (1001,2,51,11)):
            self.assertLessEqual(max(abs(a-b) for a,b in zip(channel.raw,base.raw)), bound)

    def test_extended_shapes_are_closed_and_excluded_from_accuracy(self):
        shapes = generate_shape_cases(replace(PackConfig(),profile='extended'))
        self.assertEqual(len(shapes), 12)
        self.assertTrue(all(s.category=='shape_only' and not s.facts['inversion_eligible'] for s in shapes))
        for shape in shapes:
            points = json.loads(shape.artifacts[0].content)['points']
            self.assertEqual(points[0],points[-1])
        self.assertEqual(generate_shape_cases(PackConfig()), ())


if __name__ == '__main__':
    unittest.main()
