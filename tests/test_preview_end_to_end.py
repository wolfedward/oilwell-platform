import tempfile,threading,unittest
from pathlib import Path
from tests.preview_support import make_source,ROOT,read_source_rows
class PreviewEndToEndTests(unittest.TestCase):
    def test_well_list_defaults_support_detail_entry(self):
        from backend.preview.dataset import PreviewDataset
        from backend.preview.repository import PreviewRepository
        from backend.preview.read_models.monitoring import register
        with tempfile.TemporaryDirectory() as tmp:
            r=PreviewRepository(PreviewDataset(make_source(Path(tmp))),{});register(r)
            self.assertEqual(r.read('/api/preview/legacy/api/wells',{'page_size':'1'},{} )['page'],1)
    def test_full_http_shell_all_pages(self):
        from tools.page_preview import prepare
        from tools.verify_page_preview import browser_check
        from backend.preview.server import create_preview_server
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/'install';source=make_source(root);before=read_source_rows(source);prepare(source,target,code_source=ROOT,revision=None)
            server=create_preview_server(target,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                result=browser_check(target,server.server_port,screenshot_dir=ROOT/'.superpowers/sdd/2026-10-12-page-first-platform-preview/integration-screens')
                self.assertEqual(len(result['pages']),110)
                self.assertEqual(result['open_interface_entries'],1)
                self.assertEqual(read_source_rows(source),before)
                for key in ['failures','outside_preview_requests','page_errors','overflow']:self.assertEqual(result[key],[],(key,result))
            finally:server.shutdown();server.server_close();thread.join(5)
