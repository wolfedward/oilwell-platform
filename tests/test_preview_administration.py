import tempfile,unittest
from pathlib import Path
from tests.preview_support import make_source
class AdministrationPreviewTests(unittest.TestCase):
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
