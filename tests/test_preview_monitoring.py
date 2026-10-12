import tempfile
from pathlib import Path
import unittest
from tests.preview_support import make_source,run_browser,ROOT

IDS=['dashboard','wells','well-detail','production-monitor','well-realtime','well-history','well-status','fluid-level','haulage-monitor']


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        from backend.preview.dataset import PreviewDataset
        from backend.preview.repository import PreviewRepository
        from backend.preview.read_models.monitoring import register
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.repo=PreviewRepository(PreviewDataset(make_source(Path(self.temp.name))),{'data_from':'2026-09-10','data_to':'2026-09-12'})
        register(self.repo);self.actor={'id':'preview-user-1','role_code':'administrator','permissions':['wells.read','telemetry.read','dashboard.read']}

    def read(self,path,query=None):return self.repo.read(path,query or {},self.actor)

    def test_production_totals_share_same_well_and_time_scope(self):
        dashboard=self.read('/api/preview/legacy/api/dashboard',{'well_id':'W1','from':'2026-09-11','to':'2026-09-12'})
        data=self.read('/api/preview/views/monitoring',{'well_id':'W1','from':'2026-09-11','to':'2026-09-12'})
        self.assertEqual(dashboard['production']['daily_fluid'],12.8)
        self.assertEqual([p['daily_fluid'] for p in dashboard['trends']],[13.4,12.8])
        self.assertEqual(data['production'][-1]['daily_fluid'],12.8)
        self.assertEqual(data['production'][0]['day'],'2026-09-11')
        self.assertTrue(all(r['source_kind']=='software_fixture' for r in data['fluid_level']))
        self.assertEqual(data['fluid_level'][0]['unit'],'m')
        self.assertNotEqual(data['fluid_level'][0]['value'],data['production'][0]['daily_fluid'])
        self.assertEqual([w['id'] for w in data['scope_wells']],['W1'])
        self.assertEqual(len(data['wells']),2)
        self.assertEqual(data['rankings'][0]['well_id'],'W1')

    def test_empty_scope_and_missing_measurements_are_not_zero(self):
        data=self.read('/api/preview/views/monitoring',{'well_id':'W2','from':'2020-01-01','to':'2020-01-02'})
        self.assertEqual(data['production'],[])
        self.assertEqual(data['telemetry'],[])

    def test_dashboard_has_scoped_energy_summary_and_filter_identifiers(self):
        data=self.read('/api/preview/legacy/api/dashboard',{'well_id':'W1'})
        self.assertEqual(data['energy']['comparable_strategy_count'],0)
        self.assertIsNone(data['energy']['saving_rate'])
        self.assertGreater(data['trend_metadata']['fluid_range_ratio'],0)
        wells=self.read('/api/preview/legacy/api/wells',{'page_size':100})['items']
        self.assertTrue(all(w['unit_id'] and w['group_id'] for w in wells))

    def test_bounded_telemetry_keeps_latest_window(self):
        from backend.preview.read_models.monitoring import rows_for_wells
        from contextlib import closing
        import sqlite3
        from datetime import datetime,timedelta
        with closing(sqlite3.connect(Path(self.temp.name)/'source.db')) as db,db:
            db.executemany("INSERT INTO telemetry(well_id,device_id,metric_code,value,unit,measured_at,status) VALUES('W1','D1','power_kw',?,'kW',?,'valid')",[(i,(datetime(2026,9,12,12)+timedelta(seconds=i)).isoformat()) for i in range(605)])
        data=rows_for_wells(self.repo.dataset,'telemetry',['W1'],'measured_at','2026-09-12','2026-09-12',600)
        self.assertEqual(data[-1]['value'],604)

    def test_nine_pages_have_content_at_both_viewports(self):
        payload={
            'monitoring':self.read('/api/preview/views/monitoring',{'well_id':'W1'}),
            'dashboard':self.read('/api/preview/legacy/api/dashboard'),
            'wells':self.read('/api/preview/legacy/api/wells',{'page':1,'page_size':100}),
            'detail':self.read('/api/preview/legacy/api/wells/W1'),
            'telemetry':self.read('/api/preview/legacy/api/wells/W1/telemetry',{'interval':'1h'}),
        }
        for width,height in [(1366,768),(1920,1080)]:
            with self.subTest(width=width):
                result=run_browser('monitoring',width=width,height=height,data=payload,screenshot_dir=ROOT/'.superpowers/sdd/2026-10-12-page-first-platform-preview/screens')
                self.assertEqual(result.get('error'),None,result)
                self.assertEqual(result['pages_checked'],IDS)
                self.assertEqual(result['page_errors'],[])
                self.assertEqual(result['empty_pages'],[])
                self.assertEqual(result['overflow'],[])
                self.assertEqual(result['missing'],[])
