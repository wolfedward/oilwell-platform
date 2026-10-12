from contextlib import closing
import sqlite3,tempfile,unittest
from pathlib import Path
from tests.preview_support import make_source,ROOT,run_browser

class WorkflowPreviewTests(unittest.TestCase):
    def test_eleven_pages_report_scope_and_privacy(self):
        from backend.preview.dataset import PreviewDataset
        from backend.preview.repository import PreviewRepository
        from backend.preview.read_models import register_all
        from backend.preview.permissions import role_permissions
        from backend.preview.read_models.workflows import export_reads,csv_report
        with tempfile.TemporaryDirectory() as tmp:
            path=make_source(Path(tmp))
            with closing(sqlite3.connect(path)) as db,db:
                db.executescript("""INSERT INTO device_commands(id,device_id,command_type,payload,status,signature) VALUES('C1','D1','set_intermittent_cycle','{"on_minutes":60,"off_minutes":45}','success','SECRET-SIGNATURE');
                INSERT INTO command_attempts(command_id,attempted_at,attempt_number,state,receipt_at,result_detail,status) VALUES('C1','2026-09-12T01:00:00Z',1,'success','2026-09-12T01:00:00Z','{"reason":"历史测试回执"}','success');
                INSERT INTO alarms(id,well_id,alarm_type,severity,command_id) VALUES('A1','W1','device_fault','high','C1');
                INSERT INTO work_orders(id,well_id,alarm_id,title,due_at,assignee_id) VALUES('O1','W1','A1','现场巡检','2026-09-11T00:00:00+08:00',1);
                INSERT INTO audit_logs(actor_id,action,object_type,object_id) VALUES(1,'alarm.create','alarm','A1');""")
            repo=PreviewRepository(PreviewDataset(path),{'data_to':'2026-09-12'});register_all(repo)
            actor={'id':'preview-user-1','role_code':'administrator','permissions':role_permissions('administrator')};reads=export_reads(repo,actor)
            self.assertNotIn('SECRET-SIGNATURE',str(reads));self.assertNotIn('TEST-SECRET',str(reads))
            self.assertEqual(len(reads['/api/acceptance/evidence']['requirements']),12)
            self.assertTrue(reads['/api/work-orders/O1']['overdue'])
            self.assertTrue(csv_report(repo.dataset,'production',{'well_id':'W1'}).startswith(b'\xef\xbb\xbf'))
            for width,height in [(1366,768),(1920,1080)]:
                r=run_browser('workflows',width=width,height=height,data={'reads':reads,'actor':actor},screenshot_dir=ROOT/'.superpowers/sdd/2026-10-12-page-first-platform-preview/screens');self.assertNotIn('error',r,r)
                self.assertEqual(r['pages_checked'],11)
                for key in ['missing','unknown_reads','overflow','page_errors']:self.assertEqual(r[key],[],(key,r))
