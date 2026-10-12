import unittest
from tests.preview_support import run_browser


class PreviewShellTests(unittest.TestCase):
    def test_navigation_restores_parameters_and_ignores_late_pages(self):
        for width,height in [(1366,768),(1920,1080)]:
            with self.subTest(width=width):
                result=run_browser('shell',width=width,height=height)
                self.assertEqual(result['page_errors'],[])
                self.assertEqual(result['catalog_pages'],55)
                self.assertEqual(result['groups'],9)
                self.assertTrue(result['legacy_query_roundtrip'])
                self.assertFalse(result['late_response_replaced_current'])
                self.assertFalse(result['horizontal_overflow'])
                self.assertTrue(result['preview_notice'])
