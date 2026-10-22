"""Exact observed applicability and cursor-owned dependency rechecks.

Calibration cycle intervals are absolute Unix nanoseconds, never offsets.
Unlinked historic declarations remain unproven; numerical success is not field
certification. All public rechecks use only the supplied guarded cursor.
"""
from fractions import Fraction
from hashlib import sha256
from uuid import UUID
from backend.waveforms.capacity import row
from backend.waveforms.pair_contracts import public,capture_key
from backend.waveforms.registry_context import resolve_with_cursor,datetime_ns
from .contracts import canonical_bytes,utc,strict_json
from .parameters import covers_observation,refuse,ParameterService
from .models import ModelService
from .input_contracts import parse_run_request
from .transactions import InversionError


def operating_conditions(parameters,ids,start,end,uncertainty):
    records={item.id:item for item in parameters.provenance.operating_conditions}
    if not ids or len(set(ids))!=len(ids) or any(id not in records for id in ids):
        refuse('operating_conditions_unknown')
    chosen=tuple(records[id] for id in ids)
    if any(not covers_observation(item.valid_from,item.valid_to,start,end,uncertainty) for item in chosen):
        refuse('operating_condition_interval')
    return chosen


def _no_connection():
    raise RuntimeError('History checks must use their transaction cursor')


def approved_with_cursor(cur,request,bucket,*,start,end,uncertainty,alignment):
    parameters=ParameterService(_no_connection,read_connect=_no_connection,authorize=_no_connection,accounting_bucket=bucket)
    models=ModelService(_no_connection,read_connect=_no_connection,authorize=_no_connection)
    parameter=parameters._get(cur,request.parameter_id); model=models._get(cur,request.model_id)
    if (parameter.content_sha256!=request.parameter_sha256 or parameter.review_version!=request.parameter_review_version
        or parameter.assembly_id!=request.assembly_id or parameter.review_state!='approved'):
        refuse('parameter_pin_changed')
    if model.content_sha256!=request.model_sha256 or model.review_version!=request.model_review_version or model.review_state!='approved':
        refuse('model_pin_changed')
    parsed=parameters._validate(cur,parameter,start=start,end=end,uncertainty_ns=uncertainty,alignment_ns=alignment)
    models._validate(model)
    cur.execute('SELECT id,version,body FROM inversion.selections WHERE assembly_id=%s ORDER BY version DESC LIMIT 1',(request.assembly_id,))
    selection=cur.fetchone()
    if selection is None or selection[1]!=request.selection_version: refuse('selection_changed')
    for key in ('parameter_id','parameter_sha256','parameter_review_version','model_id','model_sha256','model_review_version'):
        expected=getattr(request,key); expected=str(expected) if type(expected) is UUID else expected
        if selection[2].get(key)!=expected: refuse('selection_changed')
    if (parsed.policy.model_version!=model.installed_id or parsed.policy.algorithm!=model.body['artifact']['algorithm']
        or parsed.schema!=model.body['artifact']['parameter_schema']
        or selection[2]['policy_sha256']!=parameter.body['policy']['content_sha256']): refuse('selection_incompatible')
    return parameter,model,parsed,dict(id=str(selection[0]),version=selection[1],body=selection[2])


