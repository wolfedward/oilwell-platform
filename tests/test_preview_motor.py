import unittest
from tests.preview_support import run_browser,ROOT
class MotorPreviewTests(unittest.TestCase):
    def test_motor_pages_and_restrictions(self):
        from backend.preview.fixtures.motor import fixture
        from backend.preview.permissions import role_permissions
        data=fixture();self.assertEqual(data['source_kind'],'software_fixture');self.assertFalse(data['control_enabled'])
        self.assertIsNone(data['scenarios']['missing-speed']['speed_rpm'])
        for width,height in [(1366,768),(1920,1080)]:
            result=run_browser('motor',width=width,height=height,data={'view':data,'actor':{'id':'preview-user-1','permissions':role_permissions('administrator')}},screenshot_dir=ROOT/'.superpowers/sdd/2026-10-12-page-first-platform-preview/screens')
            self.assertNotIn('error',result,result)
            self.assertEqual(result['pages_checked'],5)
            for key in ['missing','overflow','page_errors']:self.assertEqual(result[key],[],(key,result))
