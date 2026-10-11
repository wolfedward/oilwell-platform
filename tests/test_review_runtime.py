"""Review startup uses explicitly initialized identity, never demo seeding."""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.auth import AuthService, InvalidCredentials
from backend.database import Database
from backend.http_server import create_server, PlatformHTTPServer
from backend.review_runtime import initialize_identity, check_identity


class ReviewRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'identity.db'

    def initialize(self):
        return initialize_identity(self.path)

    def running(self):
        server = create_server('127.0.0.1', 0, self.path, runtime_mode='review', studies_engines={})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            thread.join(3)
            server.server_close()
        self.addCleanup(close)
        return server

    def request(self, server, method, path, body=None, cookie=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        request_headers = {'Content-Type': 'application/json'} | (headers or {})
        if cookie:
            request_headers['Cookie'] = cookie
        connection.request(method, path, json.dumps(body) if body is not None else None, request_headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, data, response_headers

    def test_missing_identity_refuses_without_creating_any_file(self):
        with self.assertRaises(ValueError):
            create_server('127.0.0.1', 0, self.path, runtime_mode='review', studies_engines={})
        self.assertFalse(self.path.exists())

    def test_explicit_init_has_random_credentials_no_demo_records(self):
        result = self.initialize()
        self.assertEqual(len(result['accounts']), 5)
        auth = AuthService(Database(self.path))
        for account in result['accounts']:
            self.assertGreaterEqual(len(account['password']), 24)
            self.assertEqual(auth.me(auth.login(account['username'], account['password']).token).username,
                             account['username'])
        with self.assertRaises(InvalidCredentials):
            auth.login('administrator', 'Admin@2026')
        db = Database(self.path)
        for table in ('wells', 'devices', 'telemetry', 'seed_manifests', 'study_records'):
            self.assertEqual(db.scalar('SELECT COUNT(*) FROM ' + table), 0)
        admin = auth.me(auth.login('administrator', result['accounts'][0]['password']).token)
        all_permissions = set(db.column('SELECT code FROM permissions'))
        self.assertEqual(admin.permissions, all_permissions)
        self.assertIn('studies.review', admin.permissions)

    def test_existing_file_is_not_reinitialized_or_regranted(self):
        self.initialize()
        db = Database(self.path)
        with db.connection() as conn:
            conn.execute("DELETE FROM role_permissions WHERE permission_id=(SELECT id FROM permissions WHERE code='studies.create')")
        with self.assertRaises(ValueError):
            self.initialize()
        with patch('backend.http_server.seed_database', side_effect=AssertionError('seed called')), \
             patch.object(Database, 'migrate', side_effect=AssertionError('migrate called')):
            server = self.running()
            status, health, _ = self.request(server, 'GET', '/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(health['data']['runtime_mode'], 'review')
        self.assertEqual(health['data']['simulator'], 'disabled')
        self.assertFalse(health['data']['control_enabled'])
        self.assertIsNone(server.simulator)
        self.assertIsNone(server.acquisition_worker)
        self.assertIsNone(server.workflow_worker)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id WHERE p.code='studies.create'"), 0)

    def test_startup_readonly_and_schema_tamper_refusal(self):
        self.initialize()
        first = hashlib.sha256(self.path.read_bytes()).hexdigest()
        check_identity(self.path)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), first)
        with Database(self.path).connection() as conn:
            conn.execute('DROP TRIGGER study_records_no_update')
        with self.assertRaises(ValueError):
            check_identity(self.path)

    def test_review_routes_and_real_login(self):
        accounts = self.initialize()['accounts']
        server = self.running()
        status, _, headers = self.request(server, 'POST', '/api/auth/login', {
            'username': accounts[0]['username'], 'password': accounts[0]['password']})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        status, me, _ = self.request(server, 'GET', '/api/auth/me', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(me['data']['runtime_mode'], 'review')
        self.assertTrue(me['data']['capabilities']['production_studies']['enabled'])
        status, listing, _ = self.request(server, 'GET', '/api/production-studies', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(listing['data'], [])
        for method, route in (('GET', '/api/wells'), ('POST', '/api/commands'),
                              ('POST', '/api/acquisition/replay'), ('PATCH', '/api/devices/1')):
            status, body, _ = self.request(server, method, route, {}, cookie)
            self.assertEqual(status, 404, (method, route, body))

    def test_foreign_origin_login_rejected_and_loopback_only(self):
        self.initialize()
        server = self.running()
        self.assertEqual(self.request(server, 'POST', '/api/auth/login', {}, headers={'Origin': 'https://evil.invalid'})[0], 403)
        with self.assertRaises(ValueError):
            create_server('0.0.0.0', 0, self.path, runtime_mode='review', studies_engines={})

    def test_invalid_mode_never_creates_or_seeds(self):
        with self.assertRaises(ValueError):
            create_server('127.0.0.1', 0, self.path, runtime_mode='typo')
        self.assertFalse(self.path.exists())

    def test_review_identity_cannot_be_opened_in_default_demo_mode(self):
        self.initialize()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            create_server('127.0.0.1', 0, self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_launcher_carries_review_mode(self):
        from server import _launch_command
        args = _launch_command('127.0.0.1', 8770, 'instance', runtime_mode='review')
        self.assertIn('--runtime-mode=review', args)

    def test_permission_revoked_before_admin_commit_prevents_write(self):
        accounts = self.initialize()['accounts']
        server = self.running()
        token = server.auth.login(accounts[0]['username'], accounts[0]['password']).token
        original = server.auth.require
        calls = []
        def revoke_after_first_check(token, permission):
            actor = original(token, permission)
            if permission == 'roles.manage' and not calls:
                calls.append(permission)
                with server.database.connection() as conn:
                    conn.execute("DELETE FROM role_permissions WHERE role_id=? AND permission_id=(SELECT id FROM permissions WHERE code='roles.manage')", (actor.role_id,))
            return actor
        with patch.object(server.auth, 'require', side_effect=revoke_after_first_check):
            status, _, _ = self.request(server, 'POST', '/api/roles',
                                       {'code': 'blocked-role', 'name': 'blocked', 'permissions': []},
                                       'platform_session=' + token)
        self.assertEqual(status, 403)
        self.assertEqual(server.database.scalar("SELECT COUNT(*) FROM roles WHERE code='blocked-role'"), 0)

    def test_review_login_strict_json(self):
        self.initialize()
        server = self.running()
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request('POST', '/api/auth/login', '{"username":"a","username":"b","password":"c"}',
                           {'Content-Type': 'application/json'})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 400)

    def test_unexpected_module_construction_failure_closes_bound_socket(self):
        self.initialize()
        created = []
        def capture(*args, **kwargs):
            server = PlatformHTTPServer(*args, **kwargs)
            created.append(server)
            return server
        try:
            with patch('backend.http_server.PlatformHTTPServer', side_effect=capture), \
                 patch('backend.http_server.build_inversion_runtime', side_effect=RuntimeError('constructor failed')):
                with self.assertRaisesRegex(RuntimeError, 'constructor failed'):
                    create_server('127.0.0.1', 0, self.path, runtime_mode='review')
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].socket.fileno(), -1)
        finally:
            for server in created:
                server.server_close()


if __name__ == '__main__':
    unittest.main()
