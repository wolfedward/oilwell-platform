"""Read-only incident metadata queries, separate from authorized raw access."""
from datetime import datetime, timedelta, timezone
import re
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from .contracts import REJECTION_REASONS, valid_name
from .errors import ReliabilityError
from .management_contracts import ACTIONS, _UUID, _text


_PAGE = {'page', 'page_size', 'from_time', 'to_time'}
_IDENTITY = {'gateway_id', 'device_id', 'message_id', 'reason_code'}
_ALLOWED = {'cases': _PAGE | _IDENTITY | {'state'}, 'receipts': _PAGE | _IDENTITY | {'status', 'delivery_state'},
            'case': {'id'}, 'receipt': {'id'}, 'health': set(),
            'audit': _PAGE | {'fact_id', 'case_id', 'receipt_id', 'actor_id', 'action'}}

# Only these fixed metadata projections may reach the Web boundary. Never
# SELECT * from views: future additions must not silently expose new fields.
_CASE = '''SELECT c.id,c.fact_id,c.state,c.version,c.updated_at,c.topic,
    c.payload_sha256,c.payload_bytes,c.gateway_id,c.device_id,c.message_id,
    c.reason_code,c.validator_version,c.received_at,c.qos,c.retain,c.dup,
    c.first_seen,c.last_seen,c.duplicate_count,c.transport_anomaly_count,
    c.counters_saturated,r.id AS receipt_id
    FROM ingest_reliability.case_metadata c
    LEFT JOIN ingest_reliability.receipt_metadata r ON r.fact_id=c.fact_id'''
_RECEIPT = '''SELECT r.id,r.fact_id,r.topic,r.gateway_id,r.device_id,r.message_id,
    r.payload_sha256,r.status,r.reason_code,r.recorded_at,r.requested_generation,
    r.sent_generation,r.lease_until,r.next_attempt_at,r.requested_at,r.attempts,
    r.last_error_code,r.broker_acked_at,r.version,
    CASE WHEN r.requested_generation<=r.sent_generation THEN 'broker_confirmed'
         WHEN r.lease_until>%s THEN 'inflight' ELSE 'pending' END AS delivery_state
    FROM ingest_reliability.receipt_metadata r'''
_AUDIT = '''SELECT id,fact_id,case_id,receipt_id,actor,action,reason,
    before_data,after_data,recorded_at FROM ingest_reliability.audit_metadata'''
_SOURCES = {'cases': (_CASE, 'received_at'), 'case': (_CASE, 'received_at'),
            'receipts': (_RECEIPT, 'recorded_at'), 'receipt': (_RECEIPT, 'recorded_at'),
            'audit': (_AUDIT, 'recorded_at')}
_EQUALITIES = {name: 'm.' + name + '=%s' for name in
               ('id', 'fact_id', 'case_id', 'receipt_id', 'gateway_id', 'device_id',
                'message_id', 'reason_code', 'state', 'status', 'delivery_state', 'action')}
_EQUALITIES['actor_id'] = "m.actor->>'user_id'=%s"


def _wire(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec='microseconds')
    if isinstance(value, UUID) or type(value) is int:
        return str(value)
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_wire(item) for item in value]
    return value


def _invalid():
    return ReliabilityError('ingest.query_invalid', '查询条件、分页或时间范围无效', 422)


def _integer(value, maximum):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,5}', value):
        result = int(value)
    else:
        raise _invalid()
    if not 1 <= result <= maximum:
        raise _invalid()
    return result


def _time(value):
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError('timestamp type')
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError('missing timezone')
        return instant.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise _invalid() from None


def _window(start, end):
    try:
        start = end - timedelta(hours=24) if start is None else start
    except OverflowError:
        raise _invalid() from None
    if start > end or end - start > timedelta(days=31):
        raise _invalid()
    return start, end


