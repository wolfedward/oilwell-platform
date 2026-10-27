"""Controlled display fixtures, never field or actual-release evidence."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import http.client
import json
import os
import subprocess
import sys
import unittest

from tools.testpack import viewer
from tools.testpack.report import project_inversion_outcome
from tools.testpack.storage import canonical_bytes
from tests.test_testpack_report_v2 import positive
from tests.test_inversion_browser_v2 import rows


def retained_fixture():
    summary = project_inversion_outcome(positive())
    blocks = []
    for row in rows():
        if not blocks or blocks[-1]['kind'] != row['kind']:
            blocks.append(dict(kind=row['kind'], columns=row['columns'], rows=[]))
        blocks[-1]['rows'].append(row['values'])
    result = dict(schema='inversion.result/2', point_count=1025,
        source_kind='synthetic', field_accuracy_certified=False,
        external_evidence_admission_required=True, prediction_status='applicable',
        prediction_reason_codes=[], parameter_core_sha256='c'*64,
        source_proof=dict(status='verified', reason_codes=[], case_id='cycle-base', config_sha256='d'*64))
    export = dict(schema='inversion.export/1', id=summary['record_ids']['export_id'],
        result_id=summary['record_ids']['result_id'], result_sha256=summary['digests']['result_sha256'],
        intent='quantitative', format='json', restricted=False, restriction_reasons=[],
        current_review=dict(state='approved', version=1), checked_at='2026-10-01T00:00:00+00:00',
        historical=dict(execution='completed', source='synthetic', review=dict(state='approved', version=1),
            current_applicability=dict(state='applicable', reasons=[]),
            field_validation=dict(certified=False, independent_comparison=False)),
        result=result, chunks=blocks)
    return summary, export


class RetainedViewerFixture(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.summary, self.export = retained_fixture()

    def load(self):
        function = getattr(viewer, 'load_retained_result', None)
        self.assertTrue(callable(function), 'approved retained-result transport is missing')
        return function(self.summary, self.root, 'cycle-base')

    def store(self, *, rebind=True):
        raw = canonical_bytes(self.export)
        (self.root/'cycle-base-export.json').write_bytes(raw)
        if rebind:
            self.summary['digests']['export_sha256'] = sha256(raw).hexdigest()


class RetainedViewerResultTests(RetainedViewerFixture):
    def test_approved_export_loads_all_1025_native_points_and_preserves_gap(self):
        self.store()
        value = self.load()
        self.assertEqual(len(value['prediction']), 1025)
        self.assertEqual(len(value['proof']), 1025)
        self.assertEqual(len(value['reference']), 1025)
        self.assertEqual(value['prediction'][0]['s_m'], '1/3')
        self.assertEqual(value['prediction'][0]['load_N'], '2000/3')
        self.assertIsNone(value['prediction'][10]['load_N'])
        self.assertEqual(value['prediction'][10]['status'], 'input_gap')
        self.assertEqual(value['prediction'][-1]['power_index'], '1024')
        self.assertEqual(value['proof'][-1]['power_index'], '1024')
        self.assertEqual(value['provenance']['scope'], 'historical_export_only')
        self.assertFalse(value['provenance']['current_authorization_checked'])
        self.assertFalse(value['provenance']['field_accuracy_certified'])

    def test_changed_bytes_are_rejected_not_recomputed(self):
        self.store()
        self.export['chunks'][0]['rows'][0][2] = '999'
        self.store(rebind=False)
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.load()

    def test_numeric_syntax_is_bounded_before_fraction_conversion(self):
        for value in (' 1/3 ', '1_000', '1e100000'):
            with self.subTest(value=value):
                self.summary, self.export = retained_fixture()
                block = self.export['chunks'][0]
                block['rows'][0][block['columns'].index('s_m')] = value
                self.store()
                with self.assertRaises(ValueError): self.load()

    def test_native_count_must_be_an_integer_not_a_display_string(self):
        self.export['result']['point_count'] = '1025'
        self.store()
        with self.assertRaises(ValueError): self.load()

    def test_wrong_case_result_export_or_source_is_rejected_even_with_rebound_file_digest(self):
        for mutation in ('case', 'result', 'export', 'result_digest', 'field', 'source'):
            with self.subTest(mutation=mutation):
                self.summary, self.export = retained_fixture()
                if mutation == 'case': self.export['result']['source_proof']['case_id'] = 'cycle-speed'
                if mutation == 'result': self.export['result_id'] = '00000000-0000-4000-8000-000000000001'
                if mutation == 'export': self.export['id'] = '00000000-0000-4000-8000-000000000001'
                if mutation == 'result_digest': self.export['result_sha256'] = 'f'*64
                if mutation == 'field': self.export['result']['field_accuracy_certified'] = True
                if mutation == 'source': self.export['result']['source_kind'] = 'field_measurement'
                self.store()
                with self.assertRaises(ValueError): self.load()

    def test_restricted_unreviewed_or_failed_export_is_not_shown_as_approved(self):
        for mutation in ('restricted', 'review', 'historical_review', 'failed', 'intent', 'applicability'):
            with self.subTest(mutation=mutation):
                self.summary, self.export = retained_fixture()
                if mutation == 'restricted': self.export['restricted'] = True
                if mutation == 'review': self.export['current_review']['state'] = 'revoked'
                if mutation == 'historical_review': self.export['historical']['review']['state'] = 'unreviewed'
                if mutation == 'failed': self.export['historical']['execution'] = 'failed'
                if mutation == 'intent': self.export['intent'] = 'evidence'
                if mutation == 'applicability': self.export['historical']['current_applicability']['state'] = 'revoked'
                self.store()
                with self.assertRaises(ValueError): self.load()

    def test_missing_duplicate_misaligned_or_unknown_native_rows_are_rejected(self):
        for mutation in ('short_prediction', 'short_proof', 'duplicate_index', 'wrong_time', 'column', 'kind'):
            with self.subTest(mutation=mutation):
                self.summary, self.export = retained_fixture()
                blocks = self.export['chunks']
                if mutation == 'short_prediction': blocks[0]['rows'].pop()
                if mutation == 'short_proof': blocks[2]['rows'].pop()
                if mutation == 'duplicate_index': blocks[0]['rows'][1][1] = '0'
                if mutation == 'wrong_time': blocks[2]['rows'][1][0] = '2'
                if mutation == 'column': blocks[0]['columns'][2] = '__proto__'
                if mutation == 'kind': blocks[2]['kind'] = 'private_data'
                self.store()
                with self.assertRaises(ValueError): self.load()

    def test_refused_summary_never_reads_or_fabricates_prediction(self):
        from tools.testpack.inversion_http_v2 import InversionOutcome
        self.summary = project_inversion_outcome(InversionOutcome(
            'cycle-base', 'passed', 'refused', 'refused', 'worker', 'not_applicable', 'refused', {}))
        self.assertIsNone(self.load())

    def test_missing_or_oversize_export_is_rejected(self):
        with self.assertRaises(ValueError): self.load()
        path = self.root/'cycle-base-export.json'
        with path.open('wb') as stream:
            stream.seek(16*1024*1024)
            stream.write(b'X')
        with self.assertRaises(ValueError): self.load()

    def test_hardlinked_evidence_and_traversing_case_are_rejected(self):
        self.store()
        os.link(self.root/'cycle-base-export.json', self.root/'alias.json')
        with self.assertRaises(ValueError): self.load()
        with self.assertRaises(ValueError):
            viewer.load_retained_result(self.summary, self.root, '../cycle-base')


class RetainedViewerHttpTests(RetainedViewerFixture):
    def prepare_public(self):
        from tools.testpack.contracts import PackConfig, Cycle, Channel
        from tools.testpack.waveforms import encode_cycle
        from tools.testpack.storage import write_pack, generator_for, strict_json
        from tools.testpack import report
        config = PackConfig()
        channels = tuple(Channel(role, unit, scale, '0', (value,)*1025, (0,)*1025)
            for role, unit, scale, value in (
                ('active_power', 'W', '0.01', 100000), ('crank_angle', 'rad', '0.000001', 1000000),
                ('load', 'kN', '0.001', 700), ('displacement', 'm', '0.000001', 333333)))
        case = encode_cycle(Cycle('cycle-base', 128, tuple(i*7812500 for i in range(1025)),
            channels, 512, {}, {'category': 'mechanistic'}), config)
        pack = write_pack(self.root/'pack', config, (case,), generator_for(config))
        report_root = self.root/'report'; report_root.mkdir()
        report.write_report(pack, (), report_root, run_metadata={}, inversion_outcomes=(positive(),))
        path = report.export_public_report(report_root, self.root/'public')
        body = strict_json(path.read_bytes())
        body['inversion_outcomes'] = [self.summary]
        path.write_bytes(canonical_bytes(body))
        return pack, path

    def test_http_serves_digest_bound_native_data_and_rechecks_changed_file(self):
        self.store(); pack, report = self.prepare_public()
        try: server = viewer.serve_viewer(pack, report, results_root=self.root)
        except TypeError: self.fail('viewer does not accept retained result evidence')
        try:
            def request(method, path):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request(method, path)
                response = connection.getresponse(); raw = response.read(); connection.close()
                return response.status, raw
            status, raw = request('GET', '/data/cases/cycle-base.json')
            self.assertEqual(status, 200)
            data = json.loads(raw)
            self.assertEqual(data['inversion_status'], 'retained_approved_export')
            self.assertEqual(len(data['prediction']), 1025)
            self.assertEqual(len(data['retained_result']['proof']), 1025)
            self.assertFalse(data['retained_result']['provenance']['current_authorization_checked'])
            for path in ('/private/accounts.json', '/api/production-inversion/results', '/command-intents'):
                self.assertEqual(request('GET', path)[0], 404)
            self.assertEqual(request('POST', '/data/cases/cycle-base.json')[0], 405)
            self.export['chunks'][0]['rows'][0][2] = '999'; self.store(rebind=False)
            self.assertEqual(request('GET', '/data/cases/cycle-base.json')[0], 409)
        finally:
            server.shutdown(); server.server_close(); server.viewer_thread.join(5)

    def test_results_root_requires_explicit_bound_public_report(self):
        self.store(); pack, _ = self.prepare_public()
        try:
            with self.assertRaises(ValueError): viewer.serve_viewer(pack, None, results_root=self.root)
        except TypeError: self.fail('viewer retained result admission is missing')

    def test_export_whitelist_contains_retained_reader_module(self):
        from tools.review_deploy_source import _included
        self.assertTrue(_included('tools/testpack/viewer_results.py'))

    def test_cli_explicit_results_serves_the_same_native_projection(self):
        self.store(); pack, report = self.prepare_public()
        process = subprocess.Popen([sys.executable, '-B', '-m', 'tools.testpack', 'view',
            '--pack', str(pack.root), '--report', str(report), '--results', str(self.root), '--port', '0'],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(process.stdout.readline)
                try: line = pending.result(timeout=10)
                except TimeoutError:
                    process.terminate(); raise
            endpoint = json.loads(line)
            self.assertTrue(endpoint['read_only'])
            from urllib.parse import urlsplit
            url = urlsplit(endpoint['url'])
            self.assertEqual(url.hostname, '127.0.0.1')
            connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
            try:
                connection.request('GET', '/data/cases/cycle-base.json')
                response = connection.getresponse(); data = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(len(data['retained_result']['proof']), 1025)
            finally: connection.close()
        finally:
            if process.poll() is None: process.terminate()
            process.communicate(timeout=10)


if __name__ == '__main__': unittest.main()
