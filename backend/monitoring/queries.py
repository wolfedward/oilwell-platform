"""One bounded, read-only snapshot per monitoring request; no alternate source."""
from datetime import datetime
from uuid import UUID
import psycopg
from psycopg.rows import dict_row
from .contracts import validate_filters, require_permissions
from .errors import MonitoringError, invalid


def public(value, key=None):
    if isinstance(value, dict): return {k: public(v, k) for k,v in value.items()}
    if isinstance(value, (list, tuple)): return [public(v) for v in value]
    if isinstance(value, UUID): return str(value)
    if isinstance(value, datetime): return value.isoformat(timespec='microseconds')
    if value is not None and key in {'version', 'map_version', 'expected_map_version', 'parent_version', 'sequence',
        'sequence_first', 'sequence_last', 'duplicate_count', 'transport_anomaly_count', 'requested_generation',
        'sent_generation', 'attempts', 'total', 'sample_count', 'unassigned_count', 'well_count', 'case_count'}: return str(value)
    return value


def changed():
    return MonitoringError('monitoring.query_changed', '台账或查询结果已变化，请刷新后重新查询', 409)


def set_total(context, filters, total):
    total = str(total)
    if 'query_total' in filters and total != filters['query_total']: raise changed()
    context['query_total'] = total
    return total


def paginated(cur, select, source, where, params, order, filters, context, *, limit=None):
    cur.execute(f'SELECT count(*) AS total FROM {source} WHERE {where}', params)
    total = set_total(context, filters, cur.fetchone()['total'])
    cur.execute(f'SELECT {select} FROM {source} WHERE {where} ORDER BY {order} LIMIT %s OFFSET %s',
                (*params, filters['page_size'] if limit is None else limit, filters.get('offset', 0)))
    return {'items': public(cur.fetchall()), 'total': total, 'page': filters.get('page', 1),
            'page_size': filters.get('page_size', limit)}


class MonitoringQueries:
    def __init__(self, connect, *, environment, source_ref, online_seconds, delayed_seconds, clock_tolerance_seconds):
        self.connect, self.environment, self.source_ref = connect, environment, source_ref
        self.online_seconds, self.delayed_seconds = online_seconds, delayed_seconds
        self.clock_tolerance_seconds = clock_tolerance_seconds

    def read(self, resource, filters, permissions):
        return self._request(resource, filters, frozenset(permissions))

    def export(self, resource, filters, permissions):
        if resource not in {'wells_csv', 'history_csv'}: raise invalid('resource')
        return self._request(resource, filters, frozenset(permissions))

    def _request(self, resource, supplied, permissions):
        selected = validate_filters(resource, supplied)
        require_permissions(resource, selected, permissions)
        try:
            with self.connect() as conn:
                with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                    cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                    cur.execute("SET LOCAL statement_timeout='3s'")
                    cur.execute("SET LOCAL lock_timeout='1s'")
                    cur.execute("SET LOCAL idle_in_transaction_session_timeout='10s'")
                    cur.execute("SET LOCAL TIME ZONE 'UTC'")
                    cur.execute('SELECT revision,clock_timestamp() AS instant FROM device_registry.registry_head WHERE singleton=1')
                    head = cur.fetchone()
                    if head is None: raise MonitoringError('monitoring.unavailable', '台账修订信息不可用', 503)
                    if 'revision' in selected and selected['revision'] != str(head['revision']): raise changed()
                    now = head['instant']
                    cutoff = selected.get('received_before', now)
                    if cutoff > now: raise invalid('received_before')
                    context = {'registry_revision': str(head['revision']), 'received_before': public(cutoff),
                               'queried_at': public(now), 'source_ref': self.source_ref, 'environment': self.environment}
                    from .well_queries import well, wells_csv
                    from .telemetry_queries import latest, history
                    from .acquisition_queries import messages, message
                    from .quality_queries import quality
                    readers = {'well': well, 'wells_csv': wells_csv, 'latest': latest, 'history': history, 'history_csv': history,
                               'messages': messages, 'message': message, 'quality': quality}
                    if resource not in readers: raise invalid('resource')
                    result = readers[resource](self, conn, cur, selected, context, cutoff, now, permissions,
                                                exporting=resource.endswith('_csv'))
                    if isinstance(result, dict): return result | {'context': context}
                    return result
        except psycopg.Error:
            raise MonitoringError('monitoring.unavailable', '监控查询暂不可用，请保留条件后重试', 503) from None