def _filters(resource, filters):
    if (not isinstance(resource, str) or resource not in _ALLOWED or not isinstance(filters, dict)
            or set(filters) - _ALLOWED[resource] or (resource in ('case', 'receipt') and 'id' not in filters)):
        raise _invalid()
    selected = dict(filters)
    for key in ('id', 'fact_id', 'case_id', 'receipt_id'):
        if key in selected:
            if not isinstance(selected[key], str) or not _UUID.fullmatch(selected[key]):
                raise _invalid()
            selected[key] = UUID(selected[key])
    for key in ('gateway_id', 'device_id', 'message_id'):
        if key in selected and not valid_name(selected[key]):
            raise _invalid()
    choices = {'state': {'open', 'acknowledged', 'closed'}, 'status': {'stored', 'quarantined'},
               'delivery_state': {'pending', 'inflight', 'broker_confirmed'},
               'reason_code': REJECTION_REASONS | ({'accepted'} if resource == 'receipts' else set()),
               'action': ACTIONS | {'case.repeat_reopen', 'raw.preview', 'raw.download'}}
    for key, values in choices.items():
        if key in selected and (not isinstance(selected[key], str) or selected[key] not in values):
            raise _invalid()
    if 'actor_id' in selected:
        _text(selected['actor_id'])
    for key in ('from_time', 'to_time'):
        if key in selected:
            selected[key] = _time(selected[key])
    if 'to_time' in selected:
        _window(selected.get('from_time'), selected['to_time'])
    if 'page' in _ALLOWED[resource]:
        selected['page'] = _integer(selected.get('page', 1), 100001)
        selected['page_size'] = _integer(selected.get('page_size', 20), 100)
        selected['offset'] = (selected['page'] - 1) * selected['page_size']
        if selected['offset'] > 100000:
            raise _invalid()
    return selected


