import tempfile
from pathlib import Path
import unittest
from tests.preview_support import make_source,run_browser,ROOT

class AcquisitionPreviewTests(unittest.TestCase):
    def setUp(self):
        from backend.preview.dataset import PreviewDataset
        from backend.preview.repository import PreviewRepository
        from backend.preview.read_models.acquisition import register
        from backend.preview.permissions import role_permissions
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=PreviewRepository(PreviewDataset(make_source(Path(self.tmp.name))),{'data_to':'2026-09-12','snapshot_at':'2026-09-12T10:00:00+08:00'})
        register(self.repo)
        self.actor={'id':'preview-user-1','role_code':'administrator','permissions':role_permissions('administrator')}

    def test_snapshot_records_and_fixed_fixture_sources(self):
        read=lambda path:self.repo.read('/api/preview/legacy'+path,{},self.actor)
        self.assertEqual(read('/api/acquisition/stream')['items'][0]['received_at'],'2026-09-12T10:00:00+08:00')
        device=read('/api/devices/D1')
        self.assertFalse(device['hardware_connected'])
        for path in ['/api/registry/devices','/api/ingest-reliability/cases','/api/waveforms/captures']:
            rows=read(path)['items'];self.assertGreater(len(rows),0)
            self.assertTrue(all(row['source_kind']=='software_fixture' for row in rows))
        with self.assertRaises(Exception):read('/api/registry/not-registered')

    def test_eight_pages_and_subviews(self):
        from backend.preview.fixtures.acquisition import fixtures
        from backend.preview.read_models.acquisition import export_fixture_reads
        payload=export_fixture_reads(self.repo,self.actor)
        for width,height in [(1366,768),(1920,1080)]:
            result=run_browser('acquisition',width=width,height=height,data={'reads':payload,'actor':self.actor},screenshot_dir=ROOT/'.superpowers/sdd/2026-10-12-page-first-platform-preview/screens')
            self.assertNotIn('error',result,result)
            self.assertEqual(len(result['pages_checked']),8)
            self.assertEqual(result['missing_subviews'],[])
            self.assertEqual(result['page_errors'],[])
            self.assertEqual(result['unknown_reads'],[])
            self.assertEqual(result['overflow'],[])
