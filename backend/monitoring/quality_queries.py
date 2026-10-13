"""Three separate quality scopes; immutable measurements are never corrected here."""
from backend.registry.queries import _RESOLVED_FROM
from backend.ingest_reliability.queries import _CASE
from .contracts import window
from .queries import paginated, public
from .telemetry_queries import SELECT, resolve


# This SQL mirrors registry.resolution.assess so reason filtering precedes pagination.
# Python assessment remains the output authority and is checked against this projection.
REASONS = """array_remove(ARRAY[
 CASE WHEN d.id IS NULL THEN 'not_registered' END,
 CASE WHEN d.id IS NOT NULL AND b.id IS NULL THEN 'unbound' END,
 CASE WHEN d.id IS NOT NULL AND st.status IS DISTINCT FROM 'active' THEN 'state_not_active' END,
 CASE WHEN d.id IS NOT NULL AND a.map_version IS NULL THEN 'no_expected_map' END,
 CASE WHEN d.id IS NOT NULL AND a.map_version IS NOT NULL AND expected.point IS NULL THEN 'unknown_point' END,
 CASE WHEN d.id IS NOT NULL AND expected.point IS NOT NULL AND s.unit IS DISTINCT FROM expected.point->>'unit' THEN 'unit_mismatch' END,
 CASE WHEN d.id IS NOT NULL AND expected.point IS NOT NULL AND
   (s.value<(expected.point->>'engineering_min')::double precision OR s.value>(expected.point->>'engineering_max')::double precision)
   THEN 'out_of_range' END,
 CASE WHEN extract(epoch FROM (s.measured_at-s.received_at))>%s OR (d.id IS NOT NULL AND st.status IS NULL) THEN 'clock_anomaly' END,
 CASE WHEN s.quality='bad' THEN 'quality_bad' WHEN s.quality='uncertain' THEN 'quality_uncertain' END
 ],NULL)"""
EXPECTED = " LEFT JOIN LATERAL (SELECT p AS point FROM jsonb_array_elements(m.content->'points') p WHERE p->>'point_code'=s.point_code LIMIT 1) expected ON TRUE"
CASE_BINDING = """EXISTS (SELECT 1 FROM device_registry.devices d JOIN device_registry.well_bindings b ON b.device_id=d.id
 WHERE d.gateway_id=q.gateway_id AND d.device_id=q.device_id AND d.created_at<=q.received_at
 AND tstzrange(b.valid_from,b.valid_to,'[)') @> q.received_at {well})"""


def quality(query, conn, cur, filters, context, cutoff, now, permissions, **_):
    view = filters['view']
    start, end = window(filters, cutoff)
    clauses, params = ['q.received_at>=%s', 'q.received_at<%s', 'q.received_at<=%s'], [start, end, cutoff]
    for key in ('gateway_id','device_id','point_code','quality','state'):
        if key in filters: clauses.append('q.'+key+'=%s'); params.append(filters[key])
    result_extra = {'view':view, 'from_time':public(start), 'to_time':public(end), 'time_basis':'received_at',
        'coverage':'selected_well_only' if 'well_id' in filters else 'global_or_device',
        'unresolved_available_separately':True}
    if view == 'cases':
        source, select, order = '('+_CASE+') q', 'q.*', 'q.received_at DESC,q.id DESC'
        result_extra |= {'scope':'current_case_state', 'status_queried_at':public(now)}
        if 'reason_code' in filters: clauses.append('q.reason_code=%s'); params.append(filters['reason_code'])
        if 'well_id' in filters:
            clauses.append(CASE_BINDING.format(well='AND b.well_id=%s')); params.append(filters['well_id'])
            result_extra['well_filter_basis'] = 'binding_at_fact_received_time_not_sample_attribution'
        if 'unresolved' in filters:
            clauses.append(('NOT ' if filters['unresolved']=='true' else '')+CASE_BINDING.format(well=''))
    elif view == 'resolution':
        source = f'(SELECT {SELECT},{REASONS} AS resolution_reasons FROM {_RESOLVED_FROM}{EXPECTED}) q'
        params.insert(0, query.clock_tolerance_seconds)
        select, order = 'q.*', 'q.measured_at DESC,q.message_id DESC,q.sample_index DESC'
        result_extra['scope'] = 'historical_registry_resolution'
        if 'well_id' in filters: clauses.append('q.well_id=%s'); params.append(filters['well_id'])
        if 'reason_code' in filters: clauses.append('%s=ANY(q.resolution_reasons)'); params.append(filters['reason_code'])
        if 'unresolved' in filters: clauses.append('q.well_id IS '+('NULL' if filters['unresolved']=='true' else 'NOT NULL'))
    else:
        select, order = 'q.*', 'q.measured_at DESC,q.message_id DESC,q.sample_index DESC'
        source = 'public.telemetry_samples q'
        result_extra['scope'] = 'reported_observation_quality'
        if 'well_id' in filters or 'unresolved' in filters:
            source = f'(SELECT s.*,w.id AS well_id FROM {_RESOLVED_FROM}) q'
            if 'well_id' in filters: clauses.append('q.well_id=%s'); params.append(filters['well_id'])
            if 'unresolved' in filters: clauses.append('q.well_id IS '+('NULL' if filters['unresolved']=='true' else 'NOT NULL'))
    result = paginated(cur, select, source, ' AND '.join(clauses), params, order, filters, context)
    if view == 'resolution':
        items = []
        for row in result['items']:
            sql_reasons = row.pop('resolution_reasons')
            item = resolve(query, row)
            if set(sql_reasons) != set(item['reasons']):
                from .errors import MonitoringError
                raise MonitoringError('monitoring.resolution_mismatch', '解析口径不一致，查询已中止', 503)
            items.append(item)
        result['items'] = items
    return result | result_extra
