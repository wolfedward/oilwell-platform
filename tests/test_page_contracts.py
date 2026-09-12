import http.client
import csv
import io
import json
import subprocess
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest

from backend.http_server import create_server


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DashboardPayloadContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = create_server(
            "127.0.0.1", 0,
            database_path=Path(self.temporary.name) / "platform.db",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        status, payload = self._request(
            "POST", "/api/auth/login",
            {"username": "administrator", "password": "Admin@2026"},
        )
        self.assertEqual(status, 200)
        self.token = payload["data"]["token"]

    def _close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def _request(self, method, path, body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if hasattr(self, "token"):
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            connection.request(
                method, path,
                body=json.dumps(body).encode("utf-8") if body is not None else None,
                headers=headers,
            )
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8"))
        finally:
            connection.close()

    def _raw_request(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", path, headers={"Authorization": f"Bearer {self.token}"})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_dashboard_payload_contains_drilldown_keys(self):
        status, payload = self._request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertTrue({
            "production", "devices", "quality", "alarms",
            "strategies", "energy", "trends",
        }.issubset(data))
        self.assertEqual(data["devices"]["total"], 48)
        self.assertEqual(data["production"]["day"], data["data_day"])
        self.assertGreaterEqual(len(data["trends"]), 7)
        self.assertTrue(all("day" in item and "power_kwh" in item for item in data["trends"]))

    def test_well_low_output_filter_and_operational_detail(self):
        status, operating = self._request("GET", "/api/wells?status=operating&page_size=100")
        self.assertEqual(status, 200)
        status, dashboard = self._request("GET", "/api/dashboard")
        self.assertEqual(operating["data"]["total"], dashboard["data"]["active_wells"])
        status, payload = self._request("GET", "/api/wells?low_output=1&page_size=100")
        self.assertEqual(status, 200)
        self.assertGreater(payload["data"]["total"], 0)
        self.assertTrue(all(item["low_output"] for item in payload["data"]["items"]))
        well_id = payload["data"]["items"][0]["id"]
        status, detail = self._request("GET", f"/api/wells/{well_id}")
        self.assertEqual(status, 200)
        self.assertIn("production_history", detail["data"])
        self.assertIn("latest_dynagraph", detail["data"])

    def test_well_csv_reuses_all_ledger_filters(self):
        query = "low_output=1&status=intermittent&keyword=W-"
        status, listed = self._request("GET", f"/api/wells?{query}&page_size=100")
        self.assertEqual(status, 200)
        status, headers, raw = self._raw_request(f"/api/wells.csv?{query}")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual(len(rows) - 1, listed["data"]["total"])

    def test_device_detail_includes_auditable_operations(self):
        status, payload = self._request("GET", "/api/devices?page_size=1")
        self.assertEqual(status, 200)
        device_id = payload["data"]["items"][0]["id"]
        status, detail = self._request("GET", f"/api/devices/{device_id}")
        self.assertEqual(status, 200)
        self.assertTrue({
            "latest_heartbeat", "recent_events", "recent_messages", "recent_commands",
        }.issubset(detail["data"]))
        self.assertTrue(any(
            item["event_type"] == "self_check"
            for item in detail["data"]["recent_events"]
        ))

    def test_seed_includes_one_complete_signed_command_trace(self):
        status, payload = self._request("GET", "/api/commands?page_size=100")
        self.assertEqual(status, 200)
        summary = next(
            item for item in payload["data"]["items"]
            if item["id"].endswith("CMD-CONTROL-001")
        )
        status, payload = self._request("GET", f"/api/commands/{summary['id']}")
        self.assertEqual(status, 200)
        command = payload["data"]
        self.assertEqual(command["device_id"], "D-001")
        self.assertEqual(len(command["signature"]), 64)
        self.assertEqual(command["payload"]["signature"], command["signature"])
        self.assertTrue(command["idempotency_key"])
        self.assertEqual(
            (datetime.fromisoformat(command["expires_at"]) -
             datetime.fromisoformat(command["issued_at"])).total_seconds(),
            15 * 60,
        )
        self.assertEqual(
            [item["state"] for item in command["receipts"]],
            ["sent", "received", "validated", "executing", "success"],
        )
        strategy = self.server.database.query_one(
            "SELECT current_version_id,status,created_by,submitted_by,decision_by "
            "FROM energy_strategies WHERE id=?",
            (command["strategy_id"],),
        )
        self.assertEqual(
            (strategy["current_version_id"], strategy["status"]),
            (command["strategy_version_id"], "effective"),
        )
        self.assertEqual(strategy["created_by"], strategy["submitted_by"])
        self.assertNotEqual(strategy["submitted_by"], strategy["decision_by"])
        approval = self.server.database.query_one(
            "SELECT decision,status,actor_id FROM approvals "
            "WHERE object_type='energy_strategy' AND object_id=?",
            (command["strategy_id"],),
        )
        self.assertEqual((approval["decision"], approval["status"]), ("approved", "decided"))
        self.assertEqual(approval["actor_id"], strategy["decision_by"])

    def test_operational_detail_routes_reject_unknown_query_parameters(self):
        for path in (
            "/api/dashboard?bad=1", "/api/wells/W-001?bad=1",
            "/api/devices/D-001?bad=1", "/api/devices/D-001/registers?bad=1",
        ):
            with self.subTest(path=path):
                status, payload = self._request("GET", path)
                self.assertEqual(status, 422)
                self.assertEqual(payload["error"]["code"], "validation_error")


class OperationalPageSourceTests(unittest.TestCase):
    def test_electric_curve_preserves_valid_nonuniform_phase_values(self):
        script = r"""
const fs = require('fs');
let source = fs.readFileSync('frontend/pages/dynagraph-analysis.js', 'utf8');
source = source.replace('import { charts } from "../charts.js";', '');
source = source.replace('import { escapeHtml } from "../components.js";',
  'const escapeHtml = (value) => String(value ?? "");');
source = source.replace('export async function renderDynagraphAnalysis',
  'async function renderDynagraphAnalysis');
source += '\nconsole.log(electricCurve([' +
  '{phase:0,power:1},{phase:0.5,power:2},{phase:1.4,power:3},' +
  '{phase:2.2,power:4},{phase:3.8,power:5},{phase:5.5,power:6}' +
  '], []));';
import('./frontend/charts.js').then(({charts}) => eval(source));
"""
        completed = subprocess.run(
            ["node", "-e", script], cwd=PROJECT_ROOT, text=True, encoding="utf-8",
            capture_output=True, check=True,
        )
        self.assertIn("原始：曲柄角 0.50°", completed.stdout)
        self.assertIn("原始：曲柄角 5.50°", completed.stdout)

    def test_electric_curve_maps_legacy_indices_like_backend(self):
        script = r"""
const fs = require('fs');
let source = fs.readFileSync('frontend/pages/dynagraph-analysis.js', 'utf8');
source = source.replace('import { charts } from "../charts.js";', '');
source = source.replace('import { escapeHtml } from "../components.js";',
  'const escapeHtml = (value) => String(value ?? "");');
source = source.replace('export async function renderDynagraphAnalysis',
  'async function renderDynagraphAnalysis');
source += '\nconsole.log(electricCurve([' +
  '{phase:0,power:1},{phase:1,power:2},{phase:2,power:3},{phase:3,power:4}' +
  '], []));';
import('./frontend/charts.js').then(({charts}) => eval(source));
"""
        completed = subprocess.run(
            ["node", "-e", script], cwd=PROJECT_ROOT, text=True, encoding="utf-8",
            capture_output=True, check=True,
        )
        self.assertIn("原始：曲柄角 270.00°", completed.stdout)
        self.assertNotIn("原始：曲柄角 360.00°", completed.stdout)

    def test_dynagraph_chart_renders_complete_finite_axes(self):
        script = r"""
const fs = require('fs');
let source = fs.readFileSync('frontend/charts.js', 'utf8');
source = source.replace('import { escapeHtml } from "./components.js";',
  'const escapeHtml = (value) => String(value ?? "");');
source = source.replace(/^export /gm, '');
source += '\nconsole.log(JSON.stringify({\n' +
  'normal: charts.dynagraph([{x:0,y:10},{x:1,y:20},{x:2,y:15}], {title:"井A"}),\n' +
  'hostile: charts.dynagraph([{x:null,y:null},{x:2,y:Infinity},{x:2,y:NaN},{x:1e308,y:-1e308}], {title:"井B"})\n' +
  '}));';
const result = eval(source);
"""
        completed = subprocess.run(
            ["node", "-e", script], cwd=PROJECT_ROOT, text=True, encoding="utf-8",
            capture_output=True, check=True,
        )
        rendered = json.loads(completed.stdout)
        normal = rendered["normal"]
        self.assertIn("位移 (m)", normal)
        self.assertIn("载荷 (kN)", normal)
        self.assertEqual(normal.count('data-axis="x"'), 5)
        self.assertEqual(normal.count('data-axis="y"'), 5)
        for invalid in ("NaN", "Infinity", "-Infinity"):
            self.assertNotIn(invalid, rendered["hostile"])

    def test_task_two_page_modules_exist_and_use_shared_api(self):
        paths = (
            "frontend/charts.js", "frontend/pages/dashboard.js", "frontend/pages/wells.js",
            "frontend/pages/well-detail.js", "frontend/pages/devices.js",
            "frontend/pages/device-detail.js", "frontend/pages/device-command.js",
        )
        for relative in paths:
            with self.subTest(relative=relative):
                source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("Math.random", source)
        app = (PROJECT_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('from "./frontend/pages/dashboard.js"', app)
        self.assertIn('from "./frontend/pages/wells.js"', app)
        self.assertIn('from "./frontend/pages/devices.js"', app)

    def test_dashboard_exposes_live_three_layer_operations_overview(self):
        """Dropping a live metric, queue or shortcut must break the overview contract."""
        source = (PROJECT_ROOT / "frontend/pages/dashboard.js").read_text(encoding="utf-8")

        for key in (
            "data_timestamp", "daily_fluid", "daily_oil", "power_kwh", "pump_efficiency",
            "alarms.active", "alarms.high", "quality.open_events",
            "strategies.pending_strategies",
        ):
            with self.subTest(key=key):
                self.assertIn(key, source)

        for label in (
            "日产液趋势", "日产油趋势", "日耗电趋势", "平均泵效趋势",
            "异常待办", "活动报警", "高危报警", "数据质量", "待审批策略",
            "最新生产汇总", "节能成效",
        ):
            with self.subTest(label=label):
                self.assertIn(label, source)

        for target in (
            "acquisition", "dynagraph-analysis", "data-quality",
            "strategy-center", "work-orders", "reports",
        ):
            with self.subTest(target=target):
                self.assertIn(f'"{target}"', source)

        self.assertNotIn("Math.random", source)
        self.assertNotIn("Date.now", source)

    def test_dashboard_has_narrow_viewport_layout(self):
        """Dashboard-specific grids must not override the narrow-screen column rules."""
        source = (PROJECT_ROOT / "styles.css").read_text(encoding="utf-8")
        narrow = source[source.index("@media (max-width: 1260px)"):source.index("@media print")]

        self.assertIn(".dashboard-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }", narrow)
        self.assertIn(
            ".dashboard-trends, .dashboard-results, .dashboard-actions { grid-template-columns: minmax(0, 1fr); }",
            narrow,
        )
        self.assertIn(".dashboard-actions > * { min-width: 0; }", source)

    def test_device_detail_contains_live_and_control_sections(self):
        source = (PROJECT_ROOT / "frontend/pages/device-detail.js").read_text(encoding="utf-8")
        for key in ("心跳", "采集通道", "实时寄存器", "通信报文", "自检记录"):
            self.assertIn(key, source)

        command_source = (PROJECT_ROOT / "frontend/pages/device-command.js").read_text(encoding="utf-8")
        self.assertIn("历史协议指令", command_source)

    def test_well_list_exposes_acceptance_filters_and_real_csv(self):
        source = (PROJECT_ROOT / "frontend/pages/wells.js").read_text(encoding="utf-8")
        for key in ("单位", "井组", "运行状态", "低产井", "关键字", "导出当前结果"):
            self.assertIn(key, source)
        self.assertIn("/api/wells.csv", source)


if __name__ == "__main__":
    unittest.main()
