"""Small checks for the owned ingest browser fixture's local resources."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import ingest_stack


class BrowserFailureDiagnosticsTests(unittest.TestCase):
    fixture = Path(__file__).resolve().parent / 'fixtures/ingest_live_check.cjs'
    canary = 'R30-NONSECRET-INPUT-CANARY'

    def run_node(self, *arguments, timeout=30):
        node_root = Path(os.environ.get('USERPROFILE', '')) / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
        node = shutil.which('node') or str(node_root / 'bin/node.exe')
        return subprocess.run([node, *arguments], cwd=self.fixture.parents[2],
                              env=dict(os.environ, NODE_PATH=str(node_root / 'node_modules')),
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_failed_fixture_logs_only_allowlisted_diagnostics_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as evidence:
            supplied = f'<input value="{self.canary}">'
            result = self.run_node(str(self.fixture), supplied, evidence)
        self.assertEqual(result.returncode, 1, 'fixture failure must retain a failing exit code')
        self.assertFalse(self.canary in result.stdout + result.stderr,
                         'fixture failure log exposed supplied input')
        self.assertEqual(json.loads(result.stderr.strip()),
                         {'ok': False, 'phase': 'input_validation',
                          'error_kind': 'assertion', 'network': 'none'})

    def test_summary_drops_unknown_error_names_messages_and_stacks(self):
        script = f'''
const {{summarizeFailure}} = require({json.dumps(str(self.fixture))});
const canary = {json.dumps(self.canary)};
const hostile = {{name: 'Name-' + canary, message: 'net::ERR_UNSAFE_PORT ' + canary,
                 stack: '<input value="' + canary + '">', selector: canary}};
const unknown = {{name: 'Name-' + canary, message: canary, stack: canary}};
console.log(JSON.stringify(summarizeFailure('navigation', hostile)));
console.log(JSON.stringify(summarizeFailure('phase-' + canary, unknown)));
'''
        result = self.run_node('-e', script)
        self.assertEqual(result.returncode, 0, 'safe summary probe failed')
        self.assertFalse(self.canary in result.stdout + result.stderr,
                         'summary serialized supplied input')
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()], [
            {'ok': False, 'phase': 'navigation', 'error_kind': 'network', 'network': 'unsafe_port'},
            {'ok': False, 'phase': 'unknown', 'error_kind': 'unexpected', 'network': 'none'},
        ])

    def test_real_playwright_fill_timeout_drops_call_argument_and_html(self):
        script = f'''
const {{chromium}} = require('playwright');
const {{summarizeFailure}} = require({json.dumps(str(self.fixture))});
const canary = {json.dumps(self.canary)};
(async () => {{
  const browser = await chromium.launch({{
    executablePath: process.env.OILWELL_TEST_BROWSER ||
      'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true}});
  try {{
    const page = await browser.newPage();
    await page.setContent('<input id="password" type="password" disabled value="' + canary + '">');
    let failure;
    try {{ await page.locator('#password').fill(canary, {{timeout: 300}}); }}
    catch (error) {{ failure = error; }}
    if (!failure || !String(failure.stack).includes(canary)) process.exitCode = 2;
    else console.log(JSON.stringify({{
      raw_contains_canary: true, safe: summarizeFailure('login', failure)}}));
  }} finally {{ await browser.close(); }}
}})().catch(() => {{ process.exitCode = 2; }});
'''
        result = self.run_node('-e', script, timeout=45)
        self.assertEqual(result.returncode, 0, 'real browser diagnostic probe failed')
        self.assertFalse(self.canary in result.stdout + result.stderr,
                         'real browser diagnostic serialized input or page HTML')
        self.assertEqual(json.loads(result.stdout.strip()), {
            'raw_contains_canary': True,
            'safe': {'ok': False, 'phase': 'login', 'error_kind': 'timeout', 'network': 'none'},
        })


class BrowserHttpPortTests(unittest.TestCase):
    def test_port_exhaustion_starts_no_owned_stack_resources(self):
        with patch.object(ingest_stack, 'free_browser_http_port', side_effect=RuntimeError('no browser HTTP port')), \
             patch.object(ingest_stack.tempfile, 'TemporaryDirectory',
                          side_effect=AssertionError('temporary stack created')), \
             patch.object(ingest_stack, 'Broker', side_effect=AssertionError('broker started')):
            with self.assertRaisesRegex(RuntimeError, 'no browser HTTP port'):
                ingest_stack.Stack(None, http=True)

    def test_http_stack_never_uses_an_unsafe_generic_ephemeral_port(self):
        with patch.object(ingest_stack, 'Broker', return_value=Mock()), \
             patch.object(ingest_stack, 'free_port', return_value=1723):
            stack = ingest_stack.Stack(None, http=True)
        try:
            self.assertGreaterEqual(stack.http_port, 49152)
            self.assertLessEqual(stack.http_port, 65535)
            self.assertEqual(stack.url, f'http://127.0.0.1:{stack.http_port}')
        finally:
            stack.temp.cleanup()

    def test_http_port_retries_a_busy_loopback_port_and_returns_an_available_one(self):
        self.assertTrue(hasattr(ingest_stack, 'free_browser_http_port'))
        with socket.socket() as held:
            for candidate in range(49152, 65536):
                try:
                    held.bind(('127.0.0.1', candidate))
                    break
                except OSError:
                    continue
            else:
                self.fail('No loopback port available for the fixture test')
            busy = held.getsockname()[1]
            for available in range(busy + 1, 65536):
                try:
                    with socket.socket() as probe:
                        probe.bind(('127.0.0.1', available))
                    break
                except OSError:
                    continue
            else:
                self.fail('No second loopback port available for the fixture test')
            with patch.object(ingest_stack.random, 'randrange', side_effect=[busy, available]):
                selected = ingest_stack.free_browser_http_port()
            self.assertEqual(selected, available)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', selected))

    def test_http_port_exhaustion_is_bounded_and_clear(self):
        self.assertTrue(hasattr(ingest_stack, 'free_browser_http_port'))
        class OccupiedSocket:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def bind(self, address):
                if address[0] != '127.0.0.1':
                    raise AssertionError('The fixture must not bind a public address')
                raise OSError('occupied')

        with patch.object(ingest_stack.socket, 'socket', side_effect=lambda *args: OccupiedSocket()) as sockets, \
             patch.object(ingest_stack.random, 'randrange', return_value=49152):
            with self.assertRaisesRegex(RuntimeError, '49152.*65535'):
                ingest_stack.free_browser_http_port()
        self.assertEqual(sockets.call_count, 65536 - 49152)


class BrowserLogEvidenceTests(unittest.TestCase):
    def stack_without_owned_processes(self, parent):
        stack = object.__new__(ingest_stack.Stack)
        stack.temp = tempfile.TemporaryDirectory(dir=parent)
        stack.root = Path(stack.temp.name)
        stack.logs = {}
        stack.processes = {}
        stack.handles = []
        stack.broker = Mock()
        stack.run_id = 'browser-log-unit'
        stack.url = 'http://127.0.0.1:49152'
        return stack

    def test_browser_log_survives_failure_cleanup_in_owned_evidence_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            evidence = parent / 'evidence'
            evidence.mkdir()
            stack = self.stack_without_owned_processes(parent)

            def finished_browser(argv, **kwargs):
                kwargs['stdout'].write(b'{"ok": true}\n')
                return SimpleNamespace(poll=lambda: 0, returncode=0, stdin=None)

            with patch.dict(os.environ, {'OILWELL_INGEST_EVIDENCE_DIR': str(evidence)}), \
                 patch.object(ingest_stack.subprocess, 'Popen', side_effect=finished_browser):
                self.assertTrue(stack.browser_case_flow()['ok'])
                stack.__exit__(AssertionError, AssertionError('synthetic failure'), None)

            retained = evidence / 'failure-browser-log-unit' / 'browser.log'
            self.assertTrue(retained.is_file(), 'browser log was deleted with the temporary stack')
            self.assertEqual(retained.read_bytes(), b'{"ok": true}\n')
            self.assertFalse(stack.root.exists())

    def test_evidence_copy_failure_still_cleans_the_owned_stack(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            evidence = parent / 'evidence'
            evidence.mkdir()
            stack = self.stack_without_owned_processes(parent)
            browser_log = stack.root / 'browser.log'
            browser_log.write_bytes(b'synthetic browser failure\n')
            stack.logs['browser'] = browser_log

            with patch.dict(os.environ, {'OILWELL_INGEST_EVIDENCE_DIR': str(evidence)}), \
                 patch.object(ingest_stack.shutil, 'copyfile', side_effect=OSError('copy failed')):
                with self.assertRaisesRegex(OSError, 'copy failed'):
                    stack.__exit__(AssertionError, AssertionError('synthetic failure'), None)

            self.assertFalse(stack.root.exists())


if __name__ == '__main__':
    unittest.main()
