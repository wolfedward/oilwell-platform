import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


class InversionParametersBrowserTests(unittest.TestCase):
    def run_fixture(self, mode, width=1120):
        fixture = 'inversion_review_check.html' if mode.startswith('round1-') else 'inversion_parameters_check.html'
        source = (ROOT / 'tests/fixtures' / fixture).read_text(encoding='utf-8')
        for marker, path in {
            'STYLE_MODULE': 'styles.css', 'APP_MODULE': 'app.js',
            'PAGE_MODULE': 'frontend/pages/production-parameters.js',
            'API_MODULE': 'frontend/api.js',
        }.items():
            source = source.replace(marker, json.dumps((ROOT / path).as_uri()))
        source = source.replace('FIXTURE_MODE', mode)
        runtime = Path(os.environ['USERPROFILE']) / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
        with tempfile.TemporaryDirectory(prefix='inversion-ui-') as directory:
            page = Path(directory) / 'check.html'
            page.write_text(source, encoding='utf-8')
            evidence = os.environ.get('OILWELL_INVERSION_UI_EVIDENCE')
            screenshot = ''
            if evidence:
                destination = Path(evidence)
                destination.mkdir(parents=True, exist_ok=True)
                screenshot = str(destination / f'{mode}-{width}.png')
            result = subprocess.run(
                [str(runtime / 'bin/node.exe'), str(ROOT / 'tests/fixtures/inversion_fixture_runner.cjs'),
                 page.as_uri(), str(width), screenshot], cwd=ROOT,
                env=dict(os.environ, NODE_PATH=str(runtime / 'node_modules')),
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-2000:])
            outcome = json.loads(result.stdout)
            self.assertTrue(outcome['ok'], outcome)
            self.assertGreater(len(outcome['checks']), 1)

    def test_parameters_page_disabled_and_missing_evidence(self):
        self.run_fixture('disabled-evidence')

    def test_parameter_units_versions_and_review_separation(self):
        self.run_fixture('units-versions')

    def test_admin_sees_all_pages_but_no_self_approve(self):
        self.run_fixture('admin-review')

    def test_pending_request_keeps_key_after_timeout(self):
        self.run_fixture('pending-recovery')

    def test_landing_sidebar_and_deeplink_use_same_capability(self):
        self.run_fixture('shell-gate')
        self.run_fixture('shell-enabled')

    def test_denial_sticks_to_actor_session_and_disposal_blocks_late_data(self):
        self.run_fixture('denial-lifecycle')

    def test_rejected_draft_can_be_corrected_without_new_login(self):
        self.run_fixture('round1-rejection')

    def test_assembly_parameter_and_other_histories_page_without_guessing_head(self):
        self.run_fixture('round1-pagination')

    def test_actual_parameter_difference_and_read_only_mechanical_selection_history(self):
        self.run_fixture('round1-difference')

    def test_timeout_remount_displays_original_command_and_replays_it(self):
        self.run_fixture('round1-pending')

    def test_postcommit_conflict_keeps_original_command_for_lookup(self):
        self.run_fixture('round1-postcommit')

    def test_pending_review_remount_shows_original_decision_and_replays_it(self):
        self.run_fixture('round1-decision-pending')


if __name__ == '__main__':
    unittest.main()
