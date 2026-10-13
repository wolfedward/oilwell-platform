"""Independent workbench cookies and real HTTP authorization boundaries."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.http_server import create_server
from backend.registry.runtime import RegistryRuntime
from backend.review_runtime import initialize_identity
from backend.studies.api import StudyApi


class MonitoringSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'identity.db'
        self.accounts = initialize_identity(self.path)['accounts']
        try:
            self.server = create_server('127.0.0.1', 0, self.path, runtime_mode='review',
                                        ui_mode='monitoring', studies_engines={})
        except TypeError as error:
            self.fail(f'Independent monitoring server is not available: {error}')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()

    def request(self, method, path, body=None, *, cookie=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        request_headers = {'Content-Type': 'application/json'} | (headers or {})
        if cookie is not None:
            request_headers['Cookie'] = cookie
        conn.request(method, path, None if body is None else json.dumps(body), request_headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        result = response.status, data, dict(response.getheaders())
        conn.close()
        return result

    def login(self):
        account = next(a for a in self.accounts if a['username'] == 'administrator')
        status, data, headers = self.request('POST', '/api/auth/login',
            {'username': account['username'], 'password': account['password']}, headers={'Origin': self.origin})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'], data['data']['token']

    def test_monitoring_cookie_isolated(self):
        cookie, token = self.login()
        self.assertTrue(cookie.startswith('oilwell_monitoring_session='))
        self.assertNotIn('platform_session=', cookie)
        self.assertIn('HttpOnly; SameSite=Strict; Path=/', cookie)
        status, data, _ = self.request('GET', '/api/auth/me', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(data['data']['ui_mode'], 'monitoring')
        self.assertEqual(data['data']['capabilities']['monitoring']['status'], 'disabled')
        for old_name in ('platform_session', 'oilwell_page_preview'):
            self.assertEqual(self.request('GET', '/api/auth/me', cookie=f'{old_name}={token}')[0], 401)
        status, _, headers = self.request('POST', '/api/auth/logout', cookie=cookie,
                                          headers={'Origin': self.origin})
        self.assertEqual(status, 200)
        self.assertTrue(headers['Set-Cookie'].startswith('oilwell_monitoring_session=;'))
        self.assertEqual(self.request('GET', '/api/auth/me', cookie=cookie)[0], 401)

    def test_duplicate_cookie_and_invalid_bearer_do_not_fall_back(self):
        cookie, token = self.login()
        duplicate = f'oilwell_monitoring_session={token}; oilwell_monitoring_session={token}'
        self.assertEqual(self.request('GET', '/api/auth/me', cookie=duplicate)[0], 401)
        for auth in ('Bearer invalid', 'Bearer ', 'Basic ignored'):
            self.assertEqual(self.request('GET', '/api/auth/me', cookie=cookie,
                                         headers={'Authorization': auth})[0], 401)

    def test_cookie_writes_and_login_require_exact_origin(self):
        account = self.accounts[0]
        body = {'username': account['username'], 'password': account['password']}
        for headers in ({}, {'Origin': 'http://127.0.0.1:8765'}, {'Origin': 'null'}):
            self.assertEqual(self.request('POST', '/api/auth/login', body, headers=headers)[0], 403)
        cookie, _ = self.login()
        self.assertEqual(self.request('POST', '/api/auth/logout', cookie=cookie)[0], 403)
        self.assertEqual(self.request('GET', '/api/auth/me', cookie=cookie)[0], 200)

    def test_monitoring_gate(self):
        self.assertIsNone(self.server.simulator)
        self.assertIsNone(self.server.acquisition_worker)
        cookie, _ = self.login()
        for method, path in [('GET', '/api/wells'), ('POST', '/api/commands'),
                             ('GET', '/api/production-studies'), ('GET', '/api/central/telemetry'),
                             ('POST', f'/api/ingest-reliability/receipts/{uuid4()}/resend')]:
            self.assertEqual(self.request(method, path, {} if method == 'POST' else None,
                                          cookie=cookie, headers={'Origin': self.origin})[0], 404)
        self.assertEqual(self.request('GET', '/api/registry/wells', cookie=cookie)[0], 503)
        with self.assertRaises(ValueError):
            create_server('127.0.0.1', 0, Path(self.tmp.name) / 'not-created.db', ui_mode='monitoring')
        self.assertFalse((Path(self.tmp.name) / 'not-created.db').exists())

    def test_reauthentication_on_retry(self):
        # A real authenticated HTTP boundary, with only the external PG service replaced.
        # Reaching the receipt store after revoked authority is the break this catches.
        class ReceiptStore:
            def __init__(self): self.receipts = {}
            def execute(self, command):
                key = str(command.key)
                replayed = key in self.receipts
                self.receipts[key] = {'replayed': replayed, 'entity': {'id': 'stored'}}
                return self.receipts[key]
        store = ReceiptStore()
        self.server.registry_runtime = RegistryRuntime('ready', store, object(),
                                                        allowed_origins=(self.origin,))
        cookie, _ = self.login()
        body = {'area_code': 'AREA', 'well_code': 'WELL', 'name': 'Well',
                'reason': 'register well', 'idempotency_key': str(uuid4())}
        self.assertEqual(self.request('POST', '/api/registry/wells', body, cookie=cookie,
                                      headers={'Origin': self.origin})[0], 201)
        self.assertTrue(self.request('POST', '/api/registry/wells', body, cookie=cookie,
                                     headers={'Origin': self.origin})[1]['data']['replayed'])
        with self.server.database.connection() as conn:
            conn.execute("DELETE FROM role_permissions WHERE permission_id=(SELECT id FROM permissions WHERE code='registry.wells.manage')")
        self.assertEqual(self.request('POST', '/api/registry/wells', body, cookie=cookie,
                                      headers={'Origin': self.origin})[0], 403)
        with self.server.database.connection() as conn:
            conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.request('POST', '/api/registry/wells', body, cookie=cookie,
                                      headers={'Origin': self.origin})[0], 401)
        self.assertEqual(self.request('GET', '/api/registry/wells', cookie=cookie)[0], 401)

    def test_case_actions_reach_authorized_service_but_resend_does_not(self):
        from backend.ingest_reliability.runtime import ReliabilityRuntime
        self.server.ingest_runtime = ReliabilityRuntime('disabled', code='ingest.disabled',
                                                        allowed_origins=(self.origin,))
        cookie, _ = self.login()
        for action in ('ack', 'note', 'close', 'reopen'):
            status, data, _ = self.request('POST', f'/api/ingest-reliability/cases/{uuid4()}/{action}',
                {'reason': 'review observation', 'expected_version': '1', 'idempotency_key': str(uuid4())},
                cookie=cookie, headers={'Origin': self.origin})
            self.assertEqual(status, 503, data)
            self.assertEqual(data['error']['code'], 'ingest.disabled')

    def test_studies_cookie_reader_uses_server_identity(self):
        from email.message import Message
        cookie, token = self.login()
        headers = Message()
        headers['Cookie'] = cookie
        adapter = StudyApi(SimpleNamespace(server=self.server, headers=headers))
        self.assertEqual(adapter._token(), token)
        headers.replace_header('Cookie', f'platform_session={token}')
        from backend.studies.contracts import StudyError
        with self.assertRaises(StudyError): adapter._token()


class MonitoringLaunchTests(unittest.TestCase):
    def test_hidden_child_keeps_ui_mode(self):
        import server
        with patch('sys.argv', ['server.py', '--runtime-mode=review', '--ui-mode=monitoring']):
            try:
                args = server.parse_arguments()
            except SystemExit:
                self.fail('CLI does not accept monitoring UI')
        command = server._launch_command('127.0.0.1', 8774, 'owned', runtime_mode=args.runtime_mode,
                                         ui_mode=args.ui_mode)
        self.assertIn('--ui-mode=monitoring', command)
        self.assertIn('--runtime-mode=review', command)
