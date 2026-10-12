import tempfile,unittest
from pathlib import Path
from tests.preview_support import make_source
class AdministrationPreviewTests(unittest.TestCase):
    def test_pages_use_verified_immutable_snapshot_evidence_without_rescanning(self):
        from unittest.mock import patch
        from backend.preview.dataset import PreviewDataset
        from backend.preview.repository import PreviewRepository
        from backend.preview.read_models.administration import register
        with tempfile.TemporaryDirectory() as tmp:
            repo=PreviewRepository(PreviewDataset(make_source(Path(tmp))),{'snapshot_integrity':'ok','snapshot_foreign_key_violations':0,'snapshot_verified_at':'2026-10-12T06:00:00Z'});register(repo)
            with patch.object(repo.dataset,'scalar',side_effect=AssertionError('page must not rescan a GB-sized snapshot')):
                result=repo.read('/api/preview/legacy/api/acceptance/evidence',{}, {})
            self.assertEqual(result['integrity']['database'],'ok')
            self.assertEqual(result['integrity']['verified_at'],'2026-10-12T06:00:00Z')
    def test_non_admin_read_and_write_rejected(self):
        from backend.preview.dataset import PreviewDataset
        from backend.preview.repository import PreviewRepository
        from backend.preview.read_models.administration import register
        from backend.preview.permissions import role_permissions
        from backend.preview.errors import PreviewError
        from backend.preview.state import PreviewState
        from backend.preview.rehearsals import save_rehearsal
        with tempfile.TemporaryDirectory() as tmp:
            repo=PreviewRepository(PreviewDataset(make_source(Path(tmp))),{});register(repo)
            a={'id':'p','role_code':'production_engineer','permissions':role_permissions('production_engineer')}
            with self.assertRaises(PreviewError):repo.read('/api/preview/legacy/api/users',{},a)
            with self.assertRaises(PreviewError):save_rehearsal(PreviewState(Path(tmp)/'state.db'),a,'request-123','create','/api/users',0,{'display_name':'x'})
