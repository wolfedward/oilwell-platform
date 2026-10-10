import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


class BrowserFixtureCase(unittest.TestCase):
    def run_fixture(self, mode, width=1120, encoded_rows=None):
        fixture = getattr(self, 'fixture', None) or ('inversion_review_check.html' if mode.startswith('round1-') else 'inversion_parameters_check.html')
        source = (ROOT / 'tests/fixtures' / fixture).read_text(encoding='utf-8')
        for marker, path in {
            'STYLE_MODULE': 'styles.css', 'APP_MODULE': 'app.js',
            'PAGE_MODULE': 'frontend/pages/production-parameters.js',
            'API_MODULE': 'frontend/api.js',
            'CHART_MODULE': 'frontend/inversion/charts.js',
            'WORKBENCH_MODULE': 'frontend/pages/production-inversion.js',
            'RESULTS_MODULE': 'frontend/pages/production-results.js',
            'CLIENT_MODULE': 'frontend/inversion/client.js',
        }.items():
            source = source.replace(marker, json.dumps((ROOT / path).as_uri()))
        source = source.replace('FIXTURE_MODE', mode)
        source = source.replace('ENCODED_ROWS', json.dumps(encoded_rows or []))
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


class InversionWorkbenchBrowserTests(BrowserFixtureCase):
    fixture = 'inversion_chart_check.html'

    def test_two_card_toggles_hover_and_keyboard_two_decimals(self):
        self.run_fixture('two-card', 1120)

    def test_quality_gap_and_deadpoint_fill_are_distinct(self):
        self.run_fixture('gaps', 760)

    def test_large_indices_remain_exact_strings(self):
        self.run_fixture('exact', 520)

    def test_all_chart_types_use_small_hover_only_points(self):
        self.run_fixture('all-charts', 1120)

    def test_stale_selection_and_revoked_retry_refused(self):
        self.fixture = 'inversion_workbench_check.html'
        self.run_fixture('stale-retry')

    def test_replaced_run_continues_serialized_polling(self):
        self.fixture = 'inversion_workbench_check.html'
        self.run_fixture('replace-poll')

    def test_replaced_run_disposes_rendered_result_and_actions(self):
        self.fixture = 'inversion_workbench_check.html'
        self.run_fixture('replace-rendered')

    def test_replaced_run_aborts_late_result_pagination(self):
        self.fixture = 'inversion_workbench_check.html'
        self.run_fixture('replace-pages')

    def test_blocked_causes_are_readable_exact_and_disposal_safe(self):
        self.fixture='inversion_workbench_check.html'
        self.run_fixture('terminal-diagnostics')

    def test_logout_stops_poll_and_denial_is_sticky(self):
        self.fixture = 'inversion_workbench_check.html'
        self.run_fixture('shell-logout')
        self.run_fixture('sticky-denial')

    def test_shell_disabled_unavailable_and_landing_routes(self):
        self.fixture = 'inversion_workbench_check.html'
        for mode in ('shell-disabled', 'shell-unavailable', 'shell-enabled'):
            self.run_fixture(mode)

    def test_additive_client_methods_share_sticky_authority_and_disposal(self):
        self.fixture = 'inversion_workbench_check.html'
        self.run_fixture('client-methods')


class InversionResultsBrowserTests(BrowserFixtureCase):
    fixture = 'inversion_results_check.html'

    def test_unknown_metrics_show_reason_not_zero(self):
        self.run_fixture('unknown-metrics', 760)

    def test_reference_range_uses_independent_measured_support(self):
        self.run_fixture('reference-support')

    def test_encoded_reference_gaps_never_join_and_legacy_support_is_unknown(self):
        from dataclasses import replace
        from backend.inversion.comparison import compare
        from backend.inversion.result_codec import encode
        from tests.test_inversion_comparison import prediction,reference,policy
        from tests.test_inversion_worker import CodecTests
        pred=prediction((0,3),(12.,12.))
        pred=replace(pred,points=tuple(replace(p,status='unobservable',load_N=None,
            reason_codes=('deadpoint',)) for p in pred.points))
        bad=reference((0,1,2,3),(10,None,10,10))
        missing=replace(bad,load=replace(bad.load,samples=(bad.load.samples[0],)+bad.load.samples[2:]))
        for ref,chosen in ((bad,policy()),(missing,policy()),
                           (reference((0,3),(10,10)),replace(policy(),reference_max_gap_ns=10**8))):
            result=encode(pred,compare(pred,ref,chosen),CodecTests().snapshot())
            rows=[dict(kind=block['kind'],columns=block['columns'],values=row)
                  for raw in result.chunks for block in (json.loads(raw),) for row in block['rows']]
            self.run_fixture('reference-edges',encoded_rows=rows)
        for row in rows:
            if row['kind']=='comparison': row['columns']=row['columns'][:-1]; row['values']=row['values'][:-1]
        self.run_fixture('reference-legacy',encoded_rows=rows)

    def test_result_states_not_collapsed_into_success(self):
        self.run_fixture('result-states')

    def test_complete_paging_identity_and_disposal(self):
        self.run_fixture('paging')

    def test_review_revoke_export_and_metadata_permission(self):
        self.run_fixture('review-export')

    def test_per_kind_and_aggregate_point_bounds_refused(self):
        self.run_fixture('bounds')


class InversionParametersBrowserTests(BrowserFixtureCase):

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
        self.run_fixture('round1-attachment')
        self.run_fixture('round1-attachment-pending')

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
