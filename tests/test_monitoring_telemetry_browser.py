import unittest
from tests.monitoring_browser_support import run_browser


class MonitoringTelemetryBrowserTests(unittest.TestCase):
    def test_latest_history_and_precision_in_real_browser(self):
        for width in (1366,1920):
            with self.subTest(width=width):
                result=run_browser('telemetry',width=width)
                self.assertNotIn('error',result)
                self.assertEqual(result['page_errors'],[])
                self.assertEqual(result['refresh_choices'],[5,10,30,60])
                self.assertEqual(result['default_interval'],10000)
                self.assertEqual(result['hover_value'],'12.35 kN')
                self.assertEqual(result['stored_value'],12.34567)
                self.assertIsNone(result['missing_value'])
                self.assertEqual(result['requests_after_dispose'],0)
                self.assertEqual(result['line_segments'],2)
                self.assertEqual(result['writes'],0)
                for key in ('same_code_separate','bad_visible','paused_cutoff','request_cancelled','keyboard_value',
                            'hidden_markers','uncertain_marked','context_export','filter_resets_context',
                            'conflict_no_export','truncated_distinct_total','raw_precision_visible','no_overflow'):
                    self.assertTrue(result[key],key)
