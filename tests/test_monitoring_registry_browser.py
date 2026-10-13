import unittest
from tests.monitoring_browser_support import run_browser


class MonitoringRegistryBrowserTests(unittest.TestCase):
    def test_live_registry_pages_use_versioned_forms_and_real_routes(self):
        for width in (1366,1920):
            with self.subTest(width=width):
                result = run_browser('registry',width=width)
                self.assertNotIn('error',result)
                self.assertEqual(result['page_errors'],[])
                for key in ('late_refresh_ignored','created_once','same_retry_body','renamed','archive_rejected','states_separate',
                            'binding_updated','old_form_disposed','modbus_readonly','mqtt_without_registers',
                            'history_visible','export_context_matches','details_links','long_ids_safe'):
                    self.assertTrue(result[key],key)
                self.assertEqual(result['self_review_successes'],0)
                self.assertEqual(result['assignment_state'],'suspended')
                self.assertEqual(result['submitted_edge_config_fields'],[])
                self.assertEqual(result['forbidden_requests'],[])
