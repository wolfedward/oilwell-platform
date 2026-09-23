"""Real Edge DOM checks for registry pages; API doubles stop only at the network."""
import html
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class RegistryBrowserTests(unittest.TestCase):
    def run_fixture(self, filename, width=1440):
        browser = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
        self.assertTrue(browser.exists(), "Edge is required for registry browser verification")
        source = (ROOT / "tests/fixtures" / filename).read_text(encoding="utf-8")
        for placeholder, filename in {
            "STYLE_MODULE": "styles.css",
            "REGISTRY_MODULE": "frontend/pages/field-registry.js",
            "CLIENT_MODULE": "frontend/registry/client.js",
            "COMPONENT_MODULE": "frontend/components.js",
            "FORMAT_MODULE": "frontend/format.js",
        }.items():
            source = source.replace(placeholder, json.dumps((ROOT / filename).as_uri()))
        with tempfile.TemporaryDirectory(prefix="registry-ui-") as directory:
            page = Path(directory) / "test.html"
            page.write_text(source, encoding="utf-8")
            result = subprocess.run([
                str(browser), "--headless", "--disable-gpu", "--no-first-run",
                "--allow-file-access-from-files", "--virtual-time-budget=5000",
                "--window-size=" + str(width) + ",1000",
                "--user-data-dir=" + str(Path(directory) / "profile"), "--dump-dom", page.as_uri(),
            ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
            match = re.search(r'<pre id="result">(.*?)</pre>', result.stdout, re.S)
            self.assertIsNotNone(match, result.stderr[-1000:])
            payload = html.unescape(match[1])
            self.assertNotEqual(payload, "pending", "registry browser modules did not complete")
            outcome = json.loads(payload)
            self.assertTrue(outcome["ok"], outcome)

    def test_list_permissions_selection_and_safe_retry_at_desktop_width(self):
        self.run_fixture("registry_list_check.html")

    def test_list_permissions_selection_and_safe_retry_at_laptop_width(self):
        self.run_fixture("registry_list_check.html", 1024)
