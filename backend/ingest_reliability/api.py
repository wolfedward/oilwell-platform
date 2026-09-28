"""Explicit authenticated boundary for incident metadata and audited actions."""
import json
import logging
import math
import re
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from backend.auth import AuthError, PermissionDenied, SessionExpired
from .config import _origin as normalize_origin
from .errors import ReliabilityError
from .management_contracts import Actor, _UUID, validate_management
from .permissions import required_permissions
from .runtime import ReliabilityRuntime


PREFIX = '/api/ingest-reliability'
MAX_JSON_BYTES = 65536
_PAGE = {'page', 'page_size', 'from_time', 'to_time'}
_IDENTITY = {'gateway_id', 'device_id', 'message_id', 'reason_code'}
_FILTERS = {'cases': _PAGE | _IDENTITY | {'state'},
            'receipts': _PAGE | _IDENTITY | {'status', 'delivery_state'},
            'audit': _PAGE | {'fact_id', 'case_id', 'receipt_id', 'actor_id', 'action'},
            'case': set(), 'receipt': set(), 'health': set(), 'raw.preview': set(), 'raw.download': set()}


def _invalid(code='ingest.request_invalid', message='请求格式无效'):
    return ReliabilityError(code, message, 422)


def _not_found():
    return ReliabilityError('ingest.not_found', '此接收可靠性接口不存在或不支持该方法', 404)


def _route(method, path):
    parts = path[len(PREFIX):].split('/')[1:]
    if not parts or any(not part for part in parts):
        raise _not_found()
    target = None
    if method == 'GET':
        if len(parts) == 1 and parts[0] in ('cases', 'receipts', 'health', 'audit'):
            return parts[0], None, path
        if len(parts) == 2 and parts[0] in ('cases', 'receipts'):
            action = 'case' if parts[0] == 'cases' else 'receipt'
        elif len(parts) == 3 and parts[0] == 'facts' and parts[2] in ('preview', 'raw'):
            action = 'raw.preview' if parts[2] == 'preview' else 'raw.download'
        else:
            raise _not_found()
    elif method == 'POST' and len(parts) == 3:
        if parts[0] == 'cases' and parts[2] in ('ack', 'close', 'reopen', 'note'):
            action = 'case.' + parts[2]
        elif parts[0] == 'receipts' and parts[2] == 'resend':
            action = 'receipt.resend'
        else:
            raise _not_found()
    else:
        raise _not_found()
    if not _UUID.fullmatch(parts[1]):
        raise _invalid('ingest.identifier_invalid', '记录编号必须为 UUID')
    target = UUID(parts[1])
    parts[1] = str(target)
    return action, target, PREFIX + '/' + '/'.join(parts)


def _query(source, resource):
    try:
        if len(source) > 8192 or re.search(r'%(?![0-9a-fA-F]{2})', source):
            raise ValueError()
        pairs = parse_qsl(source, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=20)
        if len({key for key, _ in pairs}) != len(pairs) or any(key not in _FILTERS[resource] for key, _ in pairs):
            raise ValueError()
        return dict(pairs)
    except (ValueError, UnicodeError):
        raise _invalid('ingest.query_invalid', '查询参数重复、未知、过长或编码无效') from None


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    def reject(_value):
        raise ValueError('nonfinite number')

    def number(token):
        result = float(token)
        if not math.isfinite(result):
            raise ValueError('nonfinite number')
        return result

    try:
        if not isinstance(data, bytes) or len(data) > MAX_JSON_BYTES:
            raise ValueError('body length')
        source = data.decode('utf-8')
        depth, quoted, escaped = 0, False, False
        for char in source:
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in '[{':
                depth += 1
                if depth > 64:
                    raise ValueError('nesting limit')
            elif char in ']}':
                depth -= 1
        result = json.loads(source, object_pairs_hook=unique, parse_constant=reject, parse_float=number)
        if not isinstance(result, dict):
            raise ValueError('object required')
        return result
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise _invalid('ingest.json_invalid', '请求必须为不超过 64 KiB 的无重复字段 UTF-8 JSON 对象') from None


