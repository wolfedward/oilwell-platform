"""Acceptance contracts for acquisition and dynagraph intelligence pages."""

from datetime import timedelta
import http.client
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import unittest

from backend.http_server import create_server


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_ROOT = PROJECT_ROOT / "frontend" / "pages"


class ChartTooltipTests(unittest.TestCase):
    def test_all_xy_builders_emit_focusable_escaped_point_metadata(self):
        """Missing raw/filter/reference hit targets would make their points unreadable."""
        script = r'''
import fs from 'node:fs';
import { charts } from './frontend/charts.js';
import { escapeHtml } from './frontend/components.js';
let source = fs.readFileSync('frontend/pages/dynagraph-analysis.js', 'utf8')
  .replace(/^import .*;$/gm, '').replace(/^export /gm, '');
const electricCurve = new Function('charts', 'escapeHtml', source + '\nreturn electricCurve;')(charts, escapeHtml);
console.log(JSON.stringify({
  line: charts.line([{x:'<img src=x onerror=alert(1)>',y:1.25},{x:'2026-09-11',y:2.5}], {title:'A"<B>',unit:'kW'}),
  electric: electricCurve([{phase:0,power:1},{phase:90,power:-2}], [{phase_deg:0,power_kw:.5},{phase_deg:90,power_kw:-1}]),
  filtered: electricCurve([{phase:0,power:1},{phase:90,power:-2}], [{phase_deg:0,power_kw:.5},{phase_deg:90,power_kw:-1}], {smoothedOnly:true}),
  dynagraph: charts.dynagraph([{x:0,y:36},{x:1,y:61},{x:0,y:36}], {reference:[{x:0,y:37},{x:1,y:60},{x:0,y:37}]}),
}));
'''
        completed = subprocess.run(["node", "--input-type=module", "-e", script], cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", check=True)
        rendered = json.loads(completed.stdout)
        for name, count in (("line", 2), ("electric", 4), ("filtered", 2), ("dynagraph", 6)):
            with self.subTest(chart=name):
                points = re.findall(r'<circle\b[^>]*data-chart-point[^>]*>', rendered[name])
                self.assertEqual(len(points), count)
                for point in points:
                    for attribute in ('data-series=', 'data-x=', 'data-y=', 'data-x-unit=', 'data-y-unit=', 'tabindex="0"', 'aria-label='):
                        self.assertIn(attribute, point)
                self.assertNotRegex(rendered[name], r'<[^>]+\son(?:click|pointerover|focus)=')
        self.assertIn('data-y="1.25"', rendered["line"])
        self.assertIn('data-y-unit="kW"', rendered["line"])
        self.assertIn('&lt;img', rendered["line"])
        self.assertNotIn('<img', rendered["line"])
        self.assertIn('data-x="90"', rendered["electric"])
        self.assertIn('data-y="-2"', rendered["electric"])
        self.assertIn('data-x-unit="°"', rendered["electric"])
        self.assertIn('data-x-unit="m"', rendered["dynagraph"])
        self.assertIn('data-y-unit="kN"', rendered["dynagraph"])

    def test_delegated_pointer_and_focus_show_safe_bounded_tooltip_in_browser(self):
        """The real DOM must expose a bounded visible tooltip, guides, and keyboard cleanup."""
        browser = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
        if not browser.exists():
            self.skipTest("Edge is unavailable for the real-DOM chart interaction test")
        script = r'''
import { charts, bindChartInteractions } from CHART_MODULE;
const root = document.querySelector('#root');
root.innerHTML = charts.line([{x:'<img src=x onerror=alert(1)>',y:1.25},{x:'later',y:2.5}], {title:'Well <A>',unit:'kW'});
const checks = [];
function check(condition, message) { if (!condition) throw new Error(message); checks.push(message); }
try {
  bindChartInteractions(root);
  bindChartInteractions(root);
  let point = root.querySelector('[data-chart-point]');
  check(getComputedStyle(point).opacity === '0', 'sample markers are hidden at rest');
  check(getComputedStyle(point).pointerEvents === 'all', 'hidden sample markers retain hit targets');
  point.dispatchEvent(new PointerEvent('pointerover', {bubbles:true,clientX:999,clientY:999}));
  let tooltip = root.querySelector('.chart-tooltip');
  check(tooltip && !tooltip.hidden, 'pointer shows tooltip');
  check(tooltip.textContent.includes('<img src=x onerror=alert(1)>') && !tooltip.querySelector('img'), 'values remain safe text');
  check(tooltip.textContent.includes('1.25') && tooltip.textContent.includes('kW'), 'tooltip contains values and units');
  check(root.querySelectorAll('.chart-tooltip').length === 1, 'binding is idempotent');
  check(point.classList.contains('is-active'), 'active point is highlighted');
  check(getComputedStyle(point).opacity === '1', 'only the active marker becomes visible');
  check(root.querySelectorAll('.chart-guide').length === 2, 'two crosshair guides are present');
  const figure = point.closest('figure'), outer = figure.getBoundingClientRect(), box = tooltip.getBoundingClientRect();
  check(box.left >= outer.left && box.right <= outer.right + 1 && box.top >= outer.top && box.bottom <= outer.bottom + 1, 'tooltip stays inside figure');
  point.dispatchEvent(new PointerEvent('pointerout', {bubbles:true,relatedTarget:root}));
  check(tooltip.hidden && !point.classList.contains('is-active') && getComputedStyle(point).opacity === '0', 'pointerout clears tooltip and highlight');
  point.focus();
  check(!tooltip.hidden && point.classList.contains('is-active'), 'keyboard focus shows tooltip');
  point.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true,key:'Escape'}));
  check(tooltip.hidden, 'Escape dismisses tooltip');
  point.blur();
  root.innerHTML = charts.line([{x:'new',y:7},{x:'next',y:8}], {unit:'m'});
  point = root.querySelector('[data-chart-point]');
  point.focus();
  tooltip = root.querySelector('.chart-tooltip');
  check(tooltip && !tooltip.hidden && tooltip.textContent.includes('7'), 'delegation handles rerendered points');
  point.blur();
  check(tooltip.hidden, 'focusout dismisses tooltip');
  document.querySelector('#result').textContent = JSON.stringify({ok:true,checks});
} catch (error) { document.querySelector('#result').textContent = JSON.stringify({ok:false,error:String(error),checks}); }
'''
        script = script.replace("CHART_MODULE", json.dumps((PROJECT_ROOT / "frontend/charts.js").as_uri()))
        with tempfile.TemporaryDirectory(prefix="chart-tooltip-test-") as directory:
            page = Path(directory) / "test.html"
            page.write_text('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="' + (PROJECT_ROOT / "styles.css").as_uri() + '"><div id="root" style="width:300px"></div><pre id="result">pending</pre><script type="module">' + script + '</script>', encoding="utf-8")
            completed = subprocess.run([str(browser), "--headless", "--disable-gpu", "--no-first-run", "--allow-file-access-from-files", "--virtual-time-budget=3000", "--user-data-dir=" + str(Path(directory) / "profile"), "--dump-dom", page.as_uri()], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40)
            match = re.search(r'<pre id="result">(.*?)</pre>', completed.stdout, re.S)
            self.assertIsNotNone(match, completed.stderr[-2000:])
            import html
            result_text = html.unescape(match[1])
            self.assertNotEqual(result_text, "pending", "chart module failed to load or bind")
            result = json.loads(result_text)
            self.assertTrue(result["ok"], result)


class AnalysisPageSourceTests(unittest.TestCase):
    def source(self, name):
        return (PAGE_ROOT / name).read_text(encoding="utf-8")

    def test_task_three_modules_export_the_five_page_renderers(self):
        expected = {
            "acquisition.js": "renderAcquisitionStream",
            "data-quality.js": "renderQualityBoard",
            "dynagraphs.js": "renderDynagraphList",
            "dynagraph-analysis.js": "renderDynagraphAnalysis",
            "diagnosis-review.js": "renderDiagnosisReview",
        }
        for filename, renderer in expected.items():
            with self.subTest(filename=filename):
                source = self.source(filename)
                self.assertIn(f"export async function {renderer}", source)
                self.assertNotIn("Math.random", source)

    def test_acquisition_and_quality_pages_expose_real_polling_and_repair_controls(self):
        acquisition = self.source("acquisition.js")
        for value in (
            "/api/acquisition/stream", "暂停刷新", "继续刷新", "终端编号",
            "报文序号", "校验摘要", "接收状态",
        ):
            self.assertIn(value, acquisition)
        quality = self.source("data-quality.js")
        for value in (
            "/api/data-quality", "数据完整性", "缺失序列", "补传修复",
            "version", "sequence_gap",
        ):
            self.assertIn(value, quality)

    def test_analysis_page_exposes_intermediate_sections(self):
        source = self.source("dynagraph-analysis.js")
        for key in (
            "原始电功图", "滤波结果", "转矩换算", "示功图", "特征参数",
            "诊断依据", "产液量计算",
        ):
            self.assertIn(key, source)
        for key in ("候选工况", "模型版本", "审计记录", "/api/model-runs"):
            self.assertIn(key, source)
        self.assertIn("模板相似度对比", source)
        self.assertIn("displacement: item.displacement_m", source)
        self.assertIn("load: item.load_kn", source)

    def test_analysis_page_exposes_end_to_end_workflow_and_liquid_stages(self):
        source = self.source("dynagraph-analysis.js")
        self.assertIn(
            "电功图 → 转矩换算 → 示功图 → 智能诊断 → 产液计量 → 人工复核",
            source,
        )
        for stage in ("机构参数", "有效冲程与充满度", "日产液估算"):
            self.assertIn(stage, source)

    def test_message_deep_link_fetches_detail_without_relying_on_current_stream_page(self):
        source = self.source("acquisition.js")
        self.assertIn("showMessage(params.message_id)", source)

    def test_analysis_and_review_actions_use_exact_server_contracts(self):
        dynagraphs = self.source("dynagraphs.js")
        review = self.source("diagnosis-review.js")
        self.assertIn("/api/dynagraphs", dynagraphs)
        self.assertIn("/analyze", dynagraphs)
        self.assertIn("/api/diagnoses/", review)
        self.assertIn("condition", review)
        self.assertIn("comment", review)
        self.assertIn("version", review)
        self.assertIn("机器诊断结论", review)
        self.assertIn("人工复核记录", review)

    def test_failed_mutations_end_in_a_safe_visible_modal_state(self):
        for filename in ("data-quality.js", "diagnosis-review.js"):
            with self.subTest(filename=filename):
                self.assertIn("请关闭后重试", self.source(filename))


class AnalysisPageActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.server = create_server(
            "127.0.0.1", 0,
            database_path=Path(cls.temporary.name) / "platform.db",
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.tokens = {
            "algorithm": cls.login("algorithm_engineer", "Algorithm@2026"),
            "reviewer": cls.login("reviewer", "Review@2026"),
            "administrator": cls.login("administrator", "Admin@2026"),
        }
        cls.server.simulator.stop()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()
        cls.temporary.cleanup()

    @classmethod
    def request(cls, method, path, token=None, body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", cls.server.server_port, timeout=8
        )
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, payload, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    @classmethod
    def login(cls, username, password):
        status, payload = cls.request(
            "POST", "/api/auth/login",
            body={"username": username, "password": password},
        )
        if status != 200:
            raise AssertionError(payload)
        return payload["data"]["token"]

    @classmethod
    def next_terminal_tick(cls, scenario="normal"):
        terminal = cls.server.simulator.get("D-001")
        now = cls.server.simulator.now().replace(second=0, microsecond=0)
        base = terminal._last_tick or now
        terminal.apply_scenario(scenario, 30, now=base)
        return terminal, terminal.tick(base + timedelta(minutes=1))

    def test_repair_action_changes_quality_status(self):
        before = self.server.database.scalar(
            "SELECT COALESCE(MAX(id),0) FROM data_quality_events"
        )
        _terminal, emitted = self.next_terminal_tick("packet_loss")
        for raw in emitted:
            self.server.acquisition.ingest(raw)
        event = self.server.database.query_one(
            "SELECT id,version FROM data_quality_events "
            "WHERE id>? AND kind='sequence_gap' AND status='open' ORDER BY id DESC LIMIT 1",
            (before,),
        )
        self.assertIsNotNone(event)

        status, payload = self.request(
            "POST", f"/api/data-quality/{event['id']}/repair",
            self.tokens["administrator"], {"version": event["version"]},
        )

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["status"], "repaired")
        self.assertEqual(
            self.server.database.scalar(
                "SELECT status FROM data_quality_events WHERE id=?", (event["id"],)
            ),
            "repaired",
        )

    def test_analysis_and_review_actions_preserve_machine_result(self):
        _terminal, emitted = self.next_terminal_tick("normal")
        results = [self.server.acquisition.ingest(raw) for raw in emitted]
        card = next(result for result in results if result.kind == "electric_card")
        dynagraph_id = self.server.database.scalar(
            "SELECT id FROM dynagraphs WHERE raw_message_id=?", (card.raw_message_id,)
        )
        status, analyzed = self.request(
            "POST", f"/api/dynagraphs/{dynagraph_id}/analyze",
            self.tokens["algorithm"], {},
        )
        self.assertEqual(status, 200, analyzed)
        diagnosis = analyzed["data"]["diagnosis"]
        machine_condition = diagnosis["condition"]
        reviewed_condition = "normal" if machine_condition != "normal" else "supply_shortage"

        status, reviewed = self.request(
            "POST", f"/api/diagnoses/{diagnosis['id']}/review",
            self.tokens["reviewer"],
            {
                "condition": reviewed_condition,
                "comment": "复核电功图与示功图后记录现场判断",
                "version": diagnosis["review_version"],
            },
        )

        self.assertEqual(status, 200, reviewed)
        self.assertEqual(reviewed["data"]["reviewer_condition"], reviewed_condition)
        self.assertEqual(
            self.server.database.scalar(
                "SELECT diagnosis_type FROM diagnoses WHERE id=?", (diagnosis["id"],)
            ),
            machine_condition,
        )


if __name__ == "__main__":
    unittest.main()
