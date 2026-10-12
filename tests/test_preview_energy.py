import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tests.preview_support import make_source,run_browser,ROOT

class EnergyPreviewTests(unittest.TestCase):
    def test_six_pages_and_no_optimization(self):
        from backend.preview.dataset import PreviewDataset
        from backend.preview.repository import PreviewRepository
        from backend.preview.read_models.energy import register
        from backend.preview.fixtures.energy import strategies
        from backend.preview.permissions import role_permissions
        with tempfile.TemporaryDirectory() as tmp,patch('backend.services.strategies.fit_recovery_curve',side_effect=AssertionError('engine')):
            repo=PreviewRepository(PreviewDataset(make_source(Path(tmp))),{});register(repo)
            actor={'id':'preview-user-1','role_code':'administrator','permissions':role_permissions('administrator')}
            paths=['/api/strategies','/api/reports/energy','/api/preview/views/energy',*['/api/strategies/'+r['id'] for r in strategies()]]
            reads={p:repo.read(p if p.startswith('/api/preview/') else '/api/preview/legacy'+p,{},actor) for p in paths}
            self.assertEqual(reads['/api/strategies']['total'],2)
            for r in reads['/api/preview/views/energy']['effects']:self.assertLessEqual(r['baseline_to'],r['evaluation_from']);self.assertFalse(r['field_validation'])
            for width,height in [(1366,768),(1920,1080)]:
                result=run_browser('energy',width=width,height=height,data={'reads':reads,'actor':actor},screenshot_dir=ROOT/'.superpowers/sdd/2026-10-12-page-first-platform-preview/screens')
                self.assertNotIn('error',result,result)
                for key in ['missing','page_errors','unknown_reads','overflow']:self.assertEqual(result[key],[],(key,result))
                self.assertEqual(result['pages_checked'],6)
