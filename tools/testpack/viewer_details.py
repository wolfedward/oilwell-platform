"""Read-only projections of decoded samples, never predictions or field evidence."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
import math


def inversion_detail(pack,report,case_id):
    from hashlib import sha256
    from .report import validate_public
    if report is None:return {'approved_execution_status':'not_run','case_id':case_id}
    validate_public(report)
    if report['pack_id']!=pack.manifest['pack_id'] or report['pack_sha256']!=sha256((pack.root/'manifest.json').read_bytes()).hexdigest():
        raise ValueError('report belongs to another package')
    rows=[row for row in report.get('inversion_outcomes',[]) if row['case_id']==case_id]
    return rows[0] if rows else {'approved_execution_status':'not_run','case_id':case_id}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _validate(channel):
    if len(channel['values']) != len(channel['quality']):
        raise ValueError('channel count mismatch')
    if any(type(q) is not int or q not in range(5) for q in channel['quality']):
        raise ValueError('unknown quality code')


def quality_summary(channels):
    rows = []
    for role, channel in channels.items():
        _validate(channel)
        codes = {str(q): channel['quality'].count(q) for q in range(5)}
        invalid = sum(q == 0 and not _finite(v) for v, q in zip(channel['values'], channel['quality']))
        rows.append({'role': role, 'unit': channel['unit'], 'total': len(channel['values']),
                     'valid': codes['0']-invalid, 'missing': codes['1'],
                     'abnormal': sum(codes[str(q)] for q in (2, 3, 4))+invalid,
                     'codes': codes, 'invalid_numeric': invalid})
    return {'status': 'has_invalid_samples' if any(r['missing'] or r['abnormal'] for r in rows)
            else 'usable_samples', 'channels': rows}


def reference_summary(channels, *, cycle_support_complete=True):
    load, displacement = (channels[r] for r in ('load', 'displacement'))
    for channel in (load, displacement): _validate(channel)
    if load['unit'] != 'kN' or displacement['unit'] != 'm':
        raise ValueError('reference units must be kN and m')
    count = len(load['values'])
    if count != len(displacement['values']): raise ValueError('reference count mismatch')
    pairs = list(zip(load['values'], displacement['values']))
    good = [load['quality'][i] == displacement['quality'][i] == 0 and
            _finite(force) and _finite(position) for i, (force, position) in enumerate(pairs)]
    usable = [p for p, valid in zip(pairs, good) if valid]
    result = {'valid_pairs': len(usable), 'total_pairs': count,
              'load_range_kn': [min(p[0] for p in usable), max(p[0] for p in usable)] if usable else None,
              # Partial extrema must not be called a complete stroke.
              'stroke_m': max(p[1] for p in pairs)-min(p[1] for p in pairs) if count and all(good) and cycle_support_complete else None,
              'area_kj': None, 'signed_work_kj': None, 'area_reason': None,
              'area_method': 'abs(sum((F[i-1]+F[i])/2 * (s[i]-s[i-1]))',
              'closure_rule': 'exact decoded endpoint equality; no added closing edge'}
    if not cycle_support_complete: result['area_reason'] = 'incomplete_cycle_support'
    elif count < 4: result['area_reason'] = 'insufficient_points'
    elif not all(good): result['area_reason'] = 'invalid_reference_samples'
    elif pairs[0] != pairs[-1]: result['area_reason'] = 'open_endpoints'
    else:
        # kN*m = kJ. Decimal avoids accidental closure tolerances or interpolation.
        native = [(Decimal(str(f)), Decimal(str(s))) for f, s in pairs]
        work = sum((a[0]+b[0])*(b[1]-a[1])/2 for a, b in zip(native, native[1:]))
        result.update(area_kj=float(abs(work)), signed_work_kj=float(work))
    return result


def capture_summary(wire, metadata, channels):
    if wire['source_kind'] != 'synthetic' or metadata['source_kind'] != 'synthetic' or len(wire['streams']) != 1:
        raise ValueError('viewer requires one synthetic native stream')
    stream = wire['streams'][0]
    rate = Fraction(stream['rate_num'], stream['rate_den'])
    count = stream['frame_count']
    if rate <= 0 or count <= 0 or metadata['frame_count'] != count or Fraction(str(metadata['sample_rate_hz'])) != rate:
        raise ValueError('metadata cadence mismatch')
    descriptors = {c['channel_id']: c for c in stream['channels']}
    if set(descriptors) != set(channels): raise ValueError('metadata channels mismatch')
    for role, channel in channels.items():
        _validate(channel)
        if (len(channel['values']) != count or channel['unit'] != descriptors[role]['unit'] or
                descriptors[role]['measurement_source'] != 'synthetic'):
            raise ValueError('channel declaration mismatch')
    at = datetime.fromisoformat(wire['utc_start'].replace('Z', '+00:00')) if wire['utc_start'] is not None else None
    if at is not None and at.tzinfo is None: raise ValueError('simulation time must include timezone')
    cycle = wire['cycle']
    first = Fraction(stream['offset_ns'])
    last = first+Fraction(count-1)*1000000000/rate
    complete = bool(cycle and first == int(cycle['start_ns']) and last == int(cycle['end_ns']))
    duration = Fraction(int(cycle['end_ns'])-int(cycle['start_ns']), 1000000000) if cycle else None
    if duration is not None and duration <= 0: raise ValueError('invalid cycle duration')
    return {'device_id': wire['device_id'], 'gateway_id': wire['gateway_id'], 'capture_id': wire['capture_id'],
            'source_kind': 'synthetic', 'utc_start': wire['utc_start'],
            'simulation_start_local': at.astimezone(timezone(timedelta(hours=8))).isoformat() if at is not None else None,
            'display_timezone': 'Asia/Shanghai', 'window_s': int(wire['window_ns'])/1000000000,
            'sample_span_s': float(Fraction(count-1)/rate), 'sample_rate_hz': float(rate),
            'frame_count': count, 'first_sample_index': stream['first_sample_index'],
            'offset_ns': stream['offset_ns'], 'sample_period_ns': str(Fraction(1000000000)/rate),
            'cycle_support_complete': complete,
            'cycle_duration_s': float(duration) if duration is not None else None,
            'strokes_per_min': float(60/duration) if duration is not None else None,
            'cycle': cycle}


def scene_summary(case_id, category):
    names = {'base': '基准模型', 'balance': '平衡参数变化', 'friction': '摩擦参数变化',
             'speed': '周期速度变化', 'noise': '注入噪声', 'missing': '功率缺测',
             'clipped': '功率削顶', 'shifted': '功率错位 2 点（15.625 ms）'}
    key = case_id.removeprefix('cycle-')
    return {'category': category, 'label': names.get(key, '模型参数变体' if key.startswith('variant-') else case_id),
            'perturbation': key if key in ('noise', 'missing', 'clipped', 'shifted') else None,
            'physical_condition_confirmed': False}
