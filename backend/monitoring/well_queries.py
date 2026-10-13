"""Well metadata and independently authorized optional summaries."""
import psycopg
from .errors import MonitoringError
from .queries import paginated, public
from .exports import make_csv
from backend.registry.queries import _RESOLVED_FROM


def get_well(cur, well_id):
    cur.execute('SELECT * FROM device_registry.wells WHERE id=%s', (well_id,))
    row = cur.fetchone()
    if row is None: raise MonitoringError('monitoring.not_found', '生产井不存在', 404)
    return public(row)


def well(query, conn, cur, filters, context, cutoff, now, permissions, **_):
    record = get_well(cur, filters['well_id'])
    where = 'b.well_id=%s' + (' AND b.valid_to IS NULL' if filters.get('binding_scope', 'current') == 'current' else '')
    bindings = paginated(cur, 'b.*,d.gateway_id,d.device_id AS device_code,d.name,d.status,d.transport,d.usage,d.version',
        'device_registry.well_bindings b JOIN device_registry.devices d ON d.id=b.device_id', where,
        [filters['well_id']], 'b.valid_from DESC,b.id DESC', filters, context)
    availability = {}
    for section, permission in (('telemetry', 'telemetry.read'), ('link', 'acquisition.read'), ('cases', 'ingest.read')):
        if permission not in permissions:
            availability[section] = {'status': 'forbidden'}
            continue
        try:
            with conn.transaction():  # Optional failure rolls back only to this savepoint.
                if section == 'telemetry':
                    cur.execute(f'SELECT count(*) AS sample_count,max(s.measured_at) AS last_measured_at, '
                        f'max(s.received_at) AS last_received_at FROM {_RESOLVED_FROM} WHERE w.id=%s AND s.received_at<=%s',
                        (filters['well_id'], cutoff))
                    data = cur.fetchone() | {'scope': 'historically_attributed_samples'}
                elif section == 'link':
                    cur.execute('SELECT max(m.received_at) AS last_received_at FROM public.ingest_messages m '
                        'JOIN device_registry.devices d ON d.gateway_id=m.gateway_id AND d.device_id=m.device_id '
                        'JOIN device_registry.well_bindings b ON b.device_id=d.id '
                        'WHERE b.well_id=%s AND b.valid_to IS NULL AND m.received_at<=%s', (filters['well_id'], cutoff))
                    data = cur.fetchone()
                    age = (now-data['last_received_at']).total_seconds() if data['last_received_at'] else None
                    data |= {'scope': 'currently_bound_devices', 'age_seconds': age,
                        'link_status': 'never_reported' if age is None else 'clock_anomaly' if age<0 else
                            'online' if age<=query.online_seconds else 'delayed' if age<=query.delayed_seconds else 'offline',
                        'thresholds': {'online_seconds': query.online_seconds, 'delayed_seconds': query.delayed_seconds}}
                else:
                    cur.execute('SELECT count(*) AS total FROM ingest_reliability.case_metadata c WHERE c.received_at<=%s '
                        "AND c.state<>'closed' AND EXISTS (SELECT 1 FROM device_registry.devices d "
                        'JOIN device_registry.well_bindings b ON b.device_id=d.id WHERE b.well_id=%s AND b.valid_to IS NULL '
                        'AND d.gateway_id=c.gateway_id AND d.device_id=c.device_id)', (cutoff, filters['well_id']))
                    data = cur.fetchone() | {'scope': 'currently_bound_devices_not_historical_well_attribution',
                        'status_queried_at': public(now), 'unassigned_cases_included': False}
                availability[section] = {'status': 'ready', **public(data)}
        except psycopg.Error:
            availability[section] = {'status': 'unavailable'}
    return {'well': record, 'bindings': bindings, 'availability': availability}


def wells_csv(query, conn, cur, filters, context, cutoff, now, permissions, **_):
    fields = [key for key in ('area_code', 'well_code', 'status') if key in filters]
    result = paginated(cur, '*', 'device_registry.wells', ' AND '.join(key+'=%s' for key in fields) or 'TRUE',
        [filters[key] for key in fields], 'created_at DESC,id DESC', filters, context, limit=10001)
    return make_csv('wells.csv', ['id', 'area_code', 'well_code', 'name', 'status', 'version', 'created_at', 'updated_at'], result['items'])
