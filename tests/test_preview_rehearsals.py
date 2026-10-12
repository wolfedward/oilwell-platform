import tempfile
from pathlib import Path
import unittest
from tests.preview_support import run_browser


class RehearsalTests(unittest.TestCase):
    def test_idempotent_versioned_events_and_independent_review(self):
        from backend.preview.state import PreviewState
        from backend.preview.rehearsals import save_rehearsal
        from backend.preview.errors import PreviewError
        with tempfile.TemporaryDirectory() as temp:
            state=PreviewState(Path(temp)/'state.db')
            author={'id':'preview-user-1','role_code':'administrator'}
            other={'id':'preview-user-5','role_code':'reviewer'}
            args=(author,'request-0001','submit','S1',0,{'name':'试验策略'})
            first=save_rehearsal(state,*args)
            self.assertEqual(first,save_rehearsal(state,*args))
            self.assertEqual(first['mode'],'rehearsal')
            with self.assertRaises(PreviewError) as caught: save_rehearsal(state,author,'request-0001','submit','S1',0,{'name':'不同内容'})
            self.assertEqual(caught.exception.status,409)
            with self.assertRaises(PreviewError) as caught: save_rehearsal(state,author,'request-0002','approve','S1',1,{})
            self.assertEqual(caught.exception.status,403)
            approved=save_rehearsal(state,other,'request-0003','approve','S1',1,{'comment':'复核通过'})
            self.assertEqual(approved['version'],2)
            with self.assertRaises(PreviewError) as caught: save_rehearsal(state,author,'request-0004','edit','S1',1,{})
            self.assertEqual(caught.exception.status,409)
            self.assertEqual(len(PreviewState(state.path).history('S1')),2)

    def test_roles_unknown_actions_and_historical_replay_rejected(self):
        from backend.preview.state import PreviewState
        from backend.preview.rehearsals import save_rehearsal
        from backend.preview.errors import PreviewError
        with tempfile.TemporaryDirectory() as temp:
            state=PreviewState(Path(temp)/'state.db')
            reader={'id':'preview-user-5','role_code':'reviewer'}
            for action in ['edit','send','retry','write_register','unknown']:
                with self.subTest(action=action),self.assertRaises(PreviewError):
                    save_rehearsal(state,reader,'request-'+action,action,'OLD-COMMAND',0,{})
            self.assertEqual(state.history('OLD-COMMAND'),[])

    def test_admin_permissions_are_union_and_other_roles_not_admin(self):
        from backend.preview.permissions import role_permissions
        roles=['production_engineer','algorithm_engineer','maintenance_engineer','reviewer']
        admin=set(role_permissions('administrator'))
        self.assertTrue(set.union(*(set(role_permissions(r)) for r in roles))<=admin)
        self.assertNotIn('users.manage',role_permissions('reviewer'))
        self.assertIn('studies.create',role_permissions('algorithm_engineer'))

    def test_modal_cancel_and_submit_are_explicit(self):
        r=run_browser('rehearsals')
        self.assertEqual(r['page_errors'],[])
        self.assertEqual(r['cancelled_writes'],0)
        self.assertEqual(r['submitted_writes'],1)
        self.assertTrue(r['not_sent_notice'])
        self.assertEqual(r['writes_after_cancelled_context'],0)

    def test_reset_only_clears_rehearsals_with_explicit_confirmation(self):
        from backend.preview.state import PreviewState
        from backend.preview.rehearsals import save_rehearsal
        from backend.preview.errors import PreviewError
        with tempfile.TemporaryDirectory() as temp:
            state=PreviewState(Path(temp)/'state.db')
            with state.connection() as db:
                db.execute('CREATE TABLE page_reviews(id TEXT)')
                db.execute("INSERT INTO page_reviews VALUES('dashboard')")
            save_rehearsal(state,{'id':'preview-user-1','role_code':'administrator'},'reset-test-1','edit','W1',0,{})
            with self.assertRaises(PreviewError):state.reset('')
            state.reset('RESET_REHEARSALS')
            self.assertEqual(state.history(),[])
            with state.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM page_reviews').fetchone()[0],1)
