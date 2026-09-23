"""Exercise central query controls in a real headless Edge DOM."""

import html
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class CentralBrowserTests(unittest.TestCase):
    def test_filter_pagination_refresh_outage_and_request_race(self):
        browser = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
        if not browser.exists():
            self.skipTest('Edge is unavailable')
        source = (ROOT / 'tests/fixtures/central_ui_check.html').read_text(encoding='utf-8')
        for placeholder, filename in {
            'STYLE_MODULE': 'styles.css',
            'CENTRAL_MODULE': 'frontend/pages/central-telemetry.js',
            'ACQUISITION_MODULE': 'frontend/pages/acquisition.js',
            'COMPONENT_MODULE': 'frontend/components.js',
            'FORMAT_MODULE': 'frontend/format.js',
        }.items():
            source = source.replace(placeholder, json.dumps((ROOT / filename).as_uri()))
        with tempfile.TemporaryDirectory(prefix='central-ui-') as directory:
            page = Path(directory) / 'test.html'
            page.write_text(source, encoding='utf-8')
            result = subprocess.run([
                str(browser), '--headless', '--disable-gpu', '--no-first-run',
                '--allow-file-access-from-files', '--virtual-time-budget=3000',
                '--user-data-dir=' + str(Path(directory) / 'profile'), '--dump-dom', page.as_uri(),
            ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=40)
            match = re.search(r'<pre id="result">(.*?)</pre>', result.stdout, re.S)
            self.assertIsNotNone(match, result.stderr[-1000:])
            payload = html.unescape(match[1])
            self.assertNotEqual(payload, 'pending', 'browser module did not complete')
            outcome = json.loads(payload)
            self.assertTrue(outcome['ok'], outcome)


if __name__ == '__main__':
    unittest.main()
