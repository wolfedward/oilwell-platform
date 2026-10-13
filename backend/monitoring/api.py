"""Authenticated read/export routing; raw payloads and writes are never provided."""
from urllib.parse import urlsplit
from .contracts import parse_filters, validate_filters, require_permissions
from .errors import MonitoringError
from .runtime import MonitoringRuntime

PREFIX = '/api/production-monitoring'


def route(path):
    parts = path[len(PREFIX):].strip('/').split('/')
    if parts == ['wells.csv']: return 'wells_csv', {}
    if len(parts) in (2, 3) and parts[0] == 'wells':
        resource = 'well' if len(parts) == 2 else {'latest': 'latest', 'history': 'history', 'history.csv': 'history_csv'}.get(parts[2])
        if resource: return resource, {'well_id': parts[1]}
    raise MonitoringError('monitoring.not_found', '此监控查询接口不存在', 404)


class MonitoringApi:
    def __init__(self, handler): self.handler = handler

    def get(self, path):
        parsed = urlsplit(path)
        if parsed.path != PREFIX and not parsed.path.startswith(PREFIX+'/'): return False
        h = self.handler
        user = h._require('registry.read')
        if user is None: return True
        try:
            resource, identity = route(parsed.path)
            filters = parse_filters(resource, parsed.query) | identity
            require_permissions(resource, validate_filters(resource, filters), frozenset(user.permissions))
            runtime = getattr(h.server, 'monitoring_runtime', None) or MonitoringRuntime('disabled', code='monitoring.disabled')
            if runtime.status != 'ready' or runtime.query is None:
                raise MonitoringError(runtime.code or 'monitoring.unavailable', '监控查询未配置或暂不可用', 503)
            if resource.endswith('_csv'):
                result = runtime.query.export(resource, filters, frozenset(user.permissions))
                h.send_response(200)
                h.send_header('Content-Type', 'text/csv; charset=utf-8')
                h.send_header('Content-Length', str(len(result.body)))
                h.send_header('Content-Disposition', f'attachment; filename="{result.filename}"')
                h.send_header('Cache-Control', 'no-store')
                h.send_header('X-Content-Type-Options', 'nosniff')
                h.end_headers()
                h.wfile.write(result.body)
            else:
                result = runtime.query.read(resource, filters, frozenset(user.permissions))
                h._send_json(200, {'ok': True, 'data': result})
        except MonitoringError as error:
            h._send_json(error.status, h._error(error.code, error.message))
        return True
