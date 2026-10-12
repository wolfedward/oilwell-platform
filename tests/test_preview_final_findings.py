import math,tempfile,unittest
from pathlib import Path
from tests.preview_support import make_source

class FinalFindingsTests(unittest.TestCase):
    def setUp(self):
        from backend.preview.dataset import PreviewDataset
        from backend.preview.repository import PreviewRepository
        from backend.preview.read_models import register_all
        from backend.preview.state import PreviewState
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        self.repo=PreviewRepository(PreviewDataset(make_source(root)),{'data_to':'2026-09-12'})
        self.repo.state=PreviewState(root/'state.db');register_all(self.repo)
        from backend.preview.permissions import role_permissions
        self.author={'id':'author','role_code':'administrator','permissions':role_permissions('administrator')};self.reviewer={'id':'reviewer','role_code':'reviewer'}
    def save(self,actor,key,action,object_id,version,values):
        from backend.preview.rehearsals import save_rehearsal
        return save_rehearsal(self.repo.state,actor,key,action,object_id,version,values)
    def read(self,path,q=None):return self.repo.read('/api/preview/legacy'+path,q or {},self.author)
    def test_created_work_order_has_resolvable_identity_and_separate_history(self):
        row=self.save(self.author,'create-order-01','create','/api/work-orders',0,{'title':'预览检修','well_id':'W1','kind':'inspection'})
        self.assertTrue(row['id'].startswith('preview-'))
        item=self.read('/api/work-orders/'+row['id']);self.assertEqual(item['title'],'预览检修')
        self.assertEqual(self.read('/api/work-orders')['items'][0]['id'],row['id'])
        obj='/api/work-orders/'+row['id']
        self.save(self.author,'order-process01','edit',obj,1,{'target':'processing','comment':'处理记录'})
        self.save(self.author,'order-submit001','submit',obj,2,{'target':'review','comment':'申请独立验收'})
        from backend.preview.errors import PreviewError
        with self.assertRaises(PreviewError):self.save(self.author,'order-selfclose','close',obj,3,{'target':'closed'})
        self.save(self.reviewer,'order-close0001','close',obj,3,{'target':'closed','comment':'独立复核通过'})
        self.assertEqual(self.read('/api/work-orders/'+row['id'])['status'],'closed')
        self.assertEqual(len(self.read('/api/work-orders/'+row['id'])['logs']),4)
        self.assertEqual(self.read('/api/work-orders/'+row['id']+'/trace')['work_order_id'],row['id'])
        self.assertEqual(self.repo.dataset.scalar('SELECT count(*) FROM work_orders'),0)
    def test_strategy_submit_approval_visible_across_pages_without_source_changes(self):
        obj='/api/strategies/preview-STR-002'
        self.save(self.author,'strategy-submit','submit',obj,0,{'comment':'提交演练'})
        pending=self.read('/api/strategies',{'status':'pending_approval'})
        self.assertEqual(pending['total'],1)
        self.save(self.reviewer,'strategy-approve','approve',obj,1,{'comment':'独立复核通过'})
        self.assertEqual(self.read('/api/strategies/preview-STR-002')['status'],'approved')
        self.assertEqual(self.read('/api/strategies',{'status':'pending_approval'})['total'],0)
        self.assertEqual(self.repo.dataset.scalar('SELECT count(*) FROM energy_strategies'),0)
    def test_card_comparison_is_calculated_from_current_points(self):
        from backend.preview.fixtures.dynagraphs import cards
        for card in cards().values():
            errors=[p['load_kn']-s['load'] for p,s in zip(card['converted_points'],card['sensor_points'])]
            self.assertAlmostEqual(card['comparison']['rmse_kn'],math.sqrt(sum(x*x for x in errors)/len(errors)),places=8)
            self.assertAlmostEqual(card['comparison']['max_error_kn'],max(abs(x) for x in errors),places=8)
            self.assertIsNone(card['latest_confidence'])

    def test_scoped_detail_and_binary_exports(self):
        import csv,io,hashlib
        from backend.preview.repository import Download
        from backend.preview.fixtures.acquisition import uid
        detail=self.read('/api/wells/W1',{'from':'2026-09-10','to':'2026-09-10'})
        self.assertEqual(detail['latest_production']['day'],'2026-09-10')
        self.assertEqual(len(detail['production_history']),1)
        result=self.read('/api/wells.csv',{'keyword':'ST-001'})
        self.assertIsInstance(result,Download)
        rows=list(csv.DictReader(io.StringIO(result.body.decode('utf-8-sig'))))
        self.assertEqual([r['code'] for r in rows],['ST-001'])
        raw=self.read('/api/waveforms/facts/'+uid('fact')+'/raw')
        fact=self.read('/api/waveforms/captures/'+uid('capture'),{'view':'facts'})['items'][0]
        self.assertIsInstance(raw,Download)
        self.assertEqual(hashlib.sha256(raw.body).hexdigest(),fact['payload_sha256'])

    def test_client_versions_retries_and_quality_plot(self):
        from tests.preview_support import run_browser
        result=run_browser('final_interactions')
        self.assertEqual(result.get('error'),None,result)
        self.assertTrue(result['same_retry_body'])
        self.assertEqual(result['expected_version'],2)
        self.assertEqual(result['state_gets'],0)
        self.assertEqual(result['quality_points'],3)
        self.assertEqual(result['quality_segments'],3)

    def test_metering_retains_estimate_and_factors(self):
        import sqlite3,json
        from contextlib import closing
        with closing(sqlite3.connect(self.repo.dataset.path)) as db,db:
            db.execute("INSERT INTO model_runs(id,model_name) VALUES('MR1','existing-model')")
            db.execute("INSERT INTO production_estimates(well_id,dynagraph_id,model_run_id,theoretical_m3_day,estimated_m3_day,factors_json) VALUES('W1','DG1','MR1',20,13,?)",(json.dumps({'pump_diameter_mm':44,'fillage':.65}),))
        result=self.repo.read('/api/preview/views/dynagraphs',{'well_id':'W1'},self.author)
        self.assertEqual(result['estimates'][0]['estimated_m3_day'],13)
        self.assertEqual(result['estimates'][0]['factors']['pump_diameter_mm'],44)
        self.assertEqual(result['estimates'][0]['daily_fluid'],12.8)
        self.assertIsNone(result['estimates'][0]['validated_error_rate'])

    def test_motor_and_haulage_have_missing_fields_and_evidence(self):
        from backend.preview.fixtures.motor import fixture
        from backend.preview.fixtures.monitoring import supplementary
        data=fixture();names={r['name'] for r in data['faults']}
        self.assertTrue({'过压测试','欠压测试','过流测试','堵转测试'}<=names)
        self.assertTrue(all(r['sample_at'] and r['quality'] and 'power_kw' in r and 'speed_rpm' in r for r in data['faults']))
        self.assertTrue(data['event_waveform']);self.assertIn('通信中断',str(data['event_trace']))
        rows=supplementary({'id':'W1','status':'running'},'2026-09-12')['haulage']
        total=0;prior=8
        for row in rows:
            total+=row['unloaded_m3']
            self.assertAlmostEqual(row['daily_unloaded_m3'],total)
            self.assertAlmostEqual(row['volume_m3'],prior+row['loaded_m3']-row['unloaded_m3'])
            prior=row['volume_m3']
        self.assertTrue(any(r['alarm']!='normal' for r in rows))
