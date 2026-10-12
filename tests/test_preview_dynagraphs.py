import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tests.preview_support import make_source,run_browser,ROOT

class DynagraphPreviewTests(unittest.TestCase):
    def setUp(self):
        from backend.preview.dataset import PreviewDataset
        from backend.preview.repository import PreviewRepository
        from backend.preview.read_models import monitoring,acquisition,dynagraphs
        from backend.preview.permissions import role_permissions
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ds=PreviewDataset(make_source(Path(self.tmp.name)))
        self.repo=PreviewRepository(self.ds,{'data_to':'2026-09-12'})
        for module in (monitoring,acquisition,dynagraphs):module.register(self.repo)
        self.actor={'id':'preview-user-1','role_code':'administrator','permissions':role_permissions('administrator')}

    def test_original_card_unchanged_and_fixture_profiles_distinct(self):
        from backend.preview.fixtures.dynagraphs import cards
        before=self.ds.query_one("SELECT sample_data FROM dynagraphs WHERE id='DG1'")['sample_data']
        data=self.repo.read('/api/preview/legacy/api/dynagraphs/DG1',{},self.actor)
        self.assertEqual(data['sample_data'],json.loads(before))
        self.assertEqual(before,self.ds.query_one("SELECT sample_data FROM dynagraphs WHERE id='DG1'")['sample_data'])
        profiles=cards();self.assertGreaterEqual(len(profiles),22)
        span=lambda code:max(x['load_kn'] for x in profiles[code]['conversion']['points'])-min(x['load_kn'] for x in profiles[code]['conversion']['points'])
        self.assertGreater(span('normal'),20);self.assertLess(span('rod_break'),5)
        self.assertTrue(all(v['source_kind']=='software_fixture' for v in profiles.values()))

    def test_ten_pages_details_curves_and_review(self):
        from backend.preview.read_models.dynagraphs import export_fixture_reads
        payload=export_fixture_reads(self.repo,self.actor)
        for width,height in [(1366,768),(1920,1080)]:
            result=run_browser('dynagraphs',width=width,height=height,data={'reads':payload,'actor':self.actor},screenshot_dir=ROOT/'.superpowers/sdd/2026-10-12-page-first-platform-preview/screens')
            self.assertNotIn('error',result,result)
            self.assertEqual(len(result['pages_checked']),10)
            self.assertEqual(result['missing'],[])
            self.assertEqual(result['unknown_reads'],[])
            self.assertEqual(result['page_errors'],[])
            self.assertEqual(result['overflow'],[])
