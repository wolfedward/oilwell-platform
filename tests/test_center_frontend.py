"""Browser-module behavior for central acquisition data loading."""

import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent


def run_module(body):
    script = f"""
const page = await import('./frontend/pages/acquisition.js');
if (typeof page.loadCentralSnapshot !== 'function') {{
  console.log(JSON.stringify({{missing: true}}));
}} else {{
  {body}
}}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


class CentralAcquisitionPageTests(unittest.TestCase):
    def test_center_operations_markup_shows_live_metrics_links_and_measurements(self):
        result = run_module("""
if (typeof page.centralOperationsMarkup !== 'function') {
  console.log(JSON.stringify({missingMarkup: true}));
} else {
  const { components } = await import('./frontend/components.js');
  const format = {number: (value, digits = 0) => Number(value).toFixed(digits), dateTime: (value) => value || '—'};
  const html = page.centralOperationsMarkup({
    health: {
      status: 'available',
      generated_at: '2026-09-14T04:00:00Z',
      summary: {gateway_count: 1, device_count: 2, online_devices: 1, delayed_devices: 1,
        offline_devices: 0, samples_24h: 1440, bad_samples_24h: 3, uncertain_samples_24h: 4,
        p95_delivery_ms: 382.4, sequence_gap_count_24h: 2, missing_sequences_24h: 4},
      links: [{gateway_id: 'GW-001', device_id: 'D-001', status: 'online', last_sequence: 42,
        last_received_at: '2026-09-14T03:59:50Z', age_seconds: 10, sample_count_24h: 720,
        bad_sample_count_24h: 1}],
    },
    telemetry: {items: [{message_id: 'message-1', sample_index: 0, gateway_id: 'GW-001',
      device_id: 'D-001', point_code: 'active_power_kw', measured_at: '2026-09-14T03:59:45Z',
      sequence: 42, value: 6.125, quality: 'good', unit: 'kW', received_at: '2026-09-14T03:59:50Z'}], total: 1, page:1, page_size:100},
  }, components, format, {device_id: 'D-001', point_code: 'active_power_kw'});
  console.log(JSON.stringify({html}));
}
""")

        self.assertNotEqual(result, {"missingMarkup": True})
        html = result["html"]
        self.assertIn("中心采集链路", html)
        self.assertIn("1440", html)
        self.assertIn("382.4", html)
        self.assertIn("D-001", html)
        self.assertIn("active_power_kw", html)
        self.assertIn("6.13 kW", html)
        self.assertNotIn("测试数据", html)

    def test_available_center_loads_filtered_latest_samples(self):
        result = run_module("""
const calls = [];
const api = { get: async (path) => {
  calls.push(path);
  if (path === '/api/central/health') return {status: 'available', summary: {samples_24h: 9}, links: []};
  return {items: [{device_id: 'D-001', point_code: 'active_power_kw'}], total: 1, page: 1, page_size: 100};
}};
const snapshot = await page.loadCentralSnapshot(api, {device_id: 'D-001', point_code: 'active_power_kw'});
console.log(JSON.stringify({calls, snapshot}));
""")

        self.assertNotEqual(result, {"missing": True})
        self.assertEqual(result["calls"][0], "/api/central/health")
        self.assertEqual(
            result["calls"][1],
            "/api/central/telemetry?page=1&page_size=100&device_id=D-001&point_code=active_power_kw",
        )
        self.assertEqual(result["snapshot"]["telemetry"]["total"], 1)
        self.assertEqual(result["snapshot"]["health"]["status"], "available")

    def test_unavailable_center_does_not_issue_a_telemetry_query(self):
        result = run_module("""
const calls = [];
const api = { get: async (path) => {
  calls.push(path);
  return {status: 'not_configured', summary: {}, links: []};
}};
const snapshot = await page.loadCentralSnapshot(api, {});
console.log(JSON.stringify({calls, snapshot}));
""")

        self.assertNotEqual(result, {"missing": True})
        self.assertEqual(result["calls"], ["/api/central/health"])
        self.assertEqual(result["snapshot"]["health"]["status"], "not_configured")
        self.assertEqual(result["snapshot"]["telemetry"], {"items": [], "total": 0, "page": 1, "page_size": 100})


if __name__ == "__main__":
    unittest.main()
