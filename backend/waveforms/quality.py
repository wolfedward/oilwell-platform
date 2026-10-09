"""Fixed purpose gates over immutable server-built joint contexts; no I/O."""
from dataclasses import dataclass
from fractions import Fraction

from .evidence_contracts import EvidenceVersion, decimal_value, field_profile_complete
from .registry_context import contains_window, datetime_ns
from .timing import interpolate, sync_bound_ns

VALIDATOR_VERSION='waveform.quality/1'


@dataclass(frozen=True)
class ProfileVersion:
    evidence: EvidenceVersion

    @property
    def id(self): return self.evidence.id
    @property
    def version(self): return self.evidence.version
    @property
    def review_state(self): return self.evidence.review_state
    @property
    def body(self): return self.evidence.body
    @property
    def purpose(self): return self.body['purpose']
    @property
    def required_quantities(self): return self.body['required_quantities']
    @property
    def min_rates(self): return self.body['min_rates']
    @property
    def max_sync_error_ns(self): return self.body['max_sync_error_ns']
    @property
    def ranges(self): return self.body['ranges']
    @property
    def required_evidence(self): return self.body['required_evidence']


@dataclass(frozen=True)
class QualityContext:
    key: object
    completion: object
    manifest_sha256: object
    source_kind: object
    origin_evidence: object
    registry_snapshot: object
    streams: tuple
    calibrations: object
    sampling_configs: object
    sync_evidence: object
    cycle: object
    sample_quality_counts: object
    restrictions: tuple


@dataclass(frozen=True)
class QualityResult:
    purpose: str
    status: str
    reasons: tuple
    evidence_ids: tuple
    validator_version: str = VALIDATOR_VERSION


def channel_identity(data):
    return (data.key,data.stream.stream_id,data.channel.channel_id)


def evidence_items(context,profile):
    values=list(context.calibrations.values())+list(context.sampling_configs.values())
    values.extend(p['evidence'] for p in context.origin_evidence.values())
    if context.sync_evidence is not None: values.append(context.sync_evidence['evidence'])
    if profile is not None: values.append(profile.evidence)
    return tuple({value.id:value for value in values if value is not None}.values())


def evidence_valid(item,context,data=None):
    """Absolute validity uses corrected support and UTC uncertainty once."""
    from datetime import datetime
    if item is None: return 'evidence_unavailable'
    if item.review_state in ('revoked','rejected'): return 'calibration_expired_or_revoked'
    if item.review_state!='approved': return 'evidence_unavailable'
    body=item.body
    if body['valid_from'] is None and body['valid_to'] is None: return None
    bounds=context.cycle.get('absolute_support',{}).get(channel_identity(data) if data else 'joint')
    if bounds is None: return 'evidence_unavailable'
    lower,upper=bounds
    start=datetime_ns(datetime.fromisoformat(body['valid_from'].replace('Z','+00:00'))) if body['valid_from'] else -10**30
    end=datetime_ns(datetime.fromisoformat(body['valid_to'].replace('Z','+00:00'))) if body['valid_to'] else None
    return None if contains_window(start,end,lower,upper) else 'calibration_expired_or_revoked'


def cycle_summary(context):
    data=next((d for d in context.streams if d.role=='displacement' and d.channel.quantity=='displacement'),None)
    start,turn,end=(context.cycle.get(k) for k in ('start_ns','turn_ns','end_ns'))
    if data is None or None in (start,turn,end): return None
    points=[interpolate(data,t) for t in (start,turn,end)]
    if any(p.value is None for p in points): return None
    a,b,c=(p.value for p in points)
    if (b-a)*(c-b)>=0: return None
    summary=context.sample_quality_counts.get(channel_identity(data))
    if summary is not None and 'cycle_minimum' in summary:
        low,high,count=(summary[k] for k in ('cycle_minimum','cycle_maximum','cycle_good_count'))
    else:
        low=high=None; count=0
        for i in range(data.first_index,data.last_index+1):
            value=data.value(i); time=data.time(i)
            if value is not None and time is not None and start<=time<=end:
                count+=1; low=value if low is None else min(low,value); high=value if high is None else max(high,value)
    if low is None: return None
    return dict(stroke=str(high-low),unit=data.channel.unit,cycles_per_minute=str(Fraction(60000000000)/(end-start)),
                valid_sample_count=count,source=dict(gateway_id=data.stream.source['gateway_id'],device_id=data.stream.source['device_id'],
                boot_id=data.stream.source['boot_id'],capture_id=str(data.key.capture_id),stream_id=data.stream.stream_id,channel_id=data.channel.channel_id))


