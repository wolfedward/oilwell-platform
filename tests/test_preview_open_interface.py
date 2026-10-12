import unittest,zipfile
from tests.preview_support import ROOT
class OpenInterfacePreviewTests(unittest.TestCase):
    def test_only_public_kit_and_hardware_anchor(self):
        from tools.build_open_interface_kit import PUBLIC_FILES
        from backend.preview.page_reviews import OPEN_INTERFACE
        self.assertEqual(OPEN_INTERFACE['id'],'open-interface')
        folder=ROOT/'assets/open-interface';self.assertIn('id="hardware"',(folder/'index.html').read_text(encoding='utf-8'))
        with zipfile.ZipFile(folder/'open-interface-r1.zip') as archive:
            self.assertEqual(set(archive.namelist()),set(PUBLIC_FILES))
            for name in PUBLIC_FILES:self.assertEqual(archive.read(name),(folder/name).read_bytes())