def archived_channels(cur,parameters,selected,*,bucket):
    """Verify evidence-capture metadata without expanding the original-read bound."""
    archived=[]; by_capture={}
    for binding in parameters.provenance.channel_bindings:
        pin=binding.channel_pin
        current=selected[binding.role]
        for key in ('physical_endpoint','stream_id','channel_id','sampling_pin','calibration_pin'):
            if getattr(pin,key)!=getattr(current,key): refuse('channel_installation_changed')
        key=(pin.gateway_id,pin.device_id,pin.capture_id)
        if key not in by_capture:
            cur.execute('SELECT capture_pk,gateway_id,device_id,capture_id,manifest_sha256,state,completion_status,restricted,utc_start,utc_uncertainty_ns,source_kind,cycle,stream_id,specification FROM waveforms.archive_channel_metadata WHERE gateway_id=%s AND device_id=%s AND capture_id=%s ORDER BY stream_id LIMIT 33',key)
            records=[]
            while (record:=row(cur)) is not None: records.append(record)
            if not records or len(records)>32: refuse('archive_capture_missing')
            by_capture[key]=records
        records=by_capture[key]; found=next((r for r in records if r['stream_id']==pin.stream_id),None)
        if found is None or found['manifest_sha256']!=pin.manifest_sha256: refuse('archive_manifest_changed')
        if found['state']!='complete' or found['completion_status']!='complete' or found['restricted']: refuse('archive_restricted')
        if found['source_kind']!=parameters.source_kind: refuse('archive_source_changed')
        spec=found['specification']; channel=next((c for c in spec['channels'] if c['channel_id']==pin.channel_id),None)
        if channel is None or channel['calibration_id']!=str(pin.calibration_pin.id): refuse('archive_channel_changed')
        lo=int(spec['first_sample_index']); hi=lo+spec['frame_count']-1
        if not lo<=pin.first_index<=pin.last_index<=hi: refuse('archive_range_changed')
        cur.execute('SELECT body FROM waveforms.evidence_versions WHERE evidence_id=%s',(pin.calibration_pin.id,))
        calibration=cur.fetchone()[0]
        cur.execute('SELECT body FROM waveforms.evidence_versions WHERE evidence_id=%s',(pin.sampling_pin.id,))
        sampling=cur.fetchone()[0]
        if (pin.physical_endpoint.gateway_id!=spec['source']['gateway_id'] or pin.physical_endpoint.device_id!=spec['source']['device_id']
            or sampling['endpoint']!={'gateway_id':spec['source']['gateway_id'],'device_id':spec['source']['device_id'],
                                  'stream_id':pin.stream_id,'channel_id':pin.channel_id}
            or any(channel[key]!=sampling[key] for key in ('quantity','unit','encoding','measurement_source','signal_kind'))
            or sampling['rate']!={'num':spec['rate_num'],'den':spec['rate_den']}
            or channel['config_id']!=sampling['reported_config_id'] or channel['unit']!=calibration['unit']):
            refuse('archive_channel_changed')
        anchor=datetime_ns(utc(found['utc_start'])) if found['utc_start'] is not None else None
        correction=int(calibration['time_correction_ns'])
        def time(index):
            return Fraction(anchor+int(spec['offset_ns'])+correction)+Fraction((index-lo)*1000000000*spec['rate_den'],spec['rate_num']) if anchor is not None else None
        start,end=time(pin.first_index),time(pin.last_index)
        if start is not None and found['utc_uncertainty_ns'] is not None:
            for evidence in (sampling,calibration):
                if not covers_observation(utc(evidence['valid_from']),utc(evidence['valid_to']) if evidence['valid_to'] else None,
                                          start,end,int(found['utc_uncertainty_ns'])): refuse('archive_evidence_interval')
        cycle=found['cycle']; physical=None; cycle_start=cycle_end=None
        if cycle and anchor is not None:
            identities=[]
            for event in cycle['reference_events']:
                stream=next((r['specification'] for r in records if r['stream_id']==event['stream_id']),None)
                if stream is None or event['sample_index'] is None: break
                identities.append(dict(source=stream['source'],stream_id=event['stream_id'],
                                       channel_id=cycle['reference_channel'],index=event['sample_index']))
            reference=next((b.channel_pin for b in parameters.provenance.channel_bindings
                if (b.channel_pin.gateway_id,b.channel_pin.device_id,b.channel_pin.capture_id)==key
                and b.channel_pin.channel_id==cycle['reference_channel']),None)
            if len(identities)==3 and reference is not None:
                cur.execute('SELECT body FROM waveforms.evidence_versions WHERE evidence_id=%s',(reference.calibration_pin.id,))
                ref_correction=int(cur.fetchone()[0]['time_correction_ns'])
                cycle_start=Fraction(anchor+int(cycle['start_ns'])+ref_correction)
                cycle_end=Fraction(anchor+int(cycle['end_ns'])+ref_correction)
                if start<=cycle_start<cycle_end<=end:
                    physical=sha256(canonical_bytes(identities)).hexdigest()
        archived.append(dict(role=binding.role,capture_pk=str(found['capture_pk']),pin=public(pin),
            specification=spec,utc_start=found['utc_start'],utc_uncertainty_ns=found['utc_uncertainty_ns'],
            source_kind=found['source_kind'],absolute_start_ns=str(start) if start is not None else None,
            absolute_end_ns=str(end) if end is not None else None,physical_cycle_key=physical,
            cycle_start_ns=str(cycle_start) if physical else None,cycle_end_ns=str(cycle_end) if physical else None,
            calibration_residual_ns=calibration['residual_ns'],
            source_ranges=[dict(source=spec['source'],stream_id=pin.stream_id,channel_id=pin.channel_id,
                                first_index=str(pin.first_index),last_index=str(pin.last_index))]))
    _archive_timing(cur,parameters,archived,by_capture,bucket)
    return archived


