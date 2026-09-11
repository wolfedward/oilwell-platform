"""Stable, correlated operational trends with an exact latest-day anchor."""

import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest

from backend.audit import AuditService
from backend.database import Database
from backend.seed import seed_database
from backend.services.wells import WellsService


ROOT = Path(__file__).resolve().parent.parent
METRICS = ("daily_fluid", "daily_oil", "power_kwh", "pump_efficiency")


def correlation(xs, ys):
    xmean, ymean = sum(xs) / len(xs), sum(ys) / len(ys)
    numerator = sum((x - xmean) * (y - ymean) for x, y in zip(xs, ys))
    denominator = math.sqrt(sum((x - xmean) ** 2 for x in xs) * sum((y - ymean) ** 2 for y in ys))
    return numerator / denominator if denominator else 0.0


class DashboardTrendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "platform.db")
        self.database.migrate()
        seed_database(self.database)
        self.service = WellsService(self.database, AuditService(self.database))

    def test_four_series_have_visible_variation_and_positive_energy_correlation(self):
        """Near-flat aggregate imports must not flatten all four operational trends."""
        data = self.service.dashboard()
        trends = data["trends"]
        self.assertEqual(len(trends), 14)
        self.assertEqual(trends[0]["day"], "2026-07-02")
        self.assertEqual(trends[-1]["day"], "2026-07-15")
        for metric in METRICS[:3]:
            values = [point[metric] for point in trends]
            self.assertGreater((max(values) - min(values)) / min(values), .12, metric)
            self.assertGreaterEqual(len(set(values)), 12, metric)
        efficiency = [point["pump_efficiency"] for point in trends]
        self.assertGreater(max(efficiency) - min(efficiency), .02)
        self.assertTrue(all(0 < value < 1 for value in efficiency))
        coefficient = correlation([p["daily_fluid"] for p in trends], [p["power_kwh"] for p in trends])
        self.assertGreater(coefficient, .9)
        self.assertAlmostEqual(data["trend_metadata"]["fluid_energy_correlation"], coefficient, places=6)

    def test_latest_point_equals_unrounded_production_and_repeated_requests_are_stable(self):
        """Rounding or request-time noise must not separate the latest point from its card."""
        first = self.service.dashboard()
        self.assertEqual(first["trends"][-1], first["production"])
        self.assertEqual(first["trends"], self.service.dashboard()["trends"])
        reopened = WellsService(Database(self.database.path), AuditService(self.database))
        self.assertEqual(first["trends"], reopened.dashboard()["trends"])

    def test_trend_correction_retains_original_rows_and_reports_its_basis(self):
        """A read model must not rewrite production/energy evidence or lose raw totals."""
        before = [tuple(row) for row in self.database.query_all("SELECT * FROM production_daily ORDER BY well_id,day")]
        data = self.service.dashboard()
        after = [tuple(row) for row in self.database.query_all("SELECT * FROM production_daily ORDER BY well_id,day")]
        self.assertEqual(before, after)
        self.assertEqual(data["trend_metadata"]["method"], "calendar_normalized_v1")
        self.assertIn("校正", data["trend_metadata"]["basis"])
        self.assertEqual(len(data["reported_trends"]), 14)
        self.assertNotEqual(data["reported_trends"][0], data["trends"][0])
        self.assertEqual(data["reported_trends"][-1], data["production"])

    def test_new_import_outside_managed_history_preserves_reported_values(self):
        """A later field import must not inherit the managed history's calendar profile."""
        with self.database.transaction(None, "test.production_import", "well", "W-001") as connection:
            connection.execute("""INSERT INTO production_daily(
                well_id,day,daily_fluid,daily_oil,power_kwh,pump_efficiency,
                stroke_m,frequency_hz,runtime_hours,status,source_type
                ) VALUES('W-001','2026-09-10',10.12345,5.65432,200.12345,.61234567,
                         2.8,35,20,'valid','measurement_import')""")
        data = self.service.dashboard()
        self.assertEqual(data["trends"][-1], {"day": "2026-09-10", "daily_fluid": 10.12345,
                         "daily_oil": 5.65432, "power_kwh": 200.12345, "pump_efficiency": .61234567})
        self.assertEqual(data["trends"], data["reported_trends"])
        self.assertEqual(data["trend_metadata"]["method"], "reported_daily")

    def test_dashboard_renders_four_dated_charts_and_correlation_without_realtime_claim(self):
        """Only showing fluid/energy hides oil and efficiency variation from operators."""
        data = self.service.dashboard()
        script = r'''
import { renderDashboard } from './frontend/pages/dashboard.js';
let input = ''; for await (const chunk of process.stdin) input += chunk;
const data = JSON.parse(input), workspace = {innerHTML:''};
await renderDashboard({api:{get:async()=>data}, workspace, page:{}, pageHeader:(_page,content)=>content,
  isCurrent:()=>true, format:{number:(v,d=0)=>Number(v).toFixed(d), percent:(v,d=1)=>(v*100).toFixed(d)+'%', dateTime:String}});
console.log(workspace.innerHTML);
'''
        completed = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(data), cwd=ROOT, text=True, encoding="utf-8", capture_output=True, check=True)
        output = completed.stdout
        self.assertEqual(output.count('<figure class="chart">'), 4)
        self.assertIn("液量—耗电相关系数", output)
        self.assertIn("运营趋势校正", output)
        self.assertIn("2026-07-15", output)
        self.assertNotIn("实时数据时点", output)
        for label in ("演示趋势", "模拟波动", "验收数据"):
            self.assertNotIn(label, output)


if __name__ == "__main__":
    unittest.main()
