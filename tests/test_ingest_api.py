"""Incident HTTP contracts and real local-auth / PostgreSQL transactions."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import http.client
import importlib
import io
import json
from email.message import Message
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from types import SimpleNamespace
from uuid import UUID, uuid4

from backend.ingest_reliability.errors import ReliabilityError
from tests.ingest_support import IngestDatabaseCase
from tests.ingest_vectors import observation


PREFIX = '/api/ingest-reliability'
IDENTIFIER = '00000000-0000-0000-0000-00000000000a'
PASSWORDS = {'administrator': 'Admin@2026', 'production_engineer': 'Production@2026',
             'algorithm_engineer': 'Algorithm@2026', 'maintenance_engineer': 'Maintenance@2026',
             'reviewer': 'Review@2026'}


class HttpContractTests(unittest.TestCase):
    def setUp(self):
        try:
            self.api = importlib.import_module('backend.ingest_reliability.api')
        except ModuleNotFoundError:
            self.fail('Incident HTTP boundary is missing')

    def test_known_routes_map_only_to_explicit_resources(self):
        routes = [('GET', '/cases', 'cases', None), ('GET', '/receipts', 'receipts', None),
                  ('GET', '/health', 'health', None), ('GET', '/audit', 'audit', None),
                  ('GET', '/cases/' + IDENTIFIER, 'case', UUID(IDENTIFIER)),
                  ('GET', '/receipts/' + IDENTIFIER, 'receipt', UUID(IDENTIFIER)),
                  ('GET', '/facts/' + IDENTIFIER + '/preview', 'raw.preview', UUID(IDENTIFIER)),
                  ('GET', '/facts/' + IDENTIFIER + '/raw', 'raw.download', UUID(IDENTIFIER))]
        routes += [('POST', '/cases/' + IDENTIFIER + '/' + name, 'case.' + name, UUID(IDENTIFIER))
                   for name in ('ack', 'close', 'reopen', 'note')]
        routes.append(('POST', '/receipts/' + IDENTIFIER + '/resend', 'receipt.resend', UUID(IDENTIFIER)))
        for method, route, resource, target in routes:
            with self.subTest(route=route):
                self.assertEqual(self.api._route(method, PREFIX + route), (resource, target, PREFIX + route))

    def test_unknown_or_wrong_method_routes_are_not_reflected_into_services(self):
        for method, route in [('GET', '/cases/'), ('POST', '/cases'), ('DELETE', '/cases'),
                              ('GET', '/facts/' + IDENTIFIER), ('GET', '/execute'),
                              ('POST', '/cases/' + IDENTIFIER + '/read_raw'),
                              ('GET', '/cases/' + IDENTIFIER + '/close'), ('GET', '/cases//x')]:
            with self.subTest(method=method, route=route):
                with self.assertRaises(ReliabilityError) as caught:
                    self.api._route(method, PREFIX + route)
                self.assertEqual(caught.exception.status, 404)

    def test_known_route_requires_a_strict_uuid_and_canonicalizes_only_its_hex_case(self):
        route = PREFIX + '/cases/' + IDENTIFIER.upper() + '/close'
        self.assertEqual(self.api._route('POST', route)[2], PREFIX + '/cases/' + IDENTIFIER + '/close')
        for value in ('unknown', IDENTIFIER.replace('-', ''), '%30' + IDENTIFIER[1:], '{' + IDENTIFIER + '}'):
            with self.subTest(value=value), self.assertRaises(ReliabilityError) as caught:
                self.api._route('GET', PREFIX + '/cases/' + value)
            self.assertEqual(caught.exception.status, 422)

    def test_query_rejects_ambiguity_unknown_fields_and_unbounded_text(self):
        for resource, query in [('cases', 'page=1&page=2'), ('cases', 'sort=raw_payload'),
                                ('cases', 'gateway_id=%GG'), ('cases', 'gateway_id=%FF'),
                                ('cases', 'page'), ('cases', 'page=1&'), ('cases', 'x=' + 'a' * 8192),
                                ('raw.preview', 'anything=x'), ('case', 'id=' + IDENTIFIER),
                                ('health', 'page=1')]:
            with self.subTest(resource=resource, query=query[:50]), self.assertRaises(ReliabilityError) as caught:
                self.api._query(query, resource)
            self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.api._query('page=2&page_size=10&gateway_id=GW', 'cases'),
                         {'page': '2', 'page_size': '10', 'gateway_id': 'GW'})
        self.assertEqual(self.api._query('', 'raw.download'), {})

    def test_body_rejects_duplicate_keys_nonfinite_invalid_utf8_and_deep_json(self):
        for data in (b'{"reason":"one","reason":"two"}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}',
                     b'[]', b'null', b'\xff', b'{', b' ' * 65537,
                     b'{"x":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'):
            with self.subTest(data=data[:60]):
                with self.assertRaises(ReliabilityError) as caught:
                    self.api._json(data)
                self.assertEqual(caught.exception.status, 422)
        value = {'reason': '核查原因', 'expected_version': '9007199254740993', 'idempotency_key': IDENTIFIER}
        self.assertEqual(self.api._json(json.dumps(value, ensure_ascii=False).encode()), value)


class BoundaryGuardTests(unittest.TestCase):
    def setUp(self):
        api = importlib.import_module('backend.ingest_reliability.api')
        self.assertTrue(hasattr(api, 'IngestReliabilityApi'), 'Incident HTTP adapter is missing')
        class Connection:
            timeout = 17
            def gettimeout(self):
                return self.timeout
            def settimeout(self, value):
                self.timeout = value
        self.handler = SimpleNamespace(headers=Message(), rfile=io.BytesIO(), connection=Connection())
        self.api = api.IngestReliabilityApi(self.handler)

    def headers(self, entries):
        self.handler.headers = Message()
        for key, value in entries:
            self.handler.headers[key] = value

    def test_invalid_framing_and_content_type_are_rejected_before_read(self):
        class ForbiddenRead:
            def read(self, *_args):
                raise AssertionError('Invalid framing reached body reader')
        self.handler.rfile = ForbiddenRead()
        valid_type = [('Content-Type', 'application/json')]
        for entries in [[], [('Content-Length', '-1')], [('Content-Length', '65537')],
                        [('Content-Length', '2'), ('Content-Length', '2')],
                        [('Content-Length', '2'), ('Transfer-Encoding', 'chunked')],
                        [('Content-Length', '1.0')], [('Content-Length', '+2')],
                        [('Content-Length', '2'), ('Content-Type', 'text/plain')]]:
            self.headers(valid_type + entries)
            with self.subTest(entries=entries), self.assertRaises(ReliabilityError) as caught:
                self.api._body()
            self.assertEqual(caught.exception.status, 422)

    def test_body_short_read_and_timeout_are_safe_errors_with_timeout_restored(self):
        self.headers([('Content-Length', '3'), ('Content-Type', 'application/json')])
        self.handler.rfile = io.BytesIO(b'{}')
        with self.assertRaises(ReliabilityError):
            self.api._body()
        self.assertEqual(self.handler.connection.timeout, 17)
        class TimedOut:
            def read(self, *_args):
                raise TimeoutError('private socket diagnostic')
        self.handler.rfile = TimedOut()
        with self.assertRaises(ReliabilityError) as caught:
            self.api._body()
        self.assertEqual(self.handler.connection.timeout, 17)
        self.assertNotIn('private socket diagnostic', str(caught.exception))

    def test_utf8_body_uses_byte_length_and_is_not_whitespace_normalized(self):
        data = json.dumps({'reason': '  核查报文  ', 'expected_version': '1', 'idempotency_key': IDENTIFIER}, ensure_ascii=False).encode()
        self.headers([('Content-Length', str(len(data))), ('Content-Type', 'application/json; charset=utf-8')])
        self.handler.rfile = io.BytesIO(data)
        self.assertEqual(self.api._body()['reason'], '  核查报文  ')
        self.assertEqual(self.handler.connection.timeout, 17)

    def test_origin_without_header_needs_bearer_and_host_does_not_authorize_cookie(self):
        runtime = SimpleNamespace(allowed_origins=('http://localhost',))
        for entries in [[], [('Host', 'localhost'), ('Cookie', 'platform_session=valid')],
                        [('Origin', 'null')], [('Origin', 'http://evil.test'), ('Host', 'localhost')],
                        [('Origin', 'http://localhost/'), ('Authorization', 'Bearer valid')],
                        [('Origin', 'http://localhost'), ('Origin', 'http://localhost')]]:
            self.headers(entries)
            with self.subTest(entries=entries), self.assertRaises(ReliabilityError) as caught:
                self.api._origin(runtime)
            self.assertEqual(caught.exception.status, 403)
        for entries in [[('Authorization', 'Bearer valid')], [('Origin', 'http://localhost')],
                        [('Origin', 'http://localhost:80')]]:
            self.headers(entries)
            self.api._origin(runtime)


class AdapterTests(unittest.TestCase):
    """Fast boundary-only checks; live HTTP tests below own auth/DB proof."""
    def setUp(self):
        from backend.auth import User, AuthError
        from backend.http_server import PlatformRequestHandler
        from backend.ingest_reliability.runtime import ReliabilityRuntime
        module = importlib.import_module('backend.ingest_reliability.api')
        self.assertTrue(hasattr(module.IngestReliabilityApi, 'get'), 'Incident request dispatcher is missing')
        self.user = User(17, 'operator', '现场运维', 3, 'maintenance_engineer',
                         frozenset({'ingest.read', 'ingest.manage', 'ingest.raw.read', 'audit.read'}), 1)
        self.calls = []
        owner = self
        class Auth:
            def require(self, token, permission):
                if owner.user is None or token != 'test-token':
                    raise AuthError('unauthenticated')
                return owner.user
        class Queries:
            def read(self, resource, filters):
                owner.calls.append(('read', resource, filters))
                return {'items': [], 'total': '0'}
        class Operations:
            def execute(self, command):
                owner.calls.append(('execute', command))
                return {'entity': {'id': str(command.target), 'version': '2', 'state': 'closed'}, 'replayed': False}
            def read_raw(self, fact_id, actor, *, preview):
                owner.calls.append(('raw', fact_id, actor, preview))
                return {'fact_id': str(fact_id), 'payload_sha256': hashlib.sha256(b'abc').hexdigest(),
                        'payload_bytes': '3', 'text': 'abc' if preview else None,
                        'raw_bytes': None if preview else b'abc', 'truncated': False}
        class Handler:
            _token = PlatformRequestHandler._token
            _error = staticmethod(PlatformRequestHandler._error)
            command = 'GET'
            close_connection = False
            def send_response(self, status):
                self.status = int(status)
                self.response_count += 1
            def send_header(self, key, value):
                self.response_headers[key] = value
            def end_headers(self):
                pass
        class Connection:
            def gettimeout(self):
                return 8
            def settimeout(self, value):
                pass
        self.handler = Handler()
        self.handler.connection = Connection()
        self.runtime = ReliabilityRuntime('ready', Queries(), Operations(), allowed_origins=('http://localhost',))
        self.handler.server = SimpleNamespace(auth=Auth(), ingest_runtime=self.runtime)
        self.module = module

    def request(self, method, path, body=None, *, headers=(), broken_output=False):
        self.handler.command = method
        self.handler.status = None
        self.handler.response_count = 0
        self.handler.response_headers = {}
        self.handler.headers = Message()
        self.handler.headers['Authorization'] = 'Bearer test-token'
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b''
        if method == 'POST':
            self.handler.headers['Content-Type'] = 'application/json'
            self.handler.headers['Content-Length'] = str(len(data))
        for key, value in headers:
            self.handler.headers[key] = value
        self.handler.rfile = io.BytesIO(data)
        self.handler.wfile = io.BytesIO()
        if broken_output:
            class DisconnectedOutput(io.BytesIO):
                def write(self, _body):
                    raise ConnectionAbortedError('peer aborted response')
            self.handler.wfile = DisconnectedOutput()
        operation = 'get' if method == 'GET' else 'post' if method == 'POST' else 'unsupported'
        handled = getattr(self.module.IngestReliabilityApi(self.handler), operation)(path)
        raw = self.handler.wfile.getvalue()
        return handled, self.handler.status, json.loads(raw) if raw.startswith(b'{') else raw

    def test_outside_prefix_is_not_handled_and_has_no_service_side_effect(self):
        self.assertEqual(self.request('GET', '/api/registry/devices'), (False, None, b''))
        self.assertEqual(self.calls, [])

    def test_known_metadata_route_passes_only_approved_filters_and_typed_target(self):
        self.assertEqual(self.request('GET', PREFIX + '/cases?page=2&gateway_id=GW')[1], 200)
        self.assertEqual(self.calls[-1], ('read', 'cases', {'page': '2', 'gateway_id': 'GW'}))
        self.assertEqual(self.request('GET', PREFIX + '/cases/' + IDENTIFIER)[1], 200)
        self.assertEqual(self.calls[-1], ('read', 'case', {'id': IDENTIFIER}))
        self.assertEqual(self.handler.response_headers.get('Cache-Control'), 'no-store')

    def test_write_constructs_authenticated_actor_and_route_bound_command(self):
        body = {'reason': '核查报文', 'expected_version': '1', 'idempotency_key': str(uuid4())}
        route = PREFIX + '/cases/' + IDENTIFIER + '/close'
        self.assertEqual(self.request('POST', route, body)[1], 200)
        command = self.calls[-1][1]
        self.assertEqual((command.action, command.target, command.actor.user_id, command.actor.domain),
                         ('case.close', UUID(IDENTIFIER), '17', 'platform-local'))
        expected = hashlib.sha256(json.dumps({'action': 'case.close', 'route': route, 'body': body},
                                  sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(command.request_hash, expected)

    def test_permissions_are_checked_before_query_raw_access_or_replay(self):
        self.user = replace(self.user, permissions=frozenset({'ingest.read'}))
        for method, route in [('GET', '/audit'), ('GET', f'/facts/{IDENTIFIER}/raw'),
                               ('POST', f'/cases/{IDENTIFIER}/close')]:
            with self.subTest(route=route):
                self.assertEqual(self.request(method, PREFIX + route, {})[1], 403)
        self.assertEqual(self.calls, [])
        self.user = None
        self.assertEqual(self.request('GET', PREFIX + '/cases')[1], 401)
        self.assertEqual(self.calls, [])

    def test_unavailable_runtime_is_distinct_from_ready_empty_result(self):
        self.handler.server.ingest_runtime = replace(self.runtime, status='unavailable', code='ingest.schema_incomplete')
        result = self.request('GET', PREFIX + '/cases')
        self.assertEqual(result[1], 503)
        self.assertEqual(result[2]['error']['code'], 'ingest.schema_incomplete')
        self.assertNotIn('data', result[2])
        self.assertEqual(self.calls, [])

    def test_authorized_raw_route_returns_download_headers_and_precise_bytes(self):
        handled, status, body = self.request('GET', PREFIX + f'/facts/{IDENTIFIER}/raw')
        self.assertEqual((handled, status, body), (True, 200, b'abc'))
        self.assertEqual(self.calls[-1][0:2], ('raw', UUID(IDENTIFIER)))
        self.assertFalse(self.calls[-1][3])
        self.assertEqual(self.handler.response_headers['Content-Disposition'], f'attachment; filename="ingest-{IDENTIFIER}.bin"')
        self.assertEqual(self.handler.response_headers['X-Content-Type-Options'], 'nosniff')

    def test_unexpected_service_error_has_no_private_diagnostics_or_fake_data(self):
        class BrokenQuery:
            def read(self, *_args):
                raise RuntimeError('password=hidden-test-secret')
        self.handler.server.ingest_runtime = replace(self.runtime, queries=BrokenQuery())
        with self.assertLogs('backend.ingest_reliability.api', level='WARNING') as captured:
            result = self.request('GET', PREFIX + '/health')
        self.assertEqual(result[1], 503)
        self.assertNotIn('data', result[2])
        self.assertNotIn('hidden-test-secret', json.dumps(result) + ''.join(captured.output))

    def test_head_has_no_body_and_no_raw_service_call(self):
        handled, status, body = self.request('HEAD', PREFIX + f'/facts/{IDENTIFIER}/raw')
        self.assertEqual((handled, status, body), (True, 404, b''))
        self.assertEqual(self.calls, [])

    def test_interrupted_download_does_not_attempt_a_second_http_response(self):
        self.request('GET', PREFIX + f'/facts/{IDENTIFIER}/raw', broken_output=True)
        self.assertEqual(self.handler.response_count, 1)
        self.assertTrue(self.handler.close_connection)


class IncidentHTTPTests(IngestDatabaseCase):
    def setUp(self):
        super().setUp()
        from backend.http_server import create_server
        from backend.ingest_reliability.queries import ReliabilityQueries
        runtime_module = importlib.import_module('backend.ingest_reliability.runtime')
        self.runtime = runtime_module.ReliabilityRuntime('ready', ReliabilityQueries(lambda: self.connect('reader')),
                                                        self.operations(), allowed_origins=('http://localhost:8765',))
        self.temp = tempfile.TemporaryDirectory(prefix='ingest-http-')
        self.addCleanup(self.temp.cleanup)
        self.server = create_server('127.0.0.1', 0, Path(self.temp.name) / 'platform.db', ingest_runtime=self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.tokens = {}
        self.fact = self.repository().ingest(replace(observation('HTTP-INCIDENT'), retain=True,
                    received_at=self.scalar('SELECT clock_timestamp()')))
        self.case_id = self.scalar('SELECT id FROM ingest_reliability.case_metadata WHERE fact_id=%s', (self.fact.fact_id,))
        self.close_route = PREFIX + f'/cases/{self.case_id}/close'
        self.close_body = {'reason': '现场核查格式错误', 'expected_version': '1', 'idempotency_key': str(uuid4())}

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def token(self, role='maintenance_engineer'):
        if role not in self.tokens:
            self.tokens[role] = self.server.auth.login(role, PASSWORDS[role]).token
        return self.tokens[role]

    def http(self, method, path, body=None, *, role='maintenance_engineer', headers=None, raw=False):
        request_headers = {'Content-Type': 'application/json'}
        if role is not None:
            request_headers['Authorization'] = 'Bearer ' + self.token(role)
        for key, value in (headers or {}).items():
            if value is None:
                request_headers.pop(key, None)
            else:
                request_headers[key] = value
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=6)
        try:
            conn.request(method, path, body=data, headers=request_headers)
            response = conn.getresponse()
            payload = response.read()
            self.response_headers = dict(response.getheaders())
            return response.status, payload if raw else json.loads(payload)
        finally:
            conn.close()

    def revoke_permission(self, permission, role='maintenance_engineer'):
        with self.server.database.connection() as conn:
            conn.execute('DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code=?) '
                         'AND permission_id=(SELECT id FROM permissions WHERE code=?)', (role, permission))

    def test_revoked_permission_cannot_replay_committed_operation(self):
        first = self.http('POST', self.close_route, self.close_body)
        self.assertEqual(first[0], 200)
        self.assertFalse(first[1]['data']['replayed'])
        replay = self.http('POST', self.close_route, self.close_body)
        self.assertEqual(replay[0], 200)
        self.assertTrue(replay[1]['data']['replayed'])
        self.revoke_permission('ingest.manage')
        self.assertEqual(self.http('POST', self.close_route, self.close_body)[0], 403)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_metadata'), 1)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.operation_receipts', role='operator'), 1)

    def test_unauthenticated_and_revoked_read_never_reach_raw_or_case_operations(self):
        raw_route = PREFIX + f'/facts/{self.fact.fact_id}/raw'
        for method, route, body in [('GET', raw_route, None), ('GET', PREFIX + '/health', None),
                                     ('POST', self.close_route, self.close_body), ('GET', PREFIX + '/unknown', None)]:
            with self.subTest(route=route):
                self.assertEqual(self.http(method, route, body, role=None)[0], 401)
        self.revoke_permission('ingest.read')
        self.assertEqual(self.http('POST', self.close_route, self.close_body)[0], 403)
        self.assertEqual(self.http('GET', raw_route)[0], 403)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_metadata'), 0)

    def test_default_five_role_matrix_enforces_raw_manage_and_audit_independently(self):
        for role in PASSWORDS:
            with self.subTest(role=role):
                self.assertEqual(self.http('GET', PREFIX + '/cases', role=role)[0], 200)
                self.assertEqual(self.http('GET', PREFIX + '/health', role=role)[0], 200)
                self.assertEqual(self.http('GET', PREFIX + f'/facts/{self.fact.fact_id}/preview', role=role)[0],
                                 200 if role in {'administrator', 'algorithm_engineer', 'maintenance_engineer'} else 403)
                self.assertEqual(self.http('GET', PREFIX + '/audit', role=role)[0],
                                 200 if role in {'administrator', 'reviewer'} else 403)
                version = self.scalar('SELECT version FROM ingest_reliability.case_metadata WHERE id=%s', (self.case_id,))
                body = self.close_body | {'idempotency_key': str(uuid4()), 'expected_version': str(version)}
                self.assertEqual(self.http('POST', PREFIX + f'/cases/{self.case_id}/note', body, role=role)[0],
                                 200 if role in {'administrator', 'maintenance_engineer'} else 403)

    def test_case_change_uses_authenticated_actor_and_never_changes_receipt_or_raw_fact(self):
        before = self.scalar('SELECT payload FROM ingest_reliability.receipts WHERE receipt_id=%s', (self.fact.receipt_id,), role='publisher')
        status, data = self.http('POST', self.close_route, self.close_body)
        self.assertEqual((status, data['data']['entity']['state'], data['data']['entity']['version']), (200, 'closed', '2'))
        audit = self.http('GET', PREFIX + '/audit?case_id=' + str(self.case_id), role='administrator')[1]['data']['items'][0]
        self.assertEqual(audit['actor']['username'], 'maintenance_engineer')
        self.assertEqual(audit['actor']['role_code'], 'maintenance_engineer')
        self.assertEqual(audit['actor']['domain'], 'platform-local')
        after = self.scalar('SELECT payload FROM ingest_reliability.receipts WHERE receipt_id=%s', (self.fact.receipt_id,), role='publisher')
        self.assertEqual(before, after)
        self.assertEqual(self.scalar('SELECT status FROM ingest_reliability.facts WHERE fact_id=%s', (self.fact.fact_id,), role='ingestor'), 'quarantined')

    def test_invalid_version_body_and_conflicting_key_return_422_or_409_without_mutation(self):
        for body in (self.close_body | {'actor': {}}, self.close_body | {'expected_version': 1},
                     self.close_body | {'reason': ''}, self.close_body | {'idempotency_key': 'bad'}):
            with self.subTest(body=body):
                self.assertEqual(self.http('POST', self.close_route, body)[0], 422)
        self.assertEqual(self.http('POST', self.close_route, self.close_body | {'expected_version': '2'})[0], 409)
        self.assertEqual(self.http('POST', self.close_route, self.close_body)[0], 200)
        self.assertEqual(self.http('POST', self.close_route, self.close_body | {'reason': '改变请求内容'})[0], 409)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_metadata'), 1)

    def test_cookie_writes_need_an_allowed_origin_and_host_cannot_grant_it(self):
        cookie = 'platform_session=' + self.token()
        base = {'Authorization': None, 'Cookie': cookie}
        for more in ({}, {'Origin': 'null'}, {'Origin': 'http://evil.test', 'Host': 'localhost:8765'},
                     {'Origin': 'http://localhost:8765/'}, {'Origin': 'http://localhost:8765.evil.test'}):
            with self.subTest(more=more):
                self.assertEqual(self.http('POST', self.close_route, self.close_body, headers=base | more)[0], 403)
        self.assertEqual(self.http('POST', self.close_route, self.close_body,
                         headers=base | {'Origin': 'http://localhost:8765'})[0], 200)

    def test_default_port_origins_and_ipv6_use_configured_allowlist_not_host(self):
        from backend.ingest_reliability.config import _origin
        for configured, browser, denied in [('http://localhost:80', 'http://localhost', 'http://localhost:8765'),
                                            ('https://localhost:443', 'https://localhost', 'http://localhost'),
                                            ('https://[0:0:0:0:0:0:0:1]:443', 'https://[::1]', 'https://[::1]:8443')]:
            with self.subTest(configured=configured):
                self.server.ingest_runtime = replace(self.runtime, allowed_origins=(_origin(configured),))
                self.assertEqual(self.http('POST', self.close_route, self.close_body, headers={'Origin': denied, 'Host': browser.split('://')[1]})[0], 403)
                self.assertEqual(self.http('POST', self.close_route, self.close_body, headers={'Origin': browser})[0], 200)

    def test_invalid_query_and_unknown_method_cannot_fall_into_other_api_or_fake_empty(self):
        for suffix in ('?page=1&page=2', '?page_size=101', '?gateway_id=%FF', '?sort=raw_payload', '?page=' + '9' * 10000):
            with self.subTest(suffix=suffix[:40]):
                self.assertEqual(self.http('GET', PREFIX + '/cases' + suffix)[0], 422)
        for method in ('PATCH', 'PUT', 'DELETE', 'OPTIONS'):
            with self.subTest(method=method):
                self.assertEqual(self.http(method, PREFIX + '/cases', {})[0], 404)
        self.assertEqual(self.http('GET', PREFIX + '/execute')[0], 404)
        self.assertEqual(self.http('GET', PREFIX + '/cases/' + str(uuid4()))[0], 404)
        self.assertEqual(self.http('GET', PREFIX + '/cases?gateway_id=OTHER')[1]['data']['total'], '0')
        self.assertEqual(self.http('POST', self.close_route + '?page=1', self.close_body)[0], 422)

    def test_absolute_target_and_fragments_are_rejected_instead_of_becoming_aliases(self):
        for path in ('http://evil.test' + PREFIX + '/cases', PREFIX + '/cases#ignored',
                     PREFIX + '/cases?gateway_id=GW#ignored'):
            with self.subTest(path=path):
                self.assertEqual(self.http('GET', path)[0], 422)

    def test_all_incident_json_responses_are_noncacheable(self):
        for role, route, expected in [('maintenance_engineer', PREFIX + '/cases', 200),
                                     ('production_engineer', PREFIX + f'/facts/{self.fact.fact_id}/preview', 403),
                                     (None, PREFIX + '/cases', 401)]:
            with self.subTest(role=role, route=route):
                self.assertEqual(self.http('GET', route, role=role)[0], expected)
                self.assertEqual(self.response_headers.get('Cache-Control'), 'no-store')
                self.assertEqual(self.response_headers.get('X-Content-Type-Options'), 'nosniff')

    def test_preview_and_download_preserve_binary_and_are_never_inline_html(self):
        payload = b'<script>alert(1)</script>' + b'\xff\x00' * 2400
        fact = self.repository().ingest(replace(observation('BINARY'), payload=payload))
        route = PREFIX + f'/facts/{fact.fact_id}'
        status, data = self.http('GET', route + '/preview')
        self.assertEqual(status, 200)
        self.assertEqual(data['data']['text'], payload[:4096].decode('utf-8', errors='replace'))
        self.assertTrue(data['data']['truncated'])
        self.assertIsNone(data['data']['raw_bytes'])
        status, downloaded = self.http('GET', route + '/raw', raw=True)
        self.assertEqual((status, downloaded), (200, payload))
        self.assertEqual(self.response_headers['Content-Type'], 'application/octet-stream')
        self.assertEqual(self.response_headers['Content-Disposition'], f'attachment; filename="ingest-{fact.fact_id}.bin"')
        self.assertEqual(self.response_headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.response_headers['Cache-Control'], 'no-store')
        self.assertEqual(hashlib.sha256(downloaded).hexdigest(), data['data']['payload_sha256'])
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_metadata WHERE fact_id=%s', (fact.fact_id,)), 2)

    @contextmanager
    def reject_audit(self):
        with self.connect('migrator') as conn:
            conn.execute("CREATE FUNCTION ingest_reliability.http_reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                         "BEGIN RAISE EXCEPTION 'password=private-diagnostic'; END $$")
            conn.execute('CREATE TRIGGER http_reject_audit BEFORE INSERT ON ingest_reliability.audit_events '
                         'FOR EACH ROW EXECUTE FUNCTION ingest_reliability.http_reject_audit()')
        try:
            yield
        finally:
            with self.connect('migrator') as conn:
                conn.execute('DROP TRIGGER http_reject_audit ON ingest_reliability.audit_events')
                conn.execute('DROP FUNCTION ingest_reliability.http_reject_audit()')

    def test_audit_failure_never_returns_raw_or_claims_a_case_change(self):
        with self.reject_audit():
            for route in (PREFIX + f'/facts/{self.fact.fact_id}/preview', PREFIX + f'/facts/{self.fact.fact_id}/raw'):
                status, result = self.http('GET', route)
                self.assertEqual(status, 503)
                self.assertNotIn('data', result)
                self.assertNotIn('private-diagnostic', json.dumps(result))
            self.assertEqual(self.http('POST', self.close_route, self.close_body)[0], 503)
        self.assertEqual(self.scalar('SELECT state FROM ingest_reliability.case_metadata WHERE id=%s', (self.case_id,)), 'open')
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_metadata'), 0)

    def test_database_query_failure_is_safe_503_not_an_empty_result(self):
        from backend.ingest_reliability.queries import ReliabilityQueries
        import psycopg
        def unavailable():
            raise psycopg.OperationalError('password=private-diagnostic')
        self.server.ingest_runtime = replace(self.runtime, queries=ReliabilityQueries(unavailable))
        for resource in ('cases', 'receipts', 'health'):
            status, result = self.http('GET', PREFIX + '/' + resource)
            self.assertEqual(status, 503)
            self.assertNotIn('data', result)
            self.assertNotIn('private-diagnostic', json.dumps(result))

    def test_lost_commit_reply_returns_uncertain_then_original_key_replays_once(self):
        from backend.ingest_reliability.operations import ReliabilityOperations
        import psycopg
        owner = self
        class LostCommitReply:
            def __init__(self):
                self.conn = owner.connect('operator')
            def __enter__(self):
                return self.conn.__enter__()
            def __exit__(self, *args):
                self.conn.__exit__(*args)
                if args[0] is None:
                    raise psycopg.OperationalError('password=lost-reply-private')
        self.server.ingest_runtime = replace(self.runtime, operations=ReliabilityOperations(LostCommitReply))
        status, result = self.http('POST', self.close_route, self.close_body)
        self.assertEqual(status, 503)
        self.assertNotIn('lost-reply-private', json.dumps(result))
        self.server.ingest_runtime = self.runtime
        status, result = self.http('POST', self.close_route, self.close_body)
        self.assertEqual(status, 200)
        self.assertTrue(result['data']['replayed'])
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_metadata'), 1)

    def wire_request(self, method, path, headers, body=b''):
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=6) as conn:
            lines = [f'{method} {path} HTTP/1.0', 'Host: localhost', 'Authorization: Bearer ' + self.token(), *headers, '', '']
            conn.sendall('\r\n'.join(lines).encode('ascii') + body)
            conn.shutdown(socket.SHUT_WR)
            chunks = []
            while chunk := conn.recv(8192):
                chunks.append(chunk)
        response_headers, payload = b''.join(chunks).split(b'\r\n\r\n', 1)
        return int(response_headers.split(b' ', 2)[1]), response_headers, payload

    def test_duplicate_framing_origins_and_partial_bodies_are_rejected(self):
        cases = [(['Content-Length: 2', 'Content-Length: 2'], b'{}', 422),
                 (['Content-Length: -1'], b'', 422), ([], b'', 422),
                 (['Content-Length: 65537'], b'', 422),
                 (['Content-Length: 2', 'Transfer-Encoding: chunked'], b'{}', 422),
                 (['Content-Length: 4'], b'{}', 422),
                 (['Content-Length: 2', 'Origin: http://localhost:8765', 'Origin: http://evil.test'], b'{}', 403)]
        for headers, body, expected in cases:
            with self.subTest(headers=headers):
                status, _, payload = self.wire_request('POST', self.close_route, ['Content-Type: application/json', *headers], body)
                self.assertEqual(status, expected)
                self.assertFalse(json.loads(payload)['ok'])
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_metadata'), 0)

    def test_head_is_authenticated_without_body_or_a_raw_access_audit(self):
        status, headers, body = self.wire_request('HEAD', PREFIX + f'/facts/{self.fact.fact_id}/raw', [])
        self.assertEqual(status, 404)
        self.assertEqual(body, b'')
        self.assertNotIn(b'Access-Control-Allow-Origin', headers)
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.audit_metadata'), 0)

    def test_duplicate_authorization_is_rejected_as_ambiguous(self):
        status, _, body = self.wire_request('GET', PREFIX + '/cases', ['Authorization: Bearer rejected'])
        self.assertEqual(status, 422)
        self.assertFalse(json.loads(body)['ok'])

    def test_receipt_resend_uses_same_immutable_receipt_and_audits_request(self):
        lease = self.outbox().claim()[0]
        self.outbox().finish(lease, success=True)
        detail = self.http('GET', PREFIX + f'/receipts/{self.fact.receipt_id}')[1]['data']['entity']
        body = self.close_body | {'expected_version': detail['version']}
        status, result = self.http('POST', PREFIX + f'/receipts/{self.fact.receipt_id}/resend', body)
        self.assertEqual(status, 200)
        self.assertEqual(result['data']['entity']['state'], 'pending')
        self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.receipt_metadata'), 1)
        self.assertEqual(self.scalar('SELECT action FROM ingest_reliability.audit_metadata'), 'receipt.resend')