def _archive_timing(cur,parameters,archives,records,bucket):
    """Verify explicit archive mappings using metadata, never another raw file.

    Endpoint error follows timing.sync_bound_ns: offset + residual + age*ppm.
    The conservative absolute bound adds distinct own/reference endpoint errors, channel
    calibration residual and UTC anchor error. Bounds never correct time twice.
    """
    from backend.waveforms.contracts import CaptureKey,ManifestFrame
    from backend.waveforms.evidence_contracts import sync_proof,decimal_value
    from backend.waveforms.errors import ContractError
    from backend.waveforms.wire import _stream
    frames={CaptureKey(*key):ManifestFrame(CaptureKey(*key),rows[0]['manifest_sha256'],
        {field:rows[0][field] for field in ('utc_start','utc_uncertainty_ns','source_kind')},
        tuple(_stream(row['specification']) for row in rows)) for key,rows in records.items()}
    service=ParameterService(_no_connection,read_connect=_no_connection,authorize=_no_connection,accounting_bucket=bucket)
    cache={}
    for archive,binding in zip(archives,parameters.provenance.channel_bindings):
        pin=binding.channel_pin; sync=pin.sync_pin
        terms=dict(utc_uncertainty_ns=archive['utc_uncertainty_ns'],calibration_residual_ns=archive['calibration_residual_ns'],
                   endpoint_bound_ns=None,reference_bound_ns=None,total_bound_ns=None,status='archived_sync_budget_unproven')
        archive['timing']=terms; archive['sync_evidence']=None
        if sync is None: continue
        if sync not in cache:
            body=service._sync_evidence(cur,sync,parameters.source_kind)
            try: proof=sync_proof(body,frames,approved=True)
            except ContractError: refuse('archive_sync_invalid')
            cache[sync]=(body,proof)
        body,proof=cache[sync]
        archive['sync_evidence']=dict(pin=public(sync),body=body,proof=public(proof))
        if not any(capture_key(p['endpoint']['capture'])==CaptureKey(pin.gateway_id,pin.device_id,pin.capture_id)
                   and p['endpoint']['stream_id']==pin.stream_id for p in body['endpoints']): refuse('archive_sync_endpoint')
        if proof['status']=='ineligible': refuse('archive_sync_invalid')
        if proof['status']!='resolved': continue
        endpoint=next((p for p in proof['endpoints'] if capture_key(p['endpoint']['capture'])==CaptureKey(pin.gateway_id,pin.device_id,pin.capture_id)
                       and p['endpoint']['stream_id']==pin.stream_id),None)
        reference=next((p for p in proof['endpoints'] if p['mapping_role']=='reference'),None)
        if endpoint is None or reference is None: refuse('archive_sync_endpoint')
        def bound(point,lo,hi):
            if not int(point['valid_start_ns'])<=lo<=hi<int(point['valid_end_ns']): refuse('archive_sync_interval')
            age=max(abs(lo-Fraction(point['nominal_reference_ns'])),abs(hi-Fraction(point['nominal_reference_ns'])))
            return Fraction(int(point['offset_error_ns'])+int(point['residual_ns']))+age*decimal_value(point['frequency_error_ppm'])/1000000
        spec=archive['specification']; first=int(spec['first_sample_index'])
        def nominal(index): return Fraction(int(spec['offset_ns']))+Fraction((index-first)*1000000000*spec['rate_den'],spec['rate_num'])
        own=bound(endpoint,nominal(pin.first_index),nominal(pin.last_index))
        ref_frame=frames[capture_key(reference['endpoint']['capture'])]
        ref_stream=next(s for s in ref_frame.streams if s.stream_id==reference['endpoint']['stream_id'])
        # A nonselected reference endpoint remains bounded metadata only. Its
        # whole declared stream range is a conservative validity/clock-age check.
        ref_low=Fraction(ref_stream.offset_ns)
        ref_high=ref_low+Fraction((ref_stream.frame_count-1)*1000000000*ref_stream.rate_den,ref_stream.rate_num)
        ref=bound(reference,ref_low,ref_high)
        terms.update(endpoint_bound_ns=str(own),reference_bound_ns=str(ref))
        if archive['utc_uncertainty_ns'] is None or archive['calibration_residual_ns'] is None: continue
        # A shared reference endpoint is one physical clock, not two errors.
        additional=Fraction(0) if endpoint['endpoint']==reference['endpoint'] else ref
        terms['reference_additional_bound_ns']=str(additional)
        total=Fraction(int(archive['utc_uncertainty_ns'])+int(archive['calibration_residual_ns']))+own+additional
        if capture_key(reference['endpoint']['capture'])!=CaptureKey(pin.gateway_id,pin.device_id,pin.capture_id):
            if ref_frame.body['utc_uncertainty_ns'] is None: continue
            total+=int(ref_frame.body['utc_uncertainty_ns'])
        if archive['absolute_start_ns'] is None: continue
        if body.get('valid_from') is not None and not covers_observation(utc(body['valid_from']),utc(body['valid_to']) if body.get('valid_to') else None,
                Fraction(archive['absolute_start_ns']),Fraction(archive['absolute_end_ns']),total): refuse('archive_sync_interval')
        terms.update(total_bound_ns=str(total),status='verified_mapping_bound')


