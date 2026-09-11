"""Legacy strategy evidence must survive restart and render complete engineering views."""
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest

from backend.audit import AuditService
from backend.database import Database
from backend.seed import seed_database
from backend.services.strategies import StrategyService

ROOT = Path(__file__).resolve().parent.parent


class StrategyRecoveryRichnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Database(Path(cls.temporary.name) / "platform.db")
        cls.database.migrate()
        seed_database(cls.database)
        cls.service = StrategyService(cls.database, AuditService(cls.database))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_existing_legacy_version_has_complete_recovery_and_prediction(self):
        raw = self.database.scalar("SELECT configuration FROM strategy_versions WHERE id='SV-007'")
        detail = self.service.detail("ES-007")
        config = detail["configuration"]
        self.assertIn("selected_candidate", config)
        selected = config["selected_candidate"]
        for key in ("on_minutes", "off_minutes", "saving_rate", "predicted_liquid_m3"):
            self.assertIsInstance(selected[key], (int, float))
        self.assertEqual(config["recovery_test_id"], "RT-007")
        self.assertEqual(config["strategy_version_id"], "SV-007")
        self.assertTrue(config["recovery_tested_at"])
        model = config["recovery_model"]
        for key in ("tau_minutes", "r_squared", "baseline", "capacity", "observation_count"):
            self.assertTrue(math.isfinite(model[key]))
        self.assertGreater(model["r_squared"], .99)
        self.assertLess(model["r_squared"], 1)
        points = config["recovery_observations"]
        self.assertGreaterEqual(len(points), 8)
        self.assertTrue(all(b["liquid_rate_m3_day"] > a["liquid_rate_m3_day"] for a, b in zip(points, points[1:])))
        fitted = config["recovery_fitted_curve"]
        self.assertGreater(len(fitted), len(points))
        increments = [b["liquid_rate_m3_day"] - a["liquid_rate_m3_day"] for a, b in zip(fitted, fitted[1:])]
        self.assertTrue(all(a > b > 0 for a, b in zip(increments, increments[1:])))
        self.assertTrue(config["ranked_candidates"])
        self.assertEqual(self.database.scalar("SELECT configuration FROM strategy_versions WHERE id='SV-007'"), raw)

    def test_approval_metadata_is_persisted_idempotently_on_restart(self):
        detail = self.service.detail("ES-007")
        approval = detail["approvals"][0]
        self.assertEqual(approval["object_version"], detail["version"])
        self.assertEqual(approval["decision"], "approved")
        self.assertTrue(approval["comment"])
        self.assertTrue(approval["decided_at"])
        self.assertLessEqual(approval["decided_at"], detail["effective_at"])
        self.assertGreater(self.database.scalar("SELECT COUNT(*) FROM audit_logs WHERE action='seed.enrich_strategy_evidence'"), 0)
        before = self.database.scalar("SELECT COUNT(*) FROM audit_logs WHERE action='seed.enrich_strategy_evidence'")
        seed_database(self.database)
        self.assertEqual(self.service.detail("ES-007")["approvals"], detail["approvals"])
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM audit_logs WHERE action='seed.enrich_strategy_evidence'"), before)

    def test_effect_trace_contains_matching_measured_windows_and_conclusion(self):
        detail = self.service.detail("ES-007")
        self.assertIn("effect_trace", detail)
        effect = detail["effect_trace"]
        self.assertEqual(effect["well_id"], detail["well_id"])
        self.assertEqual(effect["strategy_version_id"], detail["current_version_id"])
        self.assertTrue(effect["valid_for_comparison"])
        for field in ("baseline_energy_kwh", "after_energy_kwh", "baseline_liquid_m3", "after_liquid_m3"):
            self.assertGreater(effect[field], 0)
        self.assertTrue(effect["conclusion"])

    def test_recovery_page_renders_observed_and_fitted_series_and_table(self):
        detail = self.service.detail("ES-007")
        script = r'''
import {renderRecoveryAnalysis} from './frontend/pages/recovery-analysis.js';
import {renderStrategyCenter} from './frontend/pages/strategy-center.js';
import {renderStrategyEditor} from './frontend/pages/strategy-editor.js';
import {components} from './frontend/components.js';
import * as format from './frontend/format.js';
const detail = JSON.parse(process.argv[1]);
const workspace = {innerHTML:'',querySelector:()=>({addEventListener(){}}),querySelectorAll:()=>[]};
const context = {api:{get:async(path)=>path.includes('?')?{items:[detail],total:1}:detail},components,format,workspace,page:{},pageHeader:()=>'',params:{id:detail.id},permissions:new Set(),navigate(){},isCurrent:()=>true};
await renderRecoveryAnalysis(context); const recovery=workspace.innerHTML;
await renderStrategyCenter(context); const strategy=workspace.innerHTML;
await renderStrategyEditor(context); console.log(JSON.stringify({recovery,strategy,editor:workspace.innerHTML}));
'''
        run = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps(detail)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True)
        rendered = json.loads(run.stdout)
        self.assertIn('data-series="recovery-observed"', rendered["recovery"])
        self.assertIn('data-series="recovery-fitted"', rendered["recovery"])
        self.assertIn("拟合液量", rendered["recovery"])
        self.assertIn("执行后", rendered["strategy"])
        self.assertIn("基线", rendered["strategy"])
        self.assertNotIn("undefined", rendered["recovery"] + rendered["strategy"])
        self.assertIn('value="70"', rendered["editor"])
        self.assertIn('value="180"', rendered["editor"])
        self.assertNotIn("undefined", rendered["editor"])
