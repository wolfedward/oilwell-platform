"""Real browser behavior; doubles replace only the HTTP transport boundary."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class IngestBrowserTests(unittest.TestCase):
    def run_fixture(self, filename, width=1440, mode='workspace', delay_ms=0):
        browser = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
        self.assertTrue(browser.exists(), 'Edge is required for incident browser verification')
        source = (ROOT / 'tests/fixtures' / filename).read_text(encoding='utf-8')
        source = source.replace('FIXTURE_MODE', mode)
        source = source.replace('FIXTURE_DELAY_MS', str(delay_ms))
        for placeholder, name in {
            'STYLE_MODULE': 'styles.css', 'APP_MODULE': 'app.js',
            'INGEST_MODULE': 'frontend/pages/ingest-reliability.js',
            'CLIENT_MODULE': 'frontend/ingest/client.js',
            'ACTIONS_MODULE': 'frontend/ingest/actions.js',
            'COMPONENT_MODULE': 'frontend/components.js',
        }.items():
            source = source.replace(placeholder, json.dumps((ROOT / name).as_uri()))
        with tempfile.TemporaryDirectory(prefix='ingest-ui-') as directory:
            page = Path(directory) / 'check.html'
            page.write_text(source, encoding='utf-8')
            evidence = os.environ.get('OILWELL_INGEST_UI_EVIDENCE')
            screenshot = ''
            if evidence:
                destination = Path(evidence).resolve()
                destination.mkdir(parents=True, exist_ok=True)
                screenshot = str(destination / f'{Path(filename).stem}-{mode}-{width}.png')
            node_root = Path(os.environ.get('USERPROFILE', '')) / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
            node = shutil.which('node') or str(node_root / 'bin/node.exe')
            result = subprocess.run([
                node, str(ROOT / 'tests/fixtures/ingest_fixture_runner.cjs'), page.as_uri(), str(width), screenshot,
            ], cwd=ROOT, env=dict(os.environ, NODE_PATH=str(node_root / 'node_modules')),
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=55,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stdout[-6000:] + result.stderr[-3000:])
            outcome = json.loads(result.stdout)
            self.assertTrue(outcome['ok'], outcome)

    def test_workspace_roles_raw_history_and_races_desktop(self):
        """Late details, XSS, lost integer precision or fabricated states break this."""
        self.run_fixture('ingest_workspace_check.html')

    def test_workspace_roles_raw_history_and_races_laptop(self):
        self.run_fixture('ingest_workspace_check.html', width=1024)

    def test_immutable_retry_actions_and_stale_write_desktop(self):
        """Changing a retry key/body or applying an old operation to a new case fails."""
        self.run_fixture('ingest_actions_check.html')

    def test_immutable_retry_actions_and_stale_write_laptop(self):
        self.run_fixture('ingest_actions_check.html', width=1024)

    def test_uncertain_action_survives_selection_change(self):
        for mode in ('settled', 'late', 'sending', 'revoked', 'success', 'closed-command'):
            with self.subTest(mode=mode):
                self.run_fixture('ingest_action_selection_check.html', mode=mode)

    def test_uncertain_action_survives_selection_change_laptop(self):
        self.run_fixture('ingest_action_selection_check.html', width=1024, mode='settled')

    def test_shell_ready_uses_actual_workspace(self):
        self.run_fixture('ingest_workspace_check.html', mode='shell-ready')

    def test_shell_disabled_hides_entry_and_explains_direct_route(self):
        self.run_fixture('ingest_workspace_check.html', mode='shell-disabled')

    def test_shell_missing_capability_is_not_enabled(self):
        self.run_fixture('ingest_workspace_check.html', mode='shell-missing')

    def test_shell_unavailable_is_not_fake_empty(self):
        self.run_fixture('ingest_workspace_check.html', mode='shell-unavailable')

    def test_delayed_shell_finishes_before_browser_result_is_collected(self):
        self.run_fixture('ingest_workspace_check.html', mode='shell-unavailable', delay_ms=8000)

    def test_shell_denied_hides_entry_and_prevents_requests(self):
        self.run_fixture('ingest_workspace_check.html', mode='shell-denied')

    def test_shell_ready_fits_laptop_without_changing_other_pages(self):
        self.run_fixture('ingest_workspace_check.html', width=1024, mode='shell-ready')