class ReliabilityQueries:
    def __init__(self, connect, *, health_stale_seconds=120):
        if not callable(connect) or type(health_stale_seconds) is not int or not 1 <= health_stale_seconds <= 86400:
            raise _invalid()
        self.connect = connect
        self.health_stale_seconds = health_stale_seconds

    def read(self, resource, filters):
        selected = _filters(resource, filters)
        try:
            with self.connect() as connection:
                return self._read(connection, resource, selected)
        except psycopg.Error:
            raise ReliabilityError('ingest.query_unavailable', '接收异常查询暂时不可用，请稍后重试') from None

    def _read(self, connection, resource, selected):
        with connection.cursor(row_factory=dict_row) as cur:
            # A list and its count (or the health components) share one snapshot.
            cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            cur.execute("SET LOCAL statement_timeout='3s'")
            cur.execute("SET LOCAL lock_timeout='1s'")
            cur.execute("SET LOCAL idle_in_transaction_session_timeout='10s'")
            cur.execute('SET LOCAL search_path=pg_catalog,pg_temp')
            cur.execute("SET LOCAL TIME ZONE 'UTC'")
            cur.execute('SELECT clock_timestamp() AS instant')
            instant = cur.fetchone()['instant']
            if resource == 'health':
                return self._health(cur, instant)
            source, time_column = _SOURCES[resource]
            params = [instant] if resource in ('receipt', 'receipts') else []
            predicates = []
            for key, clause in _EQUALITIES.items():
                if key in selected:
                    predicates.append(clause)
                    params.append(selected[key])
            detail = resource in ('case', 'receipt')
            if not detail:
                start, end = _window(selected.get('from_time'), selected.get('to_time', instant))
                predicates.extend((f'm.{time_column}>=%s', f'm.{time_column}<=%s'))
                params.extend((start, end))
            where = ' AND '.join(predicates)
            query = f'FROM ({source}) m WHERE {where}'
            if detail:
                cur.execute('SELECT m.* ' + query, params)
                row = cur.fetchone()
                if row is None:
                    raise ReliabilityError('ingest.not_found', '未找到指定的接收异常或回执', 404)
                return {'entity': _wire(row)}
            cur.execute('SELECT count(*) AS total ' + query, params)
            total = cur.fetchone()['total']
            cur.execute('SELECT m.* ' + query + f' ORDER BY m.{time_column} DESC,m.id DESC LIMIT %s OFFSET %s',
                        params + [selected['page_size'], selected['offset']])
            return {'items': _wire(cur.fetchall()), 'total': str(total),
                    'page': selected['page'], 'page_size': selected['page_size'],
                    'from_time': _wire(start), 'to_time': _wire(end), 'queried_at': _wire(instant)}

    def _health(self, cur, instant):
        cur.execute('''SELECT total_bytes,quarantine_bytes,used_total_bytes,used_quarantine_bytes,
            last_clock_seen,open_cases,oldest_open_at,pending_receipts,oldest_pending_at,
            publisher_last_seen FROM ingest_reliability.health_metadata''')
        row = cur.fetchone()
        if row is None:
            raise ReliabilityError('ingest.query_unavailable', '接收容量记录缺失，请检查配置')
        cutoff = instant - timedelta(seconds=self.health_stale_seconds)
        cur.execute('''WITH clock AS (SELECT %s::timestamptz AS instant,%s::timestamptz AS cutoff)
            SELECT count(*) AS workers,
                count(*) FILTER (WHERE last_seen BETWEEN cutoff AND instant) AS active,
                count(*) FILTER (WHERE last_seen>instant OR last_success>instant OR last_failure>instant) AS future,
                count(*) FILTER (WHERE last_seen BETWEEN cutoff AND instant AND error_code IS NOT NULL) AS failed,
                count(*) FILTER (WHERE last_seen BETWEEN cutoff AND instant AND error_code IS NULL
                    AND (last_success IS NULL OR last_success<cutoff)) AS unverified
            FROM ingest_reliability.worker_health_metadata CROSS JOIN clock''', (instant, cutoff))
        workers = cur.fetchone()
        reasons = []
        if not workers['workers']:
            reasons.append('publisher.missing')
        elif not workers['active']:
            reasons.append('publisher.stale')
        if workers['future']:
            reasons.append('publisher.clock_invalid')
        if workers['failed']:
            reasons.append('publisher.failed')
        if workers['unverified']:
            reasons.append('publisher.unverified')
        if row['used_total_bytes'] >= row['total_bytes'] or row['used_quarantine_bytes'] >= row['quarantine_bytes']:
            reasons.append('capacity.exhausted')
        if row['last_clock_seen'] is None:
            reasons.append('capacity.clock_missing')
        elif row['last_clock_seen'] > instant:
            reasons.append('capacity.clock_invalid')
        ages = {}
        for kind in ('open', 'pending'):
            oldest = row['oldest_' + kind + '_at']
            ages['oldest_' + kind + '_age_seconds'] = None if oldest is None else max(0.0, (instant - oldest).total_seconds())
            if oldest is not None and oldest > instant and 'metadata.clock_invalid' not in reasons:
                reasons.append('metadata.clock_invalid')
        cur.execute('''SELECT count(*) AS quarantined_cases,
            count(*) FILTER (WHERE received_at BETWEEN %s AND %s) AS new_cases_24h
            FROM ingest_reliability.case_metadata''', (instant - timedelta(hours=24), instant))
        growth = cur.fetchone()
        cur.execute('''SELECT error_code FROM ingest_reliability.worker_health_metadata
            WHERE last_seen BETWEEN %s AND %s AND error_code IS NOT NULL
            ORDER BY last_failure DESC NULLS LAST,last_seen DESC,worker_id DESC LIMIT 1''', (cutoff, instant))
        failure = cur.fetchone()
        return _wire(row | growth) | ages | {'healthy': not reasons, 'reasons': reasons,
                                   'publisher_error_code': failure['error_code'] if failure else None,
                                   'publisher_workers': _wire(workers), 'queried_at': _wire(instant)}
