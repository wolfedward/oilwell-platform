"""Historical attribution and latest expected slots, never synthetic observations."""
from datetime import datetime
from statistics import median
from backend.registry.queries import _RESOLVED_FROM
from backend.registry.resolution import assess
from .queries import paginated, public, set_total
from .exports import make_csv
from .well_queries import get_well

SELECT = ('s.*,d.id AS registry_device_id,current_d.status AS current_state,st.status AS sample_state,'
          'b.id AS binding_id,w.id AS well_id,w.well_code,a.map_version AS expected_map_version,m.content')
ORDER = 's.measured_at DESC,s.message_id DESC,s.sample_index DESC'
CSV_FIELDS = ['message_id', 'sample_index', 'registry_device_id', 'well_id', 'gateway_id', 'device_id', 'point_code',
              'measured_at', 'received_at', 'sequence', 'value', 'quality', 'unit', 'source', 'expected_map_version',
              'application_status', 'eligible_for_automation', 'reasons']


def resolve(query, row):
    sample = public(dict(row))
    content = sample.pop('content', None)
    point = next((p for p in content['points'] if p['point_code'] == sample['point_code']), None) if content else None
    measured, received = (datetime.fromisoformat(sample[key]) for key in ('measured_at', 'received_at'))
    registered = sample['registry_device_id'] is not None
    result = assess(sample, {'registered': registered, 'bound': sample['binding_id'] is not None,
        'state': sample['sample_state'], 'clock_valid': (measured-received).total_seconds() <= query.clock_tolerance_seconds
            and (not registered or sample['sample_state'] is not None), 'map_version': sample['expected_map_version'], 'point': point})
    return result | {'expected_unit': point['unit'] if point else None}


SLOTS = """device_registry.well_bindings b JOIN device_registry.devices d ON d.id=b.device_id
 JOIN device_registry.point_map_assignments a ON a.device_id=d.id AND a.valid_to IS NULL
 JOIN device_registry.point_map_versions m ON m.device_id=a.device_id AND m.version=a.map_version
 CROSS JOIN LATERAL jsonb_array_elements(m.content->'points') point"""


def latest(query, conn, cur, filters, context, cutoff, now, permissions, **_):
    get_well(cur, filters['well_id'])
    clauses, params = ['b.well_id=%s', 'b.valid_to IS NULL'], [filters['well_id']]
    for key, column in (('registry_device_id', 'd.id'), ('point_code', "point->>'point_code'")):
        if key in filters: clauses.append(column+'=%s'); params.append(filters[key])
    where = ' AND '.join(clauses)
    cur.execute(f'SELECT count(*) AS total FROM {SLOTS} WHERE {where}', params)
    total = set_total(context, filters, cur.fetchone()['total'])
    slot_select = "d.id AS registry_device_id,d.gateway_id,d.device_id,point->>'point_code' AS point_code,point->>'unit' AS expected_unit"
    cur.execute(f'SELECT p.*,row_to_json(l) AS observation FROM (SELECT {slot_select} FROM {SLOTS} WHERE {where} '
        "ORDER BY d.id,point->>'point_code' LIMIT %s OFFSET %s) p LEFT JOIN LATERAL ("
        f'SELECT {SELECT} FROM {_RESOLVED_FROM} WHERE w.id=%s AND d.id=p.registry_device_id '
        f'AND s.point_code=p.point_code AND s.received_at<=%s ORDER BY {ORDER} LIMIT 1) l ON TRUE '
        'ORDER BY p.registry_device_id,p.point_code', (*params, filters['page_size'], filters['offset'], filters['well_id'], cutoff))
    items = []
    for row in cur.fetchall():
        sample = resolve(query, row['observation']) if row['observation'] else None
        items.append(public(row) | {'observation': sample, 'interval_basis': 'unknown', 'interval_seconds': None,
            'age_seconds': (now-datetime.fromisoformat(sample['measured_at'])).total_seconds() if sample else None})
    return {'items': items, 'total': total, 'page': filters['page'], 'page_size': filters['page_size']}


def history(query, conn, cur, filters, context, cutoff, now, permissions, *, exporting=False):
    get_well(cur, filters['well_id'])
    clauses = ['w.id=%s', 'd.id=%s', 's.point_code=%s', 's.measured_at>=%s', 's.measured_at<%s', 's.received_at<=%s']
    params = [filters['well_id'], filters['registry_device_id'], filters['point_code'], filters['from_time'], filters['to_time'], cutoff]
    if 'quality' in filters: clauses.append('s.quality=%s'); params.append(filters['quality'])
    where = ' AND '.join(clauses)
    result = paginated(cur, SELECT, _RESOLVED_FROM, where, params, ORDER, filters, context, limit=10001 if exporting else None)
    result['items'] = [resolve(query, row) for row in result['items']]
    if exporting:
        rows = [row | {'reasons': '|'.join(row['reasons'])} for row in result['items']]
        return make_csv('well-history.csv', CSV_FIELDS, rows)
    cur.execute(f'SELECT {SELECT} FROM {_RESOLVED_FROM} WHERE {where} ORDER BY {ORDER} LIMIT 600', params)
    trend = [resolve(query, row) for row in reversed(cur.fetchall())]
    times = [datetime.fromisoformat(row['measured_at']) for row in trend]
    intervals = [(b-a).total_seconds() for a,b in zip(times, times[1:]) if b>a]
    return result | {'from_time': public(filters['from_time']), 'to_time': public(filters['to_time']),
        'trend': {'items': trend, 'point_count': len(trend), 'truncated': int(result['total']) > len(trend),
            'from_time': trend[0]['measured_at'] if trend else None, 'to_time': trend[-1]['measured_at'] if trend else None,
            'interval_basis': 'observed_median_display_only' if intervals else 'unknown',
            'interval_seconds': median(intervals) if intervals else None}}
