"""Stored message facts, independently authorized reliability and attribution."""
import psycopg
from backend.registry.queries import _RESOLVED_FROM
from .contracts import window
from .errors import MonitoringError
from .queries import paginated, public, set_total

MESSAGE_FIELDS = 'm.message_id,m.topic,m.gateway_id,m.device_id,m.boot_id,m.sequence_first,m.sequence_last,m.created_at,m.received_at,m.payload_sha256'
SUMMARY = """LEFT JOIN LATERAL (SELECT count(*) AS sample_count,min(s.measured_at) AS measured_from,
 max(s.measured_at) AS measured_to,array_agg(DISTINCT s.source ORDER BY s.source) AS sources
 FROM public.telemetry_samples s WHERE s.message_id=m.message_id) summary ON TRUE"""


def _summaries(conn, cur, rows, cutoff, now, permissions):
    ids = [row['message_id'] for row in rows]
    for row in rows:
        row.update(ingest_status='stored', processing={'status': 'forbidden'}, attribution={'status': 'forbidden'})
    if not ids: return
    if 'ingest.read' in permissions:
        try:
            with conn.transaction():
                # Count public facts before this enrichment; one canonical fact per message
                # avoids fan-out even when legacy history holds additional acceptances.
                cur.execute('SELECT DISTINCT ON (f.accepted_message_id) f.accepted_message_id AS message_id,f.fact_id,'
                    'f.status AS fact_status,f.reason_code,o.first_seen,o.last_seen,o.duplicate_count,o.transport_anomaly_count,'
                    'o.counters_saturated,r.id AS receipt_id,r.status AS receipt_status,r.version,'
                    "CASE WHEN r.id IS NULL THEN 'not_scheduled' WHEN r.requested_generation<=r.sent_generation "
                    "THEN 'broker_confirmed' WHEN r.lease_until>%s THEN 'inflight' ELSE 'pending' END AS delivery_state,"
                    '(SELECT count(*) FROM ingest_reliability.case_metadata c WHERE c.message_id=f.accepted_message_id '
                    'AND c.received_at<=%s) AS case_count '
                    'FROM ingest_reliability.facts f LEFT JOIN ingest_reliability.fact_observations o USING(fact_id) '
                    'LEFT JOIN ingest_reliability.receipt_metadata r USING(fact_id) '
                    "WHERE f.status='stored' AND f.accepted_message_id=ANY(%s) AND f.received_at<=%s "
                    'ORDER BY f.accepted_message_id,f.received_at,f.fact_id', (now, cutoff, ids, cutoff))
                records = {record['message_id']: public(record) for record in cur.fetchall()}
                for row in rows:
                    details = records.get(row['message_id'])
                    row['processing'] = ({'status':'ready', **details, 'status_queried_at':public(now)} if details
                                         else {'status':'not_recorded', 'status_queried_at':public(now)})
        except psycopg.Error:
            for row in rows: row['processing'] = {'status':'unavailable'}
    if 'registry.read' in permissions:
        try:
            with conn.transaction():
                cur.execute(f'SELECT s.message_id,count(DISTINCT w.id) AS well_count,count(*) FILTER (WHERE w.id IS NULL) '
                    f'AS unassigned_count FROM {_RESOLVED_FROM} WHERE s.message_id=ANY(%s) AND s.received_at<=%s '
                    'GROUP BY s.message_id', (ids, cutoff))
                records = {record['message_id']: public(record) for record in cur.fetchall()}
                for row in rows:
                    row['attribution'] = {'status':'ready', 'basis':'sample_measured_at', **records.get(row['message_id'],
                        {'well_count':'0', 'unassigned_count':'0'})}
        except psycopg.Error:
            for row in rows: row['attribution'] = {'status':'unavailable'}


def messages(query, conn, cur, filters, context, cutoff, now, permissions, **_):
    start, end = window(filters, cutoff)
    clauses, params = ['m.received_at>=%s', 'm.received_at<%s', 'm.received_at<=%s'], [start, end, cutoff]
    for key in ('gateway_id','device_id'):
        if key in filters: clauses.append('m.'+key+'=%s'); params.append(filters[key])
    # LATERAL summary returns exactly one row per public message, including zero samples.
    result = paginated(cur, MESSAGE_FIELDS+',summary.*', 'public.ingest_messages m '+SUMMARY,
        ' AND '.join(clauses), params, 'm.received_at DESC,m.message_id DESC', filters, context)
    _summaries(conn, cur, result['items'], cutoff, now, permissions)
    return result | {'scope':'stored_messages', 'from_time':public(start), 'to_time':public(end)}


def message(query, conn, cur, filters, context, cutoff, now, permissions, **_):
    cur.execute(f'SELECT {MESSAGE_FIELDS},summary.* FROM public.ingest_messages m {SUMMARY} '
                'WHERE m.message_id=%s AND m.received_at<=%s', (filters['id'], cutoff))
    row = cur.fetchone()
    if row is None: raise MonitoringError('monitoring.not_found', '已入库报文不存在', 404)
    row = public(row)
    _summaries(conn, cur, [row], cutoff, now, permissions)
    if 'registry.read' in permissions:
        # This explicitly requested detail is a main dependency, not a hidden zero.
        source = f'(SELECT DISTINCT w.id AS id FROM {_RESOLVED_FROM} WHERE s.message_id=%s AND s.received_at<=%s AND w.id IS NOT NULL) wells'
        result = paginated(cur, 'id', source, 'TRUE', [filters['id'], cutoff], 'id', filters, context)
        row |= {'well_ids':[item['id'] for item in result['items']], 'well_count':result['total'],
                'page':result['page'], 'page_size':result['page_size']}
        if row['attribution']['status'] == 'ready': row['unassigned_count'] = row['attribution']['unassigned_count']
    else:
        set_total(context, filters, 1)
    return row
