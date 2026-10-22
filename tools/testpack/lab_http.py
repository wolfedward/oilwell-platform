"""HTTP client restricted to one attested lab, with private actor credentials."""
import http.client
import json
import re
from urllib.parse import unquote, urlsplit

from .lab import component_root
from .storage import safe_path, strict_json


HEADERS = {'content-type', 'idempotency-key', 'if-match', 'x-waveform-reason', 'x-content-sha256'}


def validate_request(method, path, headers):
    if method not in ('GET', 'POST', 'HEAD', 'PATCH') or type(path) is not str or len(path) > 8192 or not path.startswith('/api/'):
        raise ValueError('only bounded API requests allowed')
    if method=='PATCH' and not re.fullmatch(r'/api/roles/[1-9][0-9]{0,9}',path):
        raise ValueError('only an owned role permission test may use PATCH')
    decoded = path
    for _ in range(4):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    parts = urlsplit(decoded)
    if (parts.scheme or parts.netloc or parts.fragment or '\\' in decoded or '%' in parts.path
            or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or any(part in ('.', '..') for part in parts.path.split('/'))):
        raise ValueError('unsafe API path')
    if not isinstance(headers, dict) or len(headers) > 5:
        raise ValueError('bounded headers required')
    if any(type(k) is not str or k.lower() not in HEADERS or type(v) is not str
           or len(v) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in v) for k, v in headers.items()):
        raise ValueError('unsafe extra header')
    if len({k.lower() for k in headers}) != len(headers):
        raise ValueError('duplicate header')
    for key, value in headers.items():
        if key.lower() == 'x-content-sha256' and not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError('invalid attachment digest')


class LabHttpError(RuntimeError):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(f'Lab API refused request ({status}, {code})')


class LabHttp:
    def __init__(self, lab, profile):
        self.root = component_root(lab, profile)
        self.port = lab.ports[profile]['http']
        self.profile = profile
        self.sessions = {}

    def _send(self, method, path, body, headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            limit=16 * 1024 * 1024
            declared=response.getheader('Content-Length')
            if declared is not None and (not re.fullmatch(r'0|[1-9][0-9]{0,8}',declared) or int(declared)>limit):
                raise ValueError('API response length outside bound')
            chunks=[];size=0
            while True:
                chunk=response.read(min(65536,limit+1-size))
                if not chunk:break
                chunks.append(chunk);size+=len(chunk)
                if size>limit:raise ValueError('API response exceeds bound')
            content=b''.join(chunks)
            if method!='HEAD' and declared is not None and len(content)!=int(declared):
                raise ValueError('API response length mismatch')
            return response.status, content, response.getheader('Set-Cookie', '')
        finally:
            connection.close()

    def login(self, actor):
        path = safe_path(self.root / 'private/accounts.json')
        if path.stat().st_size > 65536:
            raise ValueError('actor inventory too large')
        accounts = strict_json(path.read_bytes())['accounts']
        account = next((a for a in accounts if a['username'] == actor), None)
        if account is None:
            raise ValueError('actor not in owned inventory')
        headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}'}
        status, raw, cookie = self._send('POST', '/api/auth/login', json.dumps({'username': actor, 'password': account['password']}), headers)
        if status != 200:
            raise LabHttpError(status, 'login_failed')
        cookie = cookie.split(';', 1)[0]
        if not cookie or '=' not in cookie or '\r' in cookie or '\n' in cookie:
            raise ValueError('login cookie unavailable')
        headers['Cookie'] = cookie
        status, raw, _ = self._send('GET', '/api/auth/me', None, headers)
        if status != 200:
            raise LabHttpError(status, 'session_failed')
        me = strict_json(raw)['data']
        self.sessions[actor] = {'Cookie': cookie}
        if self.profile == 'monitoring':
            self.sessions[actor]['X-Monitoring-Session'] = me['session_context']
        return me

    def request(self, method, path, body=None, *, actor, expected=200, raw=False, headers=None):
        extra = headers or {}
        validate_request(method, path, extra)
        if actor is not None and actor not in self.sessions:
            self.login(actor)
        auth_headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}',
                        **(self.sessions[actor] if actor is not None else {}), **extra}
        data = body if isinstance(body, bytes) or body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        status, payload, _ = self._send(method, path, data, auth_headers)
        allowed=expected if type(expected) is tuple else (expected,)
        if status not in allowed:
            try:
                parsed=strict_json(payload)
                code = parsed.get('code',parsed.get('error', {}).get('code', 'unexpected_status'))
                if type(code) is not str or len(code)>128:code='unexpected_status'
            except (ValueError, TypeError):
                code = 'non_json_response'
            raise LabHttpError(status, code)
        return payload if raw else strict_json(payload)
