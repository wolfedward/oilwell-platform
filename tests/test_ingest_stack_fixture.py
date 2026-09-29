"""Small checks for the owned ingest browser fixture's local resources."""

import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import ingest_stack


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