def calibration_independence(parameters,archives,physical_key,start,end,uncertainty,evaluated_ranges):
    reasons=[]
    for declared in parameters.provenance.calibration_cycles:
        linked=[a for a in archives if a.get('physical_cycle_key')==declared.physical_cycle_id
                and a.get('cycle_start_ns')==str(declared.start_ns) and a.get('cycle_end_ns')==str(declared.end_ns)]
        if not linked: reasons.append('independence_unproven')
        if declared.physical_cycle_id==physical_key or not (declared.end_ns<start-uncertainty or declared.start_ns>end+uncertainty):
            reasons.append('calibration_cycle_overlap')
    for archive in archives:
        if archive['absolute_start_ns'] is None or archive['absolute_end_ns'] is None:
            reasons.append('independence_unproven'); continue
        if archive['physical_cycle_key']==physical_key: reasons.append('calibration_cycle_overlap')
        archive_uncertainty=archive.get('timing',{}).get('total_bound_ns')
        if archive_uncertainty is None:
            reasons.extend(('independence_unproven','archived_sync_budget_unproven'))
        elif not (Fraction(archive['absolute_end_ns'])+Fraction(archive_uncertainty)<start-uncertainty
                  or Fraction(archive['absolute_start_ns'])-Fraction(archive_uncertainty)>end+uncertainty):
            reasons.append('calibration_time_overlap')
        for original in archive['source_ranges']:
            for current in evaluated_ranges:
                if (original['source'],original['stream_id'],original['channel_id'])==(current['source'],current['stream_id'],current['channel_id']):
                    if max(int(original['first_index']),int(current['first_index']))<=min(int(original['last_index']),int(current['last_index'])):
                        reasons.append('calibration_source_overlap')
    return tuple(sorted(set(reasons)))


def modbus_maps(cur,parameters,start,end,uncertainty):
    result=[]
    for binding in parameters.provenance.channel_bindings:
        if binding.source=='native_waveform': continue
        pin=binding.channel_pin; mapping=binding.modbus
        cur.execute('SELECT id FROM waveforms.registry_devices WHERE gateway_id=%s AND device_id=%s',
                    (pin.physical_endpoint.gateway_id,pin.physical_endpoint.device_id))
        device=cur.fetchone()
        if not device or str(device[0])!=mapping.device_id: refuse('modbus_device_mismatch')
        cur.execute('SELECT device_id,version,transport,canonical_rule,content,sha256,status,created_at,reviewed_at,created_event_id,review_event_id FROM waveforms.registry_point_maps WHERE device_id=%s AND version=%s',(device[0],mapping.map_version))
        version=row(cur)
        if not version or version['sha256']!=mapping.map_sha256 or version['status']!='approved' or version['transport'] not in ('modbus_tcp','modbus_rtu'):
            refuse('modbus_map_unapproved')
        if not any(point['point_code']==mapping.point_id for point in version['content']['points']): refuse('modbus_point_missing')
        cur.execute('SELECT id,device_id,map_version,valid_from,valid_to,audit_event_id FROM waveforms.registry_map_assignments WHERE device_id=%s AND map_version=%s ORDER BY valid_from LIMIT 65',(device[0],mapping.map_version))
        candidates=[]
        while (assignment:=row(cur)) is not None: candidates.append(assignment)
        if len(candidates)>64: refuse('modbus_assignment_unbounded')
        covering=[item for item in candidates if covers_observation(item['valid_from'],item['valid_to'],start,end,uncertainty)]
        if len(covering)!=1: refuse('modbus_assignment_interval')
        result.append(dict(role=binding.role,point_id=mapping.point_id,version=_json_record(version),assignment=_json_record(covering[0])))
    return result


