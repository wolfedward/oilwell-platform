import unittest
from tests.monitoring_browser_support import run_browser


class MonitoringQualityBrowserTests(unittest.TestCase):
    def test_acquisition_and_three_quality_scopes(self):
        for width in (1366,1920):
            with self.subTest(width=width):
                result=run_browser('quality',width=width)
                self.assertNotIn('error',result)
                self.assertEqual(result['page_errors'],[])
                self.assertEqual(result['quality_view_count'],3)
                self.assertEqual(result['forbidden_write_requests'],[])
                self.assertEqual(result['telemetry_before_close'],result['telemetry_after_close'])
                self.assertEqual(result['raw_requests_without_permission'],0)
                self.assertEqual(result['actions'],['ack','close','note','reopen'])
                for key in ('separate_states','mixed_wells','detail_context','unknown_not_zero','unresolved_scope',
                            'separate_totals','same_retry','conflict_confirmed','denial_stops','raw_safe',
                            'raw_denied','audit_visible','empty_state','long_content_safe'):
                    self.assertTrue(result[key],key)
