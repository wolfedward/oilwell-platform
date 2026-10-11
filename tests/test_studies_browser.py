"""Real browser checks for the engineering studies workbench."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from backend.studies.condition_models import train_conditions, predict_conditions
from backend.studies.recovery import fit_recovery
from backend.studies.energy_effects import evaluate_effects
from backend.studies.spectrum import analyze_spectrum
from backend.studies.protection import replay_protection
from backend.studies.energy_schedule import optimize_schedule

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = Path(os.environ['USERPROFILE']) / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'


class StudiesBrowserTests(unittest.TestCase):
    def run_browser(self, mode='overview'):
        source = (ROOT / 'tests/fixtures/studies_check.html').read_text(encoding='utf-8')
        for marker, path in {
            'PAGE_MODULE': 'frontend/pages/production-studies.js',
            'APP_MODULE': 'app.js',
            'STYLE_MODULE': 'styles.css',
        }.items():
            source = source.replace(marker, json.dumps((ROOT / path).as_uri()))
        source = source.replace('FIXTURE_MODE', mode)
        with tempfile.TemporaryDirectory(prefix='studies-ui-') as folder:
            page = Path(folder) / 'check.html'
            page.write_text(source, encoding='utf-8')
            result = subprocess.run(
                [str(RUNTIME / 'bin/node.exe'), str(ROOT / 'tests/fixtures/waveform_fixture_runner.cjs'), page.as_uri(), '1440'],
                cwd=ROOT, env=dict(os.environ, NODE_PATH=str(RUNTIME / 'node_modules')),
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            self.assertEqual(result.returncode, 0, result.stdout[-6000:] + result.stderr[-3000:])
            outcome = json.loads(result.stdout)
            self.assertTrue(outcome['ok'], outcome)
            return outcome

    def test_workbench_contract(self):
        self.run_browser()

    def test_create_result_chart_and_source(self):
        self.run_browser('create')

    def test_retry_reuses_exact_body_and_receipt(self):
        self.run_browser('retry')

    def test_advanced_editor_chart_units_and_non_success_results(self):
        self.run_browser('details')

    def test_source_choice_preserves_unsaved_input_and_file_import(self):
        self.run_browser('formstate')

    def test_review_selection_export_and_server_refusal(self):
        self.run_browser('review')

    def test_review_shell_omits_demo_navigation_and_links_open_interface(self):
        self.run_browser('shell')

    def test_late_detail_cannot_replace_new_selection(self):
        self.run_browser('stale')

    def test_logout_discards_late_study_response(self):
        self.run_browser('shell-logout')

    def test_rejected_result_cannot_offer_approval(self):
        self.run_browser('rejected-review')

    def test_import_refuses_envelope_that_would_erase_provenance(self):
        self.run_browser('import-envelope')

    def test_failed_create_preserves_raw_draft(self):
        self.run_browser('form-errors')

    def test_disposal_during_receipt_lookup_never_posts(self):
        self.run_browser('disposed-receipt')

    def test_pending_selection_stays_bound_to_original_record(self):
        self.run_browser('pending-select')

    def test_pending_selection_can_resolve_after_original_target_revoked(self):
        self.run_browser('pending-select-revoked')

    def test_disposal_aborts_export_and_reads(self):
        self.run_browser('abort-reads-export')

    def test_pending_deselection_retries_original_body(self):
        self.run_browser('pending-unselect')

    def test_builtin_examples_run_real_engines(self):
        payloads = self.run_browser('templates')['payloads']
        training = train_conditions(payloads['condition_training'])
        self.assertEqual(training['status'], 'completed')
        prediction = {**payloads['condition_prediction'], 'artifact': training['artifact']}
        self.assertEqual(predict_conditions(prediction)['status'], 'completed')
        self.assertEqual(fit_recovery(payloads['recovery_fit'])['status'], 'fitted')
        schedule = payloads['energy_schedule']
        schedule['wells'][0]['recovery'] = fit_recovery(payloads['recovery_fit'])
        self.assertEqual(optimize_schedule(schedule)['status'], 'feasible')
        self.assertIn(evaluate_effects(payloads['energy_effects'])['status'], ('descriptive', 'insufficient_evidence'))
        self.assertEqual(analyze_spectrum(payloads['motor_spectrum'])['status'], 'completed')
        self.assertEqual(replay_protection(payloads['protection_replay'])['status'], 'completed')
