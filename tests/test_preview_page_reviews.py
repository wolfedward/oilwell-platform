import tempfile,unittest
from pathlib import Path
from tests.preview_support import run_browser,ROOT
class PageReviewsTests(unittest.TestCase):
    def test_open_interface_version_change_invalidates_prior_confirmation(self):
        from backend.preview.state import PreviewState
        from backend.preview.page_reviews import PageReviews
        from backend.preview.catalog import load_catalog
        with tempfile.TemporaryDirectory() as tmp:
            state=PreviewState(Path(tmp)/'state.db');catalog=load_catalog()
            old=PageReviews(state,catalog,open_interface_version='A')
            old.save_page_review({'id':'a','role_code':'administrator'},'open-interface','A',0,'approved','确认旧版本')
            current=PageReviews(state,catalog,open_interface_version='B').get_page_reviews('B')[-1]
            self.assertEqual(current['decision'],'pending');self.assertEqual(current['comment'],'确认旧版本')

    def test_persistence_versions_and_no_automatic_approval(self):
        from backend.preview.state import PreviewState
        from backend.preview.page_reviews import PageReviews
        from backend.preview.catalog import load_catalog
        from backend.preview.errors import PreviewError
        with tempfile.TemporaryDirectory() as tmp:
            state=PreviewState(Path(tmp)/'state.db');catalog=load_catalog();reviews=PageReviews(state,catalog)
            rows=reviews.get_page_reviews('1');self.assertEqual(len(rows),56);self.assertTrue(all(r['decision']=='pending' for r in rows))
            a={'id':'preview-user-1','role_code':'administrator'}
            saved=reviews.save_page_review(a,'dashboard','1',0,'approved','页面内容确认')
            with self.assertRaises(PreviewError):reviews.save_page_review(a,'dashboard','1',0,'changes_requested','stale')
            state.reset('RESET_REHEARSALS');self.assertEqual(PageReviews(state,catalog).get_page_reviews('1')[0]['decision'],'approved')
            catalog[0]['content_version']='2';changed=PageReviews(state,catalog).get_page_reviews('2')[0];self.assertEqual(changed['decision'],'pending');self.assertEqual(changed['version'],1)
            with self.assertRaises(PreviewError):reviews.save_page_review(a,'not-a-page','1',0,'approved','bad')
            for width,height in [(1366,768),(1920,1080)]:
                r=run_browser('reviews',width=width,height=height,data={'rows':rows},screenshot_dir=ROOT/'.superpowers/sdd/2026-10-12-page-first-platform-preview/screens');self.assertNotIn('error',r,r);self.assertEqual(r['missing'],[]);self.assertEqual(r['page_errors'],[])
