"""Fixed evidence bodies. No I/O, executable rules, or inferred unknown values."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import json
from math import gcd
import re
from types import MappingProxyType
from uuid import UUID

from .commands import Actor
from .contracts import (CaptureKey, require_fields, require_name, require_text, require_uuid,
                        require_sha, require_i64_string, require_count, require_utc)
from .errors import ContractError

KINDS = frozenset({'sampling_config', 'calibration', 'sync', 'origin', 'profile'})
SOURCE_KINDS = {'synthetic', 'bench_measurement', 'field_measurement', 'unknown'}
SIGNALS = {'instantaneous', 'rms', 'window_mean', 'derived'}
MEASUREMENTS = {'sensor', 'derived', 'synthetic', 'unknown'}
COMMON = {'source_kind', 'valid_from', 'valid_to', 'attachment_ids', 'notes'}
DECIMAL = re.compile(r'(?:0|-?(?:0\.[0-9]*[1-9]|[1-9][0-9]*(?:\.[0-9]*[1-9])?))\Z')


def fail():
    raise ContractError('contract_invalid')


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def freeze(value):
    if isinstance(value, dict): return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list): return tuple(freeze(v) for v in value)
    return value


def thaw(value):
    if hasattr(value,'items'): return {k:thaw(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [thaw(v) for v in value]
    return value


def field_profile_complete(body):
    if body['purpose']!='field_pair_input': return True
    required=('target_equipment_type','electrical_input','trial_basis','max_sync_error_ns')
    if any(body[name] is None or isinstance(body[name],str) and not body[name].strip() for name in required): return False
    if set(body['required_evidence'])!=KINDS-{'profile'}: return False
    if any(r['rate'] is None for r in body['min_rates']) or any(r['range'] is None for r in body['ranges']): return False
    quantities=body['required_quantities']
    if body['electrical_input']=='three_phase_instantaneous':
        expected={base+'_'+phase for base in ('voltage','current') for phase in ('a','b','c')}
        described={r['quantity'] for r in quantities if r['signal_kind']=='instantaneous'}
        return expected<=described
    return any(r['quantity']=='total_active_power' and r['signal_kind'] in ('instantaneous','window_mean') for r in quantities)


def decimal_value(value, *, nonnegative=False):
    if type(value) is not str or len(value)>38 or not DECIMAL.fullmatch(value): fail()
    whole, _, part = value.lstrip('-').partition('.')
    if len(whole)>18 or len(part)>18: fail()
    number = Fraction(Decimal(value))
    if nonnegative and number<0: fail()
    return number


def _enum(value, choices):
    if type(value) is not str or value not in choices: fail()


def _optional(value, validator, **kwargs):
    if value is not None: validator(value, **kwargs)


def _text(value): require_text(value, maximum=2048)
def _unit(value): require_text(value, maximum=32)
def _ns(value): return require_i64_string(value)
def _error_ns(value): return require_i64_string(value, nonnegative=True)


def _uuids(values):
    if type(values) is not list or len(values)>8: fail()
    parsed = [str(require_uuid(v)) for v in values]
    if len(set(parsed))!=len(parsed): fail()
    return parsed


def _endpoint(body):
    require_fields(body, ('gateway_id','device_id','stream_id','channel_id'))
    for value in body.values(): require_name(value)


def _capture(body):
    require_fields(body, ('gateway_id','device_id','capture_id'))
    require_name(body['gateway_id']); require_name(body['device_id'])
    body['capture_id'] = str(require_uuid(body['capture_id']))


def _source(body):
    require_fields(body, ('gateway_id','device_id','boot_id'))
    require_name(body['gateway_id']); require_name(body['device_id'])
    body['boot_id'] = str(require_uuid(body['boot_id']))


def _stream_endpoint(body):
    require_fields(body, ('capture','source','stream_id','clock_domain_id'))
    _capture(body['capture']); _source(body['source'])
    require_name(body['stream_id']); require_name(body['clock_domain_id'])


def _rate(body):
    require_fields(body, ('num','den'))
    n = require_count(body['num'], minimum=1)
    d = require_count(body['den'], minimum=1, maximum=1000000)
    if gcd(n,d)!=1 or not d<=n<=128000*d: fail()


def _range(body):
    require_fields(body, ('min','max','unit'))
    if decimal_value(body['min'])>=decimal_value(body['max']): fail()
    _unit(body['unit'])


def _filter(body):
    require_fields(body, ('method','cutoff_hz','delay_ns','basis'))
    _optional(body['method'], _text); _optional(body['basis'], _text)
    _optional(body['cutoff_hz'], decimal_value, nonnegative=True)
    _optional(body['delay_ns'], _ns)


def _window(body):
    require_fields(body, ('definition','averaging_window_ns','timestamp_convention'))
    _optional(body['definition'], _text)
    _optional(body['averaging_window_ns'], _error_ns)
    _optional(body['timestamp_convention'], _enum, choices={'start','center','end','instantaneous'})


def _sampling(body):
    require_fields(body, COMMON | set('endpoint reported_config_id sensor_id quantity unit encoding measurement_source signal_kind effective_bits input_range gain bias wiring analog_filter digital_filter rate clock window power reported_point_map_version'.split()))
    _endpoint(body['endpoint'])
    for name in ('reported_config_id','reported_point_map_version'): _optional(body[name], require_name)
    _optional(body['sensor_id'], _text)
    require_text(body['quantity']); _unit(body['unit'])
    _enum(body['encoding'], {'i32le','f32le'})
    _enum(body['measurement_source'], MEASUREMENTS); _enum(body['signal_kind'], SIGNALS)
    _optional(body['effective_bits'], require_count, minimum=1, maximum=32)
    _optional(body['input_range'], _range)
    for name in ('gain','bias'): _optional(body[name], decimal_value)
    for name in ('analog_filter','digital_filter'): _optional(body[name], _filter)
    _optional(body['rate'], _rate); _optional(body['window'], _window)
    wiring = body['wiring']
    if wiring is not None:
        require_fields(wiring, ('connection','voltage_reference','phase','phase_sequence','current_direction'))
        for name in ('connection','phase_sequence','current_direction'): _optional(wiring[name], _text)
        _optional(wiring['voltage_reference'], _enum, choices={'phase_to_neutral','line_to_line'})
        _optional(wiring['phase'], _enum, choices={'A','B','C','total'})
    clock = body['clock']
    if clock is not None:
        require_fields(clock, ('clock_domain_id','mode','trigger'))
        require_name(clock['clock_domain_id'])
        _enum(clock['mode'], {'shared_sampling_clock','independent','unknown'})
        _optional(clock['trigger'], _text)
    power = body['power']
    if power is not None:
        require_fields(power, ('method','averaging_window_ns','timestamp_convention','bandwidth_hz','bandwidth_basis'))
        for name in ('method','bandwidth_basis'): _optional(power[name], _text)
        _optional(power['averaging_window_ns'], _error_ns)
        _optional(power['bandwidth_hz'], decimal_value, nonnegative=True)
        _optional(power['timestamp_convention'], _enum, choices={'start','center','end','instantaneous'})


def _calibration(body):
    require_fields(body, COMMON | set('endpoint sampling_config_version_id sampling_config_sha256 scale offset unit calibrated_range zero direction measurement_uncertainty time_correction_ns residual_ns method basis calibrated_at'.split()))
    _endpoint(body['endpoint'])
    body['sampling_config_version_id'] = str(require_uuid(body['sampling_config_version_id']))
    require_sha(body['sampling_config_sha256']); _unit(body['unit'])
    for name in ('scale','offset','zero'): _optional(body[name], decimal_value)
    if body['scale'] is not None and decimal_value(body['scale'])==0: fail()
    _optional(body['measurement_uncertainty'], decimal_value, nonnegative=True)
    _optional(body['calibrated_range'], _range)
    for name in ('direction','method','basis'): _optional(body[name], _text)
    _optional(body['time_correction_ns'], _ns); _optional(body['residual_ns'], _error_ns)
    _optional(body['calibrated_at'], require_utc)


def _sync(body):
    require_fields(body, COMMON | {'method','reference','endpoints'})
    _enum(body['method'], {'shared_sampling_clock','verified_linear_mapping'})
    _stream_endpoint(body['reference'])
    rows = body['endpoints']
    if type(rows) is not list or not 1<=len(rows)<=8: fail()
    identities = []
    for item in rows:
        require_fields(item, ('endpoint','reference_sample_index','reference_event','nominal_reference_ns',
            'rate','valid_start_ns','valid_end_ns','offset_error_ns','frequency_error_ppm','residual_ns','clock_divisor'))
        _stream_endpoint(item['endpoint']); identities.append(canonical_bytes(item['endpoint']))
        require_i64_string(item['reference_sample_index'], nonnegative=True)
        _text(item['reference_event'])
        # Rational reference is checked against manifest-derived canonical text,
        # never parsed as an arbitrary unbounded Fraction supplied by callers.
        _optional(item['nominal_reference_ns'], require_text, maximum=128)
        _optional(item['rate'], _rate)
        for name in ('valid_start_ns','valid_end_ns'): _optional(item[name], _ns)
        if item['valid_start_ns'] is not None and item['valid_end_ns'] is not None:
            if _ns(item['valid_start_ns'])>=_ns(item['valid_end_ns']): fail()
        for name in ('offset_error_ns','residual_ns'): _optional(item[name], _error_ns)
        _optional(item['frequency_error_ppm'], decimal_value, nonnegative=True)
        _optional(item['clock_divisor'], require_count, minimum=1)
        if body['method']=='verified_linear_mapping' and item['clock_divisor'] is not None: fail()
        if item['endpoint']==body['reference']:
            if item['offset_error_ns'] not in (None,'0') or item['frequency_error_ppm'] not in (None,'0'): fail()
    if len(set(identities))!=len(identities) or canonical_bytes(body['reference']) not in identities: fail()


def _origin(body):
    require_fields(body, COMMON | {'capture','source_identity','provenance','verification_method',
                                 'verification_basis','credential_attachment_ids','broker_config_ref'})
    _capture(body['capture']); _source(body['source_identity'])
    p = body['provenance']
    require_fields(p, ('transport','source_ref','observation_id','outcome_id','fact_id','manifest_sha256','import_id'))
    _enum(p['transport'], {'mqtt','file'}); require_sha(p['manifest_sha256'])
    for key in ('source_ref','observation_id','outcome_id','fact_id'): p[key]=str(require_uuid(p[key]))
    if p['import_id'] is not None: p['import_id']=str(require_uuid(p['import_id']))
    if p['transport']=='mqtt' and p['import_id'] is not None: fail()
    if p['transport']=='file' and p['import_id']!=p['source_ref']: fail()
    for name in ('verification_method','verification_basis'): _optional(body[name], _text)
    _optional(body['broker_config_ref'], require_name)
    body['credential_attachment_ids'] = _uuids(body['credential_attachment_ids'])
    if not set(body['credential_attachment_ids'])<=set(body['attachment_ids']): fail()


def _profile(body):
    require_fields(body, COMMON | {'purpose','target_equipment_type','electrical_input','required_quantities',
                                 'min_rates','max_sync_error_ns','ranges','required_evidence','trial_basis'})
    _enum(body['purpose'], {'raw_archive','software_pair_test','field_pair_input'})
    for name in ('target_equipment_type','trial_basis'): _optional(body[name], _text)
    _optional(body['electrical_input'], _enum, choices={'three_phase_instantaneous','three_phase_total_active_power'})
    _optional(body['max_sync_error_ns'], _error_ns)
    rows=body['required_quantities']
    if type(rows) is not list or not (0 if body['purpose']=='raw_archive' else 1)<=len(rows)<=32: fail()
    roles=[]
    for r in rows:
        require_fields(r, ('role','quantity','unit','signal_kind','measurement_source'))
        require_name(r['role']); roles.append(r['role'])
        require_text(r['quantity']); _unit(r['unit'])
        _enum(r['signal_kind'], SIGNALS); _enum(r['measurement_source'], MEASUREMENTS)
    if len(set(roles))!=len(roles): fail()
    for field, key, validator in (('min_rates','rate',_rate),('ranges','range',_range)):
        values=body[field]
        if type(values) is not list or len(values)!=len(roles): fail()
        found=[]
        for item in values:
            require_fields(item, ('role',key)); require_name(item['role'])
            found.append(item['role']); _optional(item[key], validator)
        if set(found)!=set(roles) or len(set(found))!=len(found): fail()
    kinds=body['required_evidence']
    if type(kinds) is not list or len(kinds)>4: fail()
    for kind in kinds: _enum(kind, KINDS-{'profile'})
    if len(set(kinds))!=len(kinds): fail()


def validate_body(kind, body):
    _enum(kind, KINDS)
    if type(body) is not dict: fail()
    try: raw=canonical_bytes(body)
    except (ValueError, TypeError, UnicodeError, RecursionError): fail()
    if len(raw)>65536: fail()
    from .wire import strict_json
    body=strict_json(raw)
    if not COMMON<=body.keys(): fail()
    _enum(body['source_kind'], SOURCE_KINDS)
    for name in ('valid_from','valid_to'): _optional(body[name], require_utc)
    if body['valid_from'] is not None and body['valid_to'] is not None and body['valid_from']>=body['valid_to']: fail()
    body['attachment_ids']=_uuids(body['attachment_ids'])
    _optional(body['notes'], _text)
    {'sampling_config':_sampling,'calibration':_calibration,'sync':_sync,'origin':_origin,'profile':_profile}[kind](body)
    return body


def evidence_content(kind, body):
    raw=canonical_bytes({'protocol':'waveform.evidence-content/1','kind':kind,'body':validate_body(kind,body)})
    if len(raw)>65536: fail()
    return raw, sha256(raw).hexdigest()


def validate_attachment(payload, media_type):
    if type(payload) is not bytes or not 1<=len(payload)<=4194304: fail()
    if media_type=='application/pdf': valid=payload.startswith(b'%PDF-')
    elif media_type=='image/png': valid=payload.startswith(b'\x89PNG\r\n\x1a\n')
    elif media_type=='image/jpeg': valid=payload.startswith(b'\xff\xd8\xff') and len(payload)>=6 and payload.endswith(b'\xff\xd9')
    else: valid=False
    if not valid: fail()


def sync_proof(body, manifests, *, approved):
    """Consistency of documented mappings; no assertion of report truth or timing accuracy.

    Returned reference coordinates preserve age for Task11 drift calculations.
    Differential endpoint errors are not another nominal time correction.
    """
    body=validate_body('sync',body)
    if type(approved) is not bool: fail()
    reasons=[]; rows=[]; reference=None
    if not approved or not body['notes'] or not body['notes'].strip() or not body['attachment_ids']:
        reasons.append('evidence_unavailable')
    for item in body['endpoints']:
        endpoint=item['endpoint']; cap=endpoint['capture']
        key=CaptureKey(cap['gateway_id'],cap['device_id'],UUID(cap['capture_id']))
        frame=manifests.get(key)
        if frame is None:
            reasons.append('evidence_unavailable'); continue
        stream=next((s for s in frame.streams if s.stream_id==endpoint['stream_id']),None)
        if stream is None or stream.source!=endpoint['source'] or stream.clock_domain_id!=endpoint['clock_domain_id']: fail()
        if body['source_kind']!=frame.body['source_kind']: fail()
        index=int(item['reference_sample_index'])
        if not stream.first_sample_index<=index<stream.first_sample_index+stream.frame_count: fail()
        nominal=Fraction(stream.offset_ns)+Fraction((index-stream.first_sample_index)*1000000000*stream.rate_den,stream.rate_num)
        if item['nominal_reference_ns'] is not None and item['nominal_reference_ns']!=str(nominal): fail()
        if item['rate'] is not None and item['rate']!={'num':stream.rate_num,'den':stream.rate_den}: fail()
        if item['valid_start_ns'] is not None and nominal<int(item['valid_start_ns']): fail()
        if item['valid_end_ns'] is not None and nominal>=int(item['valid_end_ns']): fail()
        required=('nominal_reference_ns','rate','valid_start_ns','valid_end_ns','offset_error_ns','frequency_error_ppm','residual_ns')
        if any(item[name] is None for name in required) or not item['reference_event'].strip(): reasons.append('unknown_clock')
        if body['method']=='shared_sampling_clock' and item['clock_divisor'] is None: reasons.append('unknown_clock')
        utc=frame.body['utc_start']
        utc_ns=None
        if utc is not None:
            dt=datetime.fromisoformat(utc.replace('Z','+00:00'))-datetime(1970,1,1,tzinfo=timezone.utc)
            utc_ns=(dt.days*86400+dt.seconds)*1000000000+dt.microseconds*1000
        bound=None if item['offset_error_ns'] is None or item['residual_ns'] is None else int(item['offset_error_ns'])+int(item['residual_ns'])
        proof=dict(item,manifest_sha256=frame.raw_sha256,nominal_reference_ns=str(nominal),
            time_basis={'unit':'ns','reference':'capture.utc_start','coordinates':'common_nominal_before_channel_correction'},
            mapping_role='reference' if endpoint==body['reference'] else 'differential',
            utc_start=utc,utc_uncertainty_ns=frame.body['utc_uncertainty_ns'])
        row=(proof,key,nominal,utc_ns,bound,frame)
        rows.append(row)
        if endpoint==body['reference']: reference=row
    if reference is None: reasons.append('evidence_unavailable')
    else:
        ref,refkey,reftime,refutc,refbound,refframe=reference
        for item,key,nominal,utc_ns,bound,frame in rows:
            if item['reference_event']!=ref['reference_event']: fail()
            if body['method']=='shared_sampling_clock' and item['clock_divisor'] is not None and ref['clock_divisor'] is not None:
                s=next(s for s in frame.streams if s.stream_id==item['endpoint']['stream_id'])
                rs=next(s for s in refframe.streams if s.stream_id==ref['endpoint']['stream_id'])
                if Fraction(s.rate_num,s.rate_den)*item['clock_divisor']!=Fraction(rs.rate_num,rs.rate_den)*ref['clock_divisor']: fail()
            if bound is None or refbound is None: continue
            allowance=bound+refbound
            if key==refkey: difference=abs(nominal-reftime)
            else:
                uncertainty=frame.body['utc_uncertainty_ns']; ref_uncertainty=refframe.body['utc_uncertainty_ns']
                if utc_ns is None or refutc is None or uncertainty is None or ref_uncertainty is None:
                    reasons.append('unknown_utc'); continue
                difference=abs(utc_ns+nominal-refutc-reftime)
                allowance+=int(uncertainty)+int(ref_uncertainty)
            if difference>allowance: reasons.append('reference_event_mismatch')
    reasons=tuple(dict.fromkeys(reasons))
    return {'status':'ineligible' if 'reference_event_mismatch' in reasons else 'pending' if reasons else 'resolved',
            'reasons':reasons,'endpoints':tuple(freeze(r[0]) for r in rows)}


@dataclass(frozen=True)
class EvidenceVersion:
    id: UUID
    kind: str
    content_sha256: str
    body: object
    creator: Actor
    version: int
    review_state: str
    accounting_bucket: str


@dataclass(frozen=True)
class Attachment:
    id: UUID
    media_type: str
    bytes: int
    sha256: str
    accounting_bucket: str


@dataclass(frozen=True)
class Restriction:
    id: UUID
    subject_kind: str
    subject_id: UUID
    reason_code: str
    recorded_at: object
    source_event_id: UUID
