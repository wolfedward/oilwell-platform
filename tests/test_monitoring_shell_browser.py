import unittest
from tests.monitoring_browser_support import run_browser
from tests import test_monitoring_session as sessions

CONNECTED = {'wells','well-detail','field-registry','channel-mapping','acquisition','data-quality','well-realtime','well-history'}


class MonitoringShellTests(unittest.TestCase):
    def test_independent_shell_all_routes_and_disposal_at_both_widths(self):
        for width in (1366,1920):
            with self.subTest(width=width):
                result = run_browser('shell', width=width)
                self.assertNotIn('error', result)
                self.assertEqual(result['page_errors'], [])
                self.assertEqual(result['catalog_count'],55)
                self.assertTrue(result['catalog_matches'])
                self.assertEqual(set(result['connected_ids']), CONNECTED)
                self.assertEqual(result['forbidden_requests'], [])
                self.assertFalse(result['role_switch'])
                self.assertFalse(result['horizontal_overflow'])
                self.assertTrue(result['pending_links'])
                self.assertTrue(result['legacy_well_warning'])
                self.assertTrue(result['forbidden_state'])
                self.assertFalse(result['late_response_replaced_new_route'])
                self.assertTrue(result['unsafe_calls_blocked'])
                self.assertTrue(result['environment_visible'])
                self.assertEqual(result['late_auth_events'], 0)
                self.assertTrue(result['login_error_preserves_form'])
                self.assertTrue(result['successful_login_starts_session_checks'])


class MonitoringStaticTests(unittest.TestCase):
    setUp = sessions.MonitoringSessionTests.setUp
    close = sessions.MonitoringSessionTests.close

    def test_monitoring_root_is_independent_and_static_traversal_is_refused(self):
        import http.client
        for path, status, marker in [('/',200,b'frontend/workbench/app.js'),
            ('/workbench.html',200,b'frontend/workbench/app.js'), ('/frontend/../../server.py',404,None)]:
            conn = http.client.HTTPConnection('127.0.0.1',self.server.server_port)
            conn.request('GET',path)
            response = conn.getresponse(); body = response.read(); conn.close()
            self.assertEqual(response.status,status)
            if marker: self.assertIn(marker,body)
