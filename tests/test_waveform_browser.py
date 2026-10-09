"""Edge exercises actual waveform modules; doubles stop at the HTTP boundary."""
import base64
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from backend.waveforms.files import scan_file
from backend.waveforms.errors import ContractError
from tests.waveform_vectors import bit_pattern_capture, golden_capture, encoded

ROOT = Path(__file__).resolve().parent.parent


def vectors():
    manifest, chunks = golden_capture()
    original = b'\n'.join((manifest, *chunks))
    rows = [('golden', original), ('final-lf', original + b'\n'),
            ('reordered', b'\n'.join((manifest, *reversed(chunks)))),
            ('count-minus-zero', b'\n'.join((manifest, chunks[0].replace(b'"chunk_index":0', b'"chunk_index":-0'), *chunks[1:]))),
            ('bom', b'\xef\xbb\xbf' + original), ('crlf', original.replace(b'\n', b'\r\n')),
            ('empty-line', original + b'\n\n'), ('missing', b'\n'.join((manifest, *chunks[:-1]))),
            ('duplicate-chunk', original + b'\n' + chunks[0]), ('invalid-utf8', b'\xff' + original),
            ('duplicate-key', original.replace(b'"kind":"manifest"', b'"kind":"manifest","kind":"manifest"', 1)),
            ('fraction-count', original.replace(b'"chunk_index":0', b'"chunk_index":0.0', 1)),
            ('exponent-count', original.replace(b'"chunk_index":0', b'"chunk_index":0e0', 1))]
    for label, changed in [
            ('manifest-exact-limit', manifest + b' ' * (65536 - len(manifest))),
            ('manifest-over-limit', manifest + b' ' * (65537 - len(manifest)))]:
        parts = [c.replace(sha256(manifest).hexdigest().encode(), sha256(changed).hexdigest().encode()) for c in chunks]
        rows.append((label, b'\n'.join((changed, *parts))))
    rows.extend([
        ('chunk-exact-limit', b'\n'.join((manifest, chunks[0] + b' ' * (262144 - len(chunks[0])), *chunks[1:]))),
        ('chunk-over-limit', b'\n'.join((manifest, chunks[0] + b' ' * (262145 - len(chunks[0])), *chunks[1:]))),
        ('unknown-chunk-field', b'\n'.join((manifest, chunks[0][:-1] + b',"extra":1}', *chunks[1:]))),
        ('foreign-capture', b'\n'.join((manifest, chunks[0].replace(b'11111111-1111-4111-8111-111111111111', b'99999999-1111-4111-8111-111111111111'), *chunks[1:]))),
        ('wrong-start', b'\n'.join((manifest, chunks[0].replace(b'"sample_start":0', b'"sample_start":1'), *chunks[1:]))),
        ('wrong-digest', b'\n'.join((manifest, chunks[0].replace(b'"data_sha256":"', b'"data_sha256":"f'), *chunks[1:]))),
        ('truncated', original[:-8]), ('zip', b'PK\x03\x04' + original),
        ('manifest-literal-lf', manifest.replace(b'Synthetic Test', b'Synthetic\nTest') + b'\n' + b'\n'.join(chunks)),
    ])
    for label, bits in [('positive-infinity', '0000807f'), ('negative-infinity', '000080ff'), ('nan-sample', '0000c07f')]:
        rows.append((label, bit_pattern_capture(bytes.fromhex(bits), 'f32le')))
        rows.append((label + '-i32', bit_pattern_capture(bytes.fromhex(bits), 'i32le')))
    changed = json.loads(chunks[0]); changed['data_base64'] += '\n'
    rows.append(('noncanonical-base64', b'\n'.join((manifest, encoded(changed), *chunks[1:]))))
    for label, config in [('null-config', None), ('opaque-config', 'CFG1'), ('bad-config-int', 0), ('bad-config-bool', False), ('bad-config-space', 'unknown label')]:
        body = json.loads(manifest)
        body['streams'][0]['channels'][0]['config_id'] = config
        raw = encoded(body)
        parts = [chunk.replace(sha256(manifest).hexdigest().encode(), sha256(raw).hexdigest().encode()) for chunk in chunks]
        rows.append((label, b'\n'.join((raw, *parts))))
    for label, mutation in [('i64-minus-zero', lambda b: b['streams'][0].update(first_sample_index='-0')),
                            ('unknown-field', lambda b: b.update(extra=1)),
                            ('nested-depth', lambda b: b.update(source_evidence={'x': [[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]})),
                            ('boolean-count', lambda b: b['streams'][0].update(rate_num=True))]:
        body = json.loads(manifest); mutation(body)
        raw = encoded(body)
        rows.append((label, b'\n'.join((raw, *[c.replace(sha256(manifest).hexdigest().encode(), sha256(raw).hexdigest().encode()) for c in chunks]))))
    output = []
    for label, raw in rows:
        if label in ('positive-infinity', 'negative-infinity', 'nan-sample'):
            output.append(dict(name=label, base64=base64.b64encode(raw).decode(), accepted=False, reason_code='layout_invalid'))
            continue
        if label.endswith('-i32'):
            output.append(dict(name=label, base64=base64.b64encode(raw).decode(), accepted=True,
                               hash=sha256(raw).hexdigest(), lines=len(chunks)+1, final_lf=False))
            continue
        try:
            desc = scan_file(BytesIO(raw))
            expected = {'accepted': True, 'hash': desc.sha256, 'lines': len(desc.lines), 'final_lf': desc.final_lf}
        except ContractError as error:
            expected = {'accepted': False, 'reason_code': error.code}
        output.append(dict(name=label, base64=base64.b64encode(raw).decode(), **expected))
    return output


class WaveformBrowserTests(unittest.TestCase):
    def run_fixture(self, name, width=1440, mode='normal', with_vectors=False):
        source = (ROOT / 'tests/fixtures' / name).read_text(encoding='utf-8')
        for marker, path in {'STYLE_MODULE': 'styles.css', 'APP_MODULE': 'app.js',
                             'PAGE_MODULE': 'frontend/pages/waveforms.js',
                             'CLIENT_MODULE': 'frontend/waveforms/client.js',
                             'CHART_MODULE': 'frontend/waveforms/charts.js',
                             'IMPORT_MODULE': 'frontend/waveforms/imports.js',
                             'API_MODULE': 'frontend/api.js'}.items():
            source = source.replace(marker, json.dumps((ROOT / path).as_uri()))
        source = source.replace('FIXTURE_MODE', mode).replace('VECTOR_DATA', json.dumps(vectors() if with_vectors else []))
        runtime = Path(os.environ['USERPROFILE']) / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
        with tempfile.TemporaryDirectory(prefix='waveform-ui-') as directory:
            page = Path(directory) / 'check.html'; page.write_text(source, encoding='utf-8')
            evidence = os.environ.get('OILWELL_WAVEFORM_UI_EVIDENCE')
            screenshot = ''
            if evidence:
                destination = Path(evidence); destination.mkdir(parents=True, exist_ok=True)
                screenshot = str(destination / f'{Path(name).stem}-{mode}-{width}.png')
            result = subprocess.run([str(runtime / 'bin/node.exe'), str(ROOT / 'tests/fixtures/waveform_fixture_runner.cjs'),
                                     page.as_uri(), str(width), screenshot], cwd=ROOT,
                                    env=dict(os.environ, NODE_PATH=str(runtime / 'node_modules')),
                                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stdout[-6000:] + result.stderr[-2000:])
            outcome = json.loads(result.stdout)
            self.assertTrue(outcome['ok'], outcome)

    def test_four_states_and_direct_route(self):
        for width in (1440, 1024):
            self.run_fixture('waveform_workspace_check.html', width)

    def test_two_decimal_hover_retains_exact_index(self):
        self.run_fixture('waveform_chart_check.html')

    def test_gaps_not_joined(self):
        self.run_fixture('waveform_chart_check.html', 1024)

    def test_resume_import_original_bytes(self):
        self.run_fixture('waveform_import_check.html', with_vectors=True)

    def test_definitive_import_rejections_allow_replacement_refresh_and_cancel(self):
        self.run_fixture('waveform_import_check.html', mode='rejection', with_vectors=True)

    def test_stale_selection_and_revoked_retry(self):
        for width in (1440, 1024):
            self.run_fixture('waveform_actions_check.html', width)

    def test_direct_route_shell_capability_and_logout(self):
        for mode in ('shell-disabled', 'shell-missing', 'shell-unavailable', 'shell-denied', 'shell-logout'):
            with self.subTest(mode=mode):
                self.run_fixture('waveform_workspace_check.html', 1024, mode)

    def test_management_envelopes_self_review_and_pagination(self):
        for width in (1440, 1024):
            self.run_fixture('waveform_actions_check.html', width, 'management')

    def test_import_network_disposal_and_raw_transport(self):
        self.run_fixture('waveform_import_check.html', 1024, 'lifecycle', with_vectors=True)

    def test_receiving_list_resolves_actual_metadata(self):
        for width in (1440, 1024):
            self.run_fixture('waveform_workspace_check.html', width, 'reception')

    def test_conflicting_decisions_preserve_original_recovery(self):
        self.run_fixture('waveform_actions_check.html', 1024, 'decisions')

    def test_denial_survives_view_and_workspace_until_reauthentication(self):
        self.run_fixture('waveform_actions_check.html', 1024, 'denial')

    def test_filter_and_detail_scopes_abort_replaced_reads(self):
        self.run_fixture('waveform_actions_check.html', 1024, 'cancellation')

    def test_pair_exact_history_measurement_and_cycle_display(self):
        for width in (1440, 1024):
            self.run_fixture('waveform_actions_check.html', width, 'history')


if __name__ == '__main__':
    unittest.main()
