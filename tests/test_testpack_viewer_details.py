"""Hand-derived display checks; no inverse/model output used as an oracle."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.testpack.contracts import PackConfig
from tools.testpack.storage import write_pack
from tests.test_testpack_contracts import GENERATOR


def rectangle():
    return {
        'load': {'values': [1, 3, 3, 1, 1], 'quality': [0]*5, 'unit': 'kN'},
        'displacement': {'values': [0, 0, 2, 2, 0], 'quality': [0]*5, 'unit': 'm'},
    }


class ViewerDetails(unittest.TestCase):
    def test_area_is_abs_signed_closed_integral_not_load_span(self):
        from tools.testpack.viewer_details import reference_summary
        channels = rectangle()
        result = reference_summary(channels)
        self.assertEqual(result['area_kj'], 4)
        self.assertEqual(result['signed_work_kj'], 4)
        self.assertEqual(result['stroke_m'], 2)
        self.assertEqual(result['load_range_kn'], [1, 3])
        for ch in channels.values():
            ch['values'].reverse()
        result = reference_summary(channels)
        self.assertEqual(result['area_kj'], 4)
        self.assertEqual(result['signed_work_kj'], -4)

    def test_reference_never_bridges_invalid_points_or_closes_open_endpoints(self):
        from tools.testpack.viewer_details import reference_summary
        for quality in (1, 2, 3, 4):
            channels = rectangle()
            channels['load']['quality'][2] = quality
            self.assertEqual(reference_summary(channels)['area_reason'], 'invalid_reference_samples')
            self.assertIsNone(reference_summary(channels)['area_kj'])
        channels = rectangle()
        channels['load']['values'][-1] = 1.001
        self.assertEqual(reference_summary(channels)['area_reason'], 'open_endpoints')
        self.assertIsNone(reference_summary(channels)['area_kj'])
        for value in (None, float('nan'), float('inf')):
            channels = rectangle(); channels['load']['values'][2] = value
            self.assertIsNone(reference_summary(channels)['area_kj'])

    def test_power_fault_does_not_remove_complete_reference_area(self):
        from tools.testpack.viewer_details import reference_summary
        channels = rectangle()
        channels['active_power'] = {'values': [None]*5, 'quality': [1]*5, 'unit': 'W'}
        self.assertEqual(reference_summary(channels)['area_kj'], 4)
        channels['displacement']['values'].pop()
        with self.assertRaises(ValueError): reference_summary(channels)
        channels = rectangle(); channels['load']['unit'] = 'A'
        with self.assertRaises(ValueError): reference_summary(channels)

    def test_quality_counts_do_not_treat_finite_clipped_values_as_good(self):
        from tools.testpack.viewer_details import quality_summary
        result = quality_summary({'power': {'values': [1, None, 20, 4, 5, None],
                                           'quality': [0, 1, 2, 3, 4, 0], 'unit': 'W'}})
        row = result['channels'][0]
        self.assertEqual((row['valid'], row['missing'], row['abnormal'], row['total']), (1, 1, 4, 6))
        self.assertEqual(row['codes'], {'0': 2, '1': 1, '2': 1, '3': 1, '4': 1})
        self.assertEqual(row['invalid_numeric'], 1)
        self.assertEqual(result['status'], 'has_invalid_samples')
        self.assertEqual(quality_summary(rectangle())['status'], 'usable_samples')

    def test_capture_uses_native_manifest_cadence_and_explicit_simulation_time(self):
        from tools.testpack.viewer_details import capture_summary
        wire = {'device_id': 'SYN-1', 'gateway_id': 'GW-1', 'capture_id': 'capture',
                'source_kind': 'synthetic', 'utc_start': '2026-09-30T16:00:00.000000Z',
                'window_ns': '2000000000', 'cycle': {'start_ns': '0', 'end_ns': '2000000000', 'turn_ns': '1000000000'},
                'streams': [{'rate_num': 2, 'rate_den': 1, 'frame_count': 5, 'offset_ns': '0',
                             'first_sample_index': '10', 'channels': [{'channel_id': r, 'unit': c['unit'],
                             'measurement_source': 'synthetic'} for r, c in rectangle().items()]}]}
        meta = {'sample_rate_hz': 2, 'frame_count': 5, 'source_kind': 'synthetic'}
        result = capture_summary(wire, meta, rectangle())
        self.assertEqual(result['sample_period_ns'], '500000000')
        self.assertEqual(result['first_sample_index'], '10')
        self.assertEqual(result['cycle_duration_s'], 2)
        self.assertEqual(result['strokes_per_min'], 30)
        self.assertEqual(result['sample_span_s'], 2)
        self.assertEqual(result['utc_start'], wire['utc_start'])
        self.assertEqual(result['simulation_start_local'], '2026-10-01T00:00:00+08:00')
        self.assertTrue(result['cycle_support_complete'])
        wire['utc_start'] = None
        unknown = capture_summary(wire, meta, rectangle())
        self.assertIsNone(unknown['utc_start'])
        self.assertIsNone(unknown['simulation_start_local'])
        wire['cycle']['end_ns'] = '2500000000'
        self.assertFalse(capture_summary(wire, meta, rectangle())['cycle_support_complete'])
        wire['cycle']['end_ns'] = '2000000000'
        wire['streams'][0]['offset_ns'] = '500000000'
        self.assertFalse(capture_summary(wire, meta, rectangle())['cycle_support_complete'])
        wire['streams'][0]['offset_ns'] = '0'
        # Current-style endpoint exclusion is a sample span, not a full cycle.
        wire['cycle'] = None; wire['window_ns'] = '2500000000'
        result = capture_summary(wire, meta, rectangle())
        self.assertIsNone(result['cycle_duration_s'])
        self.assertIsNone(result['strokes_per_min'])
        self.assertFalse(result['cycle_support_complete'])
        self.assertEqual(result['window_s'], 2.5)
        bad = deepcopy(wire); bad['streams'][0]['channels'][0]['unit'] = 'N'
        with self.assertRaises(ValueError): capture_summary(bad, meta, rectangle())
        bad = deepcopy(wire); bad['source_kind'] = 'measured'
        with self.assertRaises(ValueError): capture_summary(bad, meta, rectangle())
        with self.assertRaises(ValueError): capture_summary(wire, {**meta, 'sample_rate_hz': 128}, rectangle())

    def test_real_cases_keep_perturbations_distinct_from_quality_and_preserve_raw(self):
        from tools.testpack.cycles import generate_cycles
        from tools.testpack.waveforms import encode_cycle
        from tools.testpack.viewer import case_data
        cycles = {c.case_id: c for c in generate_cycles(PackConfig())}
        slow = replace(cycles['cycle-base'], case_id='cycle-slow', sample_rate_hz=64,
                       time_ns=tuple(t*2 for t in cycles['cycle-base'].time_ns))
        partial = replace(cycles['cycle-base'], case_id='cycle-partial', sample_rate_hz=256)
        cases = tuple(encode_cycle(cycles['cycle-'+name], PackConfig()) for name in ('base', 'noise', 'missing', 'clipped', 'shifted'))+(encode_cycle(slow, PackConfig()), encode_cycle(partial, PackConfig()))
        with TemporaryDirectory() as folder:
            pack = write_pack(Path(folder)/'pack', PackConfig(), cases, GENERATOR)
            before = {p: (pack.root/p).read_bytes() for c in pack.manifest['cases'] for p in c['artifact_paths']}
            results = {c['case_id']: case_data(pack, c) for c in pack.manifest['cases']}
            base, noise, missing = (results['cycle-'+n] for n in ('base', 'noise', 'missing'))
            self.assertEqual(base['capture']['device_id'], 'TESTPACK-PUMP-001')
            self.assertEqual(base['capture']['cycle_duration_s'], 8)
            self.assertEqual(base['reference'][1]['time_ns'], '7812500')
            self.assertEqual(results['cycle-slow']['reference'][1]['time_ns'], '15625000')
            self.assertEqual(results['cycle-slow']['capture']['cycle_duration_s'], 16)
            self.assertFalse(results['cycle-partial']['capture']['cycle_support_complete'])
            self.assertIsNone(results['cycle-partial']['reference_summary']['area_kj'])
            self.assertIsNone(results['cycle-partial']['reference_summary']['stroke_m'])
            self.assertEqual(results['cycle-partial']['reference_summary']['area_reason'], 'incomplete_cycle_support')
            self.assertEqual(noise['quality_summary']['status'], 'usable_samples')
            self.assertEqual(noise['scene']['perturbation'], 'noise')
            self.assertEqual(noise['reference_summary']['area_reason'], 'open_endpoints')
            self.assertEqual(missing['quality_summary']['channels'][0]['missing'], 20)
            self.assertEqual(missing['reference_summary']['area_kj'], base['reference_summary']['area_kj'])
            self.assertTrue(all(r['prediction'] == [] for r in results.values()))
            self.assertEqual(before, {p: (pack.root/p).read_bytes() for p in before})

    def test_new_helper_is_in_committed_export_allowlist(self):
        from tools.review_deploy_source import _included
        self.assertTrue(_included('tools/testpack/viewer_details.py'))

    def test_viewer_cannot_borrow_report_from_another_package(self):
        from tools.testpack.viewer import serve_viewer
        from tools.testpack.report import write_report, export_public_report
        from tests.test_testpack_contracts import example
        with TemporaryDirectory() as folder:
            root = Path(folder)
            first = write_pack(root/'a', PackConfig(), (example(),), GENERATOR)
            second = write_pack(root/'b', replace(PackConfig(), seed=42), (example(),), GENERATOR)
            run = root/'run'; run.mkdir()
            write_report(first, (), run, run_metadata={})
            report = export_public_report(run, root/'public')
            with self.assertRaisesRegex(ValueError, 'another package'):
                serve_viewer(second, report)


if __name__ == '__main__': unittest.main()