def _json_record(value):
    from datetime import datetime
    if isinstance(value,datetime): return value.isoformat().replace('+00:00','Z')
    if isinstance(value,UUID): return str(value)
    if type(value) is float: return {'registry_number':repr(value)}
    if hasattr(value,'items'): return {k:_json_record(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [_json_record(v) for v in value]
    return value


def applicability_with_cursor(cur,request,bucket,observation,selected,source_ranges):
    start,end,u=(Fraction(observation[key]) for key in ('start_ns','end_ns','uncertainty_ns'))
    record,model,parameters,selection=approved_with_cursor(cur,request,bucket,start=start,end=end,uncertainty=u,
                                                        alignment=Fraction(observation['relative_alignment_ns']))
    conditions=operating_conditions(parameters,request.operating_condition_ids,start,end,u)
    archives=archived_channels(cur,parameters,selected,bucket=bucket)
    maps=modbus_maps(cur,parameters,start,end,u)
    registry=public(resolve_with_cursor(cur,tuple(capture_key(item) for item in observation['sources']),
        utc_start=observation['utc_start'],start_ns=Fraction(observation['relative_start_ns']),
        end_ns=Fraction(observation['relative_end_ns']),uncertainty_ns=u))
    if registry['status']!='resolved' and parameters.source_kind!='synthetic': refuse('registry_ineligible')
    cur.execute('SELECT a.id,a.well_id,a.valid_from,s.valid_to,s.version,a.content_sha256 FROM inversion.assignments a JOIN inversion.assignment_state s ON s.assignment_id=a.id WHERE a.assembly_id=%s ORDER BY a.valid_from LIMIT 65',(request.assembly_id,))
    candidates=cur.fetchall()
    if len(candidates)>64: refuse('assignment_unbounded')
    assignments=[a for a in candidates if covers_observation(a[2],a[3],start,end,u)]
    if len(assignments)!=1: refuse('assignment_interval')
    assignment=assignments[0]
    if parameters.source_kind=='synthetic':
        if not assignment[1].startswith('test:'): refuse('source_ineligible')
    elif not registry['well'] or assignment[1]!=registry['well']['id']: refuse('assignment_well_mismatch')
    cur.execute('SELECT v.id,s.revision,s.valid_from,s.valid_to FROM inversion.assembly_versions v JOIN inversion.mechanical_state s ON s.version_id=v.id WHERE v.assembly_id=%s AND v.version=%s',(request.assembly_id,parameters.assembly_version))
    mechanical=cur.fetchone()
    independence=calibration_independence(parameters,archives,observation['physical_cycle_key'],start,end,u,source_ranges)
    return parameters,dict(parameter_body=public(record.body),parameter_reviews=public(record.reviews),
        model_body=public(model.body),model_reviews=public(model.reviews),selection=selection,
        operating_conditions=[public(record.body['provenance']['operating_conditions'][next(i for i,c in enumerate(parameters.provenance.operating_conditions) if c.id==condition.id)]) for condition in conditions],
        archived_channels=archives,modbus_maps=maps,registry=registry,
        assignment=_json_record(dict(zip(('id','well_id','valid_from','valid_to','version','content_sha256'),assignment))),
        mechanical=_json_record(dict(zip(('id','revision','valid_from','valid_to'),mechanical))),
        independence_reasons=list(independence),independent_comparison=not independence,
        external_evidence_admission_required=True,field_accuracy_certified=False)


def observation_applicability(value,observation):
    """Compare admitted facts over exact observation support, retaining history.

    Only effective closure endpoints and their closure counters are normalized.
    Stable period identities, starts, immutable content, review pins, registry
    state, map versions and all other facts remain part of equality. Each side
    must itself cover the complete uncertain support before normalization.
    """
    import json
    from datetime import datetime
    result=json.loads(canonical_bytes(value))
    start,end,u=(Fraction(observation[key]) for key in ('start_ns','end_ns','uncertainty_ns'))
    def interval(record,counter=None):
        if record is None: return
        # Stored DB projections use isoformat(), which may omit zero fractions;
        # these are server-owned snapshots, not the stricter request UTC wire.
        lower=datetime.fromisoformat(record['valid_from'])
        upper=datetime.fromisoformat(record['valid_to']) if record['valid_to'] else None
        if covers_observation(lower,upper,start,end,u):
            record.pop('valid_to')
            if counter: record.pop(counter,None)
    interval(result.get('assignment'),'version')
    interval(result.get('mechanical'),'revision')
    for mapping in result.get('modbus_maps',()): interval(mapping['assignment'])
    registry=result.get('registry')
    if registry is not None:
        registry.pop('revision',None)
        for source in registry.get('sources',()):
            for key in ('states','bindings'):
                for period in source.get(key,()): interval(period)
    return result


def recheck_with_cursor(cur,snapshot) -> tuple[str,...]:
    """Caller holds waveform → registry → inversion, then any payer/capacity.

    Lock the entire selected (<=2) + archived (<=4) capture union once. The
    owner function sorts gateway/device/capture, matching intake's capture lock.
    Never acquire a waveform payer after these rows: intake takes payer first.
    All mutable capture metadata below is read only after this same-cursor lock.
    """
    from .series import ChannelPin
    body=snapshot.body
    captures={UUID(item['descriptor']['capture_pk']) for item in body['captures']}
    captures.update(UUID(item['capture_pk']) for item in body['applicability']['archived_channels'])
    if not 1<=len(captures)<=6: refuse('capture_union_limit')
    cur.execute('SELECT waveforms.lock_business_captures(%s)',(sorted(captures,key=str),))
    # InputSnapshot verifies/fixes its canonical immutable body before locks.
    request=parse_run_request(canonical_bytes(body['request'])); reasons=[]
    cur.execute('SELECT content_sha256,purpose FROM waveforms.dataset_versions WHERE version_id=%s',(request.dataset_version_id,))
    if cur.fetchone()!=(request.dataset_sha256,body['purpose']): reasons.append('dataset_changed')
    cur.execute('SELECT kind,reference_id,snapshot FROM waveforms.dataset_members WHERE version_id=%s AND ordinal=%s',(request.dataset_version_id,request.member_ordinal))
    member=cur.fetchone()
    if member is None or member[0]!='pair' or member[1]!=request.pair_id or canonical_bytes(member[2])!=canonical_bytes(body['member_snapshot']):
        reasons.append('member_changed')
    cur.execute('SELECT version,decision FROM waveforms.pair_reviews WHERE pair_version_id=%s ORDER BY version DESC LIMIT 1',(request.pair_id,))
    if cur.fetchone()!=(request.pair_review_version,'approve'): reasons.append('pair_review_changed')
    for evidence in body['evidence']:
        cur.execute('SELECT content_sha256,body FROM waveforms.evidence_versions WHERE evidence_id=%s',(UUID(evidence['id']),)); saved=cur.fetchone()
        cur.execute('SELECT version,state FROM waveforms.evidence_events WHERE evidence_id=%s ORDER BY version DESC LIMIT 1',(UUID(evidence['id']),))
        if (saved is None or saved[0]!=evidence['content_sha256'] or canonical_bytes(saved[1])!=canonical_bytes(evidence['body'])
            or cur.fetchone()!=(evidence['version'],'approved')): reasons.append('evidence_changed')
    for chosen in body['selection']['captures']:
        key=capture_key(chosen['key'])
        cur.execute('SELECT manifest_sha256,state,completion_status,restricted FROM waveforms.archive_channel_metadata WHERE gateway_id=%s AND device_id=%s AND capture_id=%s LIMIT 1',(key.gateway_id,key.device_id,key.capture_id))
        if cur.fetchone()!=(chosen['manifest_sha256'],'complete','complete',False): reasons.append('capture_changed')
    try:
        _,current=applicability_with_cursor(cur,request,body['accounting_bucket'],body['observation'],
            {key:ChannelPin.from_wire(strict_json(canonical_bytes(value))) for key,value in body['channel_pins'].items()},body['source_ranges'])
        old=observation_applicability(body['applicability'],body['observation'])
        current=observation_applicability(current,body['observation'])
        if canonical_bytes(current)!=canonical_bytes(old): reasons.append('applicability_changed')
    except InversionError as error:
        reasons.append(error.code.removeprefix('inversion.'))
    return tuple(sorted(set(reasons)))