class IngestReliabilityApi:
    def __init__(self, handler):
        self.handler = handler

    def get(self, path):
        return self._dispatch('GET', path)

    def post(self, path):
        return self._dispatch('POST', path)

    def unsupported(self, path):
        return self._dispatch('unsupported', path)

    def _send(self, status, body, *, filename=None):
        h = self.handler
        if filename is None:
            content = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
        else:
            if not isinstance(body, bytes):
                raise TypeError('Raw response must be bytes')
            content, content_type = body, 'application/octet-stream'
        h.send_response(status)
        h.send_header('Content-Type', content_type)
        h.send_header('Content-Length', str(len(content)))
        h.send_header('Cache-Control', 'no-store')
        h.send_header('X-Content-Type-Options', 'nosniff')
        if filename is not None:
            h.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        try:
            h.end_headers()
            if h.command != 'HEAD':
                h.wfile.write(content)
        except OSError:
            # A peer disappearing after headers must not trigger a second HTTP
            # response. Any committed action remains retriable by its own key.
            h.close_connection = True

    def _dispatch(self, method, path):
        h = self.handler
        try:
            parsed = urlsplit(path)
        except ValueError:
            if not path.startswith(PREFIX):
                return False
            self._send(422, h._error('ingest.request_invalid', '请求路径格式无效'))
            h.close_connection = True
            return True
        if parsed.path != PREFIX and not parsed.path.startswith(PREFIX + '/'):
            return False
        try:
            if any(len(h.headers.get_all(name, [])) > 1 for name in ('Authorization', 'Cookie', 'Host')):
                raise _invalid('ingest.headers_ambiguous', '身份或主机请求头不能重复')
            # Every request, including a retry, resolves current authorization
            # before any operation receipt lookup or database content access.
            user = h.server.auth.require(h._token(), 'ingest.read')
            if parsed.scheme or parsed.netloc or parsed.fragment or '#' in path:
                raise _invalid('ingest.request_invalid', '接口不接受绝对地址或片段')
            action, target, canonical = _route(method, parsed.path)
            if not required_permissions(action).issubset(user.permissions):
                raise ReliabilityError('ingest.forbidden', '当前账号没有此项接收异常操作权限', 403)
            runtime = getattr(h.server, 'ingest_runtime', None) or ReliabilityRuntime('disabled', code='ingest.disabled')
            if method == 'POST':
                if parsed.query or '?' in path:
                    raise _invalid('ingest.query_invalid', '写入接口不接受查询参数')
                self._origin(runtime)
                body = self._body()
                actor = Actor('platform-local', str(user.id), user.username, user.display_name, user.role_code)
                command = validate_management(action, target, actor, body, route=canonical)
            else:
                filters = _query(parsed.query, action)
                if target is not None:
                    filters['id'] = str(target)
            if runtime.status != 'ready' or runtime.queries is None or runtime.operations is None:
                raise ReliabilityError(runtime.code or 'ingest.unavailable', '接收异常管理未配置或暂不可用，请联系管理员检查')
            if method == 'POST':
                result = runtime.operations.execute(command)
            elif action in ('raw.preview', 'raw.download'):
                actor = Actor('platform-local', str(user.id), user.username, user.display_name, user.role_code)
                result = runtime.operations.read_raw(target, actor, preview=action == 'raw.preview')
                if action == 'raw.download':
                    self._send(200, result['raw_bytes'], filename=f'ingest-{target}.bin')
                    return True
            else:
                result = runtime.queries.read(action, filters)
            self._send(200, {'ok': True, 'data': result})
        except PermissionDenied:
            h.close_connection = True
            self._send(403, h._error('forbidden', 'Permission denied'))
        except (AuthError, SessionExpired):
            h.close_connection = True
            self._send(401, h._error('unauthenticated', 'Authentication required'))
        except ReliabilityError as error:
            if method != 'GET':
                h.close_connection = True
            self._send(error.status, h._error(error.code, error.message))
        except (BrokenPipeError, ConnectionResetError):
            # The committed operation remains retryable with its original key.
            h.close_connection = True
        except Exception:
            h.close_connection = True
            logging.getLogger(__name__).warning('Incident request failed: ingest.unavailable')
            self._send(503, h._error('ingest.unavailable', '接收异常服务暂不可用；处置请求请保留原标识和内容重试'))
        return True

    def _body(self):
        h = self.handler
        lengths = h.headers.get_all('Content-Length', [])
        types = h.headers.get_all('Content-Type', [])
        if h.headers.get_all('Transfer-Encoding') or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]):
            raise _invalid('ingest.framing_invalid', '请求须使用单一有效 Content-Length')
        length = int(lengths[0])
        if length > MAX_JSON_BYTES:
            raise _invalid('ingest.body_too_large', '请求不能超过 64 KiB')
        if len(types) != 1 or types[0].split(';', 1)[0].strip().lower() != 'application/json':
            raise _invalid('ingest.content_type_invalid', '请求必须使用 application/json')
        previous = h.connection.gettimeout()
        try:
            h.connection.settimeout(3)
            data = h.rfile.read(length)
            if len(data) != length:
                raise _invalid('ingest.body_incomplete', '请求正文未完整接收')
            return _json(data)
        except (OSError, TimeoutError):
            raise _invalid('ingest.body_timeout', '请求正文读取超时或连接中断') from None
        finally:
            h.connection.settimeout(previous)

    def _origin(self, runtime):
        origins = self.handler.headers.get_all('Origin', [])
        if origins:
            try:
                if len(origins) != 1 or normalize_origin(origins[0]) not in runtime.allowed_origins:
                    raise ValueError()
            except (ReliabilityError, ValueError):
                raise ReliabilityError('ingest.origin_denied', '不允许此来源执行接收异常处置', 403) from None
        else:
            authorization = self.handler.headers.get('Authorization', '')
            if not authorization.startswith('Bearer ') or not authorization[7:].strip():
                raise ReliabilityError('ingest.origin_required', '无来源头的客户端须使用 Bearer 认证', 403)
