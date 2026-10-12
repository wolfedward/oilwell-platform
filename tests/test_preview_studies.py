import tempfile,unittest
from pathlib import Path
from tests.preview_support import make_source,run_browser,ROOT

class StudiesPreviewTests(unittest.TestCase):
    def test_fixed_studies_and_browser(self):
        from backend.preview.fixtures.studies import records
        from backend.preview.dataset import PreviewDataset
        from backend.preview.repository import PreviewRepository
        from backend.preview.read_models.studies import register
        from backend.preview.permissions import role_permissions
        rows=records();self.assertEqual(len({r['kind'] for r in rows}),7)
        self.assertTrue(all(not r['field_validation'] and not r['control_eligible'] for r in rows))
        self.assertIn('no_feasible_candidate',{r['status'] for r in rows})
        with tempfile.TemporaryDirectory() as tmp:
            repo=PreviewRepository(PreviewDataset(make_source(Path(tmp))),{});register(repo)
            actor={'id':'preview-user-1','role_code':'administrator','permissions':role_permissions('administrator')}
            reads={'/api/production-studies':repo.read('/api/preview/legacy/api/production-studies',{},actor)}
            reads.update({'/api/production-studies/'+r['id']:repo.read('/api/preview/legacy/api/production-studies/'+r['id'],{},actor) for r in rows})
            for width,height in [(1366,768),(1920,1080)]:
                result=run_browser('studies',width=width,height=height,data={'reads':reads,'actor':actor},screenshot_dir=ROOT/'.superpowers/sdd/2026-10-12-page-first-platform-preview/screens')
                self.assertNotIn('error',result,result);self.assertEqual(result['pages_checked'],5)
                for key in ['missing','unknown_reads','page_errors','overflow']:self.assertEqual(result[key],[],(key,result))