def evaluate(context: QualityContext, *, profile: ProfileVersion | None) -> QualityResult:
    purpose=context.cycle['purpose']
    reasons=[]; hard=[]
    def add(reason,*,failure=False):
        if reason and reason not in reasons: reasons.append(reason)
        if reason and (failure or reason=='calibration_expired_or_revoked') and reason not in hard: hard.append(reason)
    complete=bool(context.completion) and all(c is not None and c['status']=='complete' for c in context.completion.values())
    if not complete: add('incomplete',failure=any(c and c['status']=='invalid' for c in context.completion.values()))
    if context.restrictions: add('capture_conflict',failure=True)
    if profile is None: add('evidence_unavailable')
    elif profile.evidence.kind!='profile' or profile.purpose!=context.cycle['purpose']: add('unsupported_quantity',failure=True)
    else: add(evidence_valid(profile.evidence,context),failure=profile.review_state in ('revoked','rejected'))
    for key,kind in context.source_kind.items():
        if kind is None or kind=='unknown': add('unknown_origin')
        elif purpose=='software_pair_test' and kind not in ('synthetic','bench_measurement'): add('unknown_origin',failure=True)
        elif purpose=='field_pair_input' and kind!='field_measurement': add('unknown_origin',failure=True)
    for origin in context.origin_evidence.values():
        for reason in origin['proof']['reasons']: add(reason,failure=origin['proof']['status']=='ineligible')
        add(evidence_valid(origin['evidence'],context),failure=origin['evidence'] is not None and origin['evidence'].review_state=='revoked')
    if not context.origin_evidence: add('unknown_origin')
    if context.registry_snapshot is None: add('evidence_unavailable')
    elif context.registry_snapshot.status!='resolved':
        for reason in context.registry_snapshot.resolution_reasons:
            add(reason,failure=purpose=='field_pair_input' and context.registry_snapshot.status=='ineligible')
    if context.cycle.get('status')!='resolved': add('cycle_unresolved')
    elif cycle_summary(context) is None: add('cycle_unresolved')
    start,end=context.cycle.get('start_ns'),context.cycle.get('end_ns')
    required={q['role']:q for q in profile.required_quantities} if profile else {}
    rates={q['role']:q['rate'] for q in profile.min_rates} if profile else {}
    ranges={q['role']:q['range'] for q in profile.ranges} if profile else {}
    selected={d.role:d for d in context.streams}
    requested=set(context.cycle.get('selected_roles',selected))
    unresolved=set(context.cycle.get('unresolved_roles',())) | (requested-selected.keys())
    if unresolved: add('selected_channel_unavailable')
    if required.keys()!=requested: add('unsupported_quantity',failure=True)
    electrical=profile.body['electrical_input'] if profile else None
    expected={base+'_'+phase for base in ('voltage','current') for phase in ('a','b','c')}
    if purpose!='raw_archive':
        reference=selected.get('displacement')
        if 'displacement' not in requested or reference is not None and reference.channel.quantity!='displacement':
            add('reference_role_mismatch',failure=True)
    if purpose!='raw_archive' and not unresolved:
        quantities={d.channel.quantity for d in context.streams}
        if not {'load','displacement'}<=quantities: add('unsupported_quantity',failure=True)
        if electrical=='three_phase_instantaneous':
            instantaneous={d.channel.quantity for d in context.streams if d.channel.signal_kind=='instantaneous'}
            if not expected<=instantaneous: add('unsupported_quantity',failure=True)
        elif electrical=='three_phase_total_active_power':
            if not any(d.channel.quantity=='total_active_power' and d.channel.signal_kind in ('instantaneous','window_mean')
                       for d in context.streams): add('unsupported_quantity',failure=True)
        else: add('unsupported_quantity',failure=True)
    for data in context.streams:
        identity=channel_identity(data); channel=data.channel
        if purpose!='raw_archive' and (
                electrical=='three_phase_instantaneous' and channel.quantity in expected and channel.signal_kind!='instantaneous'
                or channel.quantity=='total_active_power' and channel.signal_kind not in ('instantaneous','window_mean')):
            add('unsupported_quantity',failure=True)
        config=context.sampling_configs.get(identity); calibration=context.calibrations.get(identity)
        requirement=required.get(data.role)
        if requirement:
            if any(requirement[k]!=getattr(channel,k) for k in ('quantity','signal_kind','measurement_source')):
                add('unsupported_quantity',failure=True)
            if requirement['unit']!=channel.unit: add('unknown_units',failure=True)
        if channel.measurement_source=='derived': add('unsupported_quantity',failure=True)
        if purpose=='field_pair_input' and channel.quantity in ('load','displacement') and channel.measurement_source!='sensor':
            add('unsupported_quantity',failure=True)
        if config is None: add('config_mismatch')
        else:
            b=config.body
            if b['source_kind']!=context.source_kind.get(data.key): add('unknown_origin',failure=True)
            actual_endpoint=dict(gateway_id=data.stream.source['gateway_id'],device_id=data.stream.source['device_id'],stream_id=data.stream.stream_id,channel_id=channel.channel_id)
            if (dict(b['endpoint'])!=actual_endpoint or channel.config_id is None or b['reported_config_id']!=channel.config_id
                    or any(b[k]!=getattr(channel,k) for k in ('quantity','unit','encoding','measurement_source','signal_kind'))
                    or b['rate'] is None or dict(b['rate'])!={'num':data.stream.rate_num,'den':data.stream.rate_den}):
                add('config_mismatch',failure=True)
            add(evidence_valid(config,context,data),failure=config.review_state in ('revoked','rejected'))
            if channel.quantity.startswith(('voltage_','current_')):
                wiring=b['wiring']
                if not wiring or any(not wiring[k] for k in ('connection','voltage_reference','phase','phase_sequence','current_direction')):
                    add('evidence_unavailable')
            if channel.quantity=='total_active_power':
                power=b['power']
                if not power or any(power[k] is None or power[k]=='' for k in ('method','averaging_window_ns','timestamp_convention','bandwidth_hz','bandwidth_basis')):
                    add('evidence_unavailable')
                elif (decimal_value(power['bandwidth_hz'])<=0 or start is not None and end is not None
                      and int(power['averaging_window_ns'])>=end-start): add('insufficient_coverage',failure=True)
        if calibration is None or data.correction_ns is None or data.scale is None or data.offset is None:
            add('unknown_calibration')
        else:
            b=calibration.body
            if b['source_kind']!=context.source_kind.get(data.key): add('unknown_origin',failure=True)
            if (config is None or str(config.id)!=b['sampling_config_version_id'] or config.content_sha256!=b['sampling_config_sha256']
                    or channel.calibration_id!=calibration.id or dict(b['endpoint'])!=dict(config.body['endpoint'])):
                add('config_mismatch',failure=True)
            if b['unit']!=channel.unit: add('unknown_units',failure=True)
            if any(b[k] is None or b[k]=='' for k in ('calibrated_range','measurement_uncertainty','method','basis','direction','residual_ns')):
                add('unknown_calibration')
            add(evidence_valid(calibration,context,data),failure=calibration.review_state in ('revoked','rejected'))
        rate=rates.get(data.role)
        if rate is None and purpose!='raw_archive': add('evidence_unavailable')
        elif rate and Fraction(data.stream.rate_num,data.stream.rate_den)<Fraction(rate['num'],rate['den']):
            add('insufficient_coverage',failure=True)
        limits=ranges.get(data.role)
        if limits is None and purpose!='raw_archive': add('evidence_unavailable')
        if start is not None and end is not None:
            if interpolate(data,start).gap or interpolate(data,end).gap: add('insufficient_coverage',failure=True)
        # Trusted server summaries cover ALL selected samples, built before locks.
        summary=context.sample_quality_counts.get(identity)
        if summary is None:
            counts=[0]*5; low=high=None
            for index in range(data.first_index,data.last_index+1):
                counts[data.sample(index)[1]]+=1; value=data.value(index)
                if value is not None:
                    low=value if low is None else min(low,value); high=value if high is None else max(high,value)
            summary=dict(counts=counts,minimum=low,maximum=high)
        if summary['counts'][1]: add('missing_samples',failure=True)
        if any(summary['counts'][2:]): add('bad_or_clipped_samples',failure=True)
        for bound in (limits,calibration.body['calibrated_range'] if calibration else None):
            if bound and summary['minimum'] is not None and (bound['unit']!=channel.unit or
                    summary['minimum']<decimal_value(bound['min']) or summary['maximum']>decimal_value(bound['max'])):
                add('out_of_range',failure=True)
    sync=context.sync_evidence
    if sync is None: add('unknown_clock')
    else:
        for reason in sync['proof']['reasons']: add(reason,failure=sync['proof']['status']=='ineligible')
        add(evidence_valid(sync['evidence'],context),failure=sync['evidence'].review_state=='revoked')
        budget=sync_bound_ns(sync.get('budget'),end-start if start is not None and end is not None else 0)
        if budget is None or sync['proof']['status']!='resolved': add('unknown_clock')
        elif profile and profile.max_sync_error_ns is not None and budget>int(profile.max_sync_error_ns): add('sync_budget_exceeded',failure=True)
        if profile and profile.max_sync_error_ns is None and purpose!='raw_archive': add('unknown_clock')
    if purpose=='field_pair_input' and profile and not field_profile_complete(profile.body): add('evidence_unavailable')
    ids=tuple(str(e.id) for e in evidence_items(context,profile))
    if purpose=='raw_archive':
        status='eligible' if complete and profile is not None and profile.review_state=='approved' else 'pending'
        if profile and profile.purpose!=purpose: status='ineligible'
        if any(c and c['status']=='invalid' for c in context.completion.values()): status='ineligible'
    else:
        # Software vectors do not need field registry histories, but retain issues.
        informational={'unregistered','unbound','binding_boundary','inactive_source','unknown_utc','different_wells'} if purpose=='software_pair_test' else set()
        blocking=[r for r in reasons if r not in informational]
        status='ineligible' if hard else 'pending' if blocking else 'eligible'
    return QualityResult(purpose,status,tuple(reasons),ids)
