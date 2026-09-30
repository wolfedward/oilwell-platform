"""Display-only XY over immutable full-quality anchors and bounded cursors.

Two monotonic cursors hold at most two decoded chunks. Each fact is read at most
twice per pass (shared stream), hence raw<=2 and summary<=4, never per point.
"""
from dataclasses import dataclass,field
from fractions import Fraction
from math import floor,ceil
from uuid import UUID

from .contracts import CaptureKey
from .pair_contracts import capture_key,selection_hash,METHOD_PARAMETERS
from .evidence_contracts import thaw
from .quality import VALIDATOR_VERSION
from .timing import align_pair,sample_time_ns,METHOD_VERSION
from .quality_context import utc_ns
from .windows import (Calibration,DisplaySample,WindowGap,SummaryState,SourceIdentity,finite_float,
                      validate_request,stale,integrity)
from .routes import invalid


@dataclass
class SparseChannel:
    key:CaptureKey
    stream:object
    channel:object
    first_index:int
    last_index:int
    scale:Fraction
    offset:Fraction
    correction_ns:int
    anchor_shift_ns:Fraction
    samples:dict=field(default_factory=dict)

    def time(self,index):
        return sample_time_ns(self.stream,index,correction_ns=self.correction_ns)+self.anchor_shift_ns

    def sample(self,index):
        if not self.first_index<=index<=self.last_index or index not in self.samples:
            raise ValueError('Unprepared original sample')
        return self.samples[index]

    def value(self,index):
        raw,quality=self.sample(index)
        return None if quality else Fraction(raw)*self.scale+self.offset


@dataclass(frozen=True)
class XYPoint:
    index:str
    relative_ns:str
    displacement:float
    load:float
    displacement_indices:tuple
    load_indices:tuple
    displacement_interpolated:bool
    load_interpolated:bool
    displacement_qualities:tuple
    load_qualities:tuple


@dataclass(frozen=True)
class XYReference:
    key:CaptureKey
    stream_id:str
    channel_id:str
    time_basis:object


@dataclass(frozen=True)
class XYSource:
    key:CaptureKey
    manifest_sha256:str
    stream_id:str
    channel_id:str
    source:SourceIdentity
    source_kind:str
    quantity:str
    unit:str
    calibration_id:str|None


def source_metadata(frame,stream,channel):
    return XYSource(frame.key,frame.raw_sha256,stream.stream_id,channel.channel_id,SourceIdentity(**stream.source),
                    frame.body['source_kind'],channel.quantity,channel.unit,str(channel.calibration_id) if channel.calibration_id else None)


@dataclass(frozen=True)
class XYWindowResult:
    pair_id:str
    pair_version:str
    method_version:str
    reference:XYReference|None
    points:tuple
    gaps:tuple
    mode:str
    total_frames:int
    budget:int
    status:str
    restrictions:tuple
    displacement_source:XYSource|None
    load_source:XYSource|None
    quality_scope:str='historical_full_selection_unchanged_dependencies'
    quantitative_use_allowed:bool=False
    closed:bool=False


def align_original(reference,load,index):
    aligned=align_pair((reference,reference,load),reference_times=(reference.time(index),))
    left,right=aligned.left[0],aligned.right[0]
    if left.gap or right.gap: return None
    x,y=finite_float(left.value),finite_float(right.value)
    if x is None or y is None: return None
    return XYPoint(str(aligned.reference_indices[0]),str(aligned.reference_times[0]),x,y,
        tuple(str(i) for i in left.indices),tuple(str(i) for i in right.indices),left.interpolated,right.interpolated,left.qualities,right.qualities)


def references(selection):
    refs=[selection['profile'],selection['sync']]
    refs.extend(r for c in selection['captures'] for r in c['origins'])
    refs.extend(r for c in selection['channels'] for r in (c['sampling_config'],c['calibration']))
    return {r['id']:r for r in refs if r is not None}


def historical_gate(snapshot,selection):
    pair=snapshot['pair']; reasons=[]; digest=selection_hash(selection)
    if pair.method_version!=METHOD_VERSION or thaw(pair.body['method_parameters'])!=METHOD_PARAMETERS: reasons.append('unsupported_method')
    if pair.body['selection_sha256']!=digest: integrity()
    if list(pair.body['quality_run_ids'])!=[str(pair.left_quality_run_id),str(pair.right_quality_run_id)]: integrity()
    if pair.review_state in ('reject','revoke'): reasons.append('pair_rejected' if pair.review_state=='reject' else 'pair_revoked')
    if pair.body['joint_result']['status']!='eligible' or selection['purpose']=='raw_archive': reasons.append('historical_quality_ineligible')
    expected={capture_key(c['key']):c for c in selection['captures']}; found=set(); refs=references(selection); profile=selection['profile'] or {}
    for run in snapshot['runs']:
        key=capture_key(run['source_key']); chosen=expected.get(key)
        if chosen is None or key in found: integrity()
        found.add(key)
        if (run['parameters']['selection']!=selection or run['parameters']['selection_sha256']!=digest
                or run['manifest_sha256']!=chosen['manifest_sha256'] or run['purpose']!=selection['purpose']
                or run['result']!=thaw(pair.body['joint_result'])
                or (str(run['profile_id']) if run['profile_id'] else None)!=profile.get('id') or run['profile_sha256']!=profile.get('content_sha256')): integrity()
        if run['validator_version']!=VALIDATOR_VERSION: reasons.append('unsupported_validator')
        if run['status']!='eligible': reasons.append('historical_quality_ineligible')
        old={v['id']:v for v in run['evidence']}
        if set(old)!=set(refs) or any(old[id]['content_sha256']!=r['content_sha256'] or old[id]['version']!=r['version'] or old[id]['review_state']!='approved' for id,r in refs.items()): integrity()
        registry=run['registry_snapshot']
        if registry is None or registry['revision']!=snapshot['registry_revision']: reasons.append('current_quality_recheck_required')
    if found!=set(expected): integrity()
    return reasons


def _channel_samples(windows,frame,stream,pin,channel_id,first,last):
    for index,sample in windows.samples(frame,stream,pin,first,last-first+1):
        if sample is None or sample.channel_id==channel_id: yield index,sample


class AdjacentCursor:
    """Retain only the current adjacent pair; report skipped unavailable support."""
    def __init__(self,source): self.source=iter(source); self.current=None; self.next=next(self.source,None)

    def take(self,indices):
        data={}; skipped=set()
        while self.next is not None and self.next[0]<=indices[-1]:
            self.current=self.next; index,sample=self.current
            if sample is None: skipped.add('missing_chunk')
            elif sample.quality: skipped.add(('missing_sample','clipped','bad','uncertain')[sample.quality-1])
            if index in indices: data[index]=sample
            self.next=next(self.source,None)
        if self.current and self.current[0] in indices: data[self.current[0]]=self.current[1]
        # Previous adjacent sample may be needed again by a faster reference.
        for index,sample in getattr(self,'previous',{}).items():
            if index in indices: data.setdefault(index,sample)
        self.previous=data
        return data,tuple(sorted(skipped))


def xy_samples(windows,reference,target,ref_source,target_source):
    cursor=AdjacentCursor(target_source)
    period=Fraction(1000000000*target.stream.rate_den,target.stream.rate_num)
    for index,sample in ref_source:
        windows.check()
        if sample is None:
            yield DisplaySample(index,None,('missing_chunk',)); continue
        if sample.quality:
            yield DisplaySample(index,None,(('missing_sample','clipped','bad','uncertain')[sample.quality-1],)); continue
        position=(reference.time(index)-target.time(target.first_index))/period+target.first_index
        low=floor(position); indices=(low,) if position.denominator==1 else (low,low+1)
        if indices[0]<target.first_index or indices[-1]>target.last_index:
            yield DisplaySample(index,None,('outside_support',)); continue
        data,skipped=cursor.take(indices)
        if any(i not in data or data[i] is None for i in indices):
            yield DisplaySample(index,None,('missing_chunk',)); continue
        flags=tuple(data[i].quality for i in indices)
        if any(flags) or skipped:
            yield DisplaySample(index,None,skipped or ('non_good_sample',)); continue
        reference.samples={index:(sample.raw_value,sample.quality)}
        target.samples={i:(data[i].raw_value,data[i].quality) for i in indices}
        point=align_original(reference,target,index)
        if point is None:
            yield DisplaySample(index,None,('engineering_nonfinite',)); continue
        # Envelope comparisons use exact affine/interpolated values, not floats.
        values=tuple(target.value(i) for i in indices)
        y=values[0] if len(indices)==1 else values[0]+(values[1]-values[0])*(position-low)
        yield DisplaySample(index,(reference.value(index),y),(),point)


def read_pair(windows,id,*,start,count,mode,budget=20000):
    validate_request(start,count,mode,budget); windows.check()
    initial=windows.queries.pair_window_snapshot(id); pair=initial['pair']; selection=thaw(pair.body['selection'])
    reasons=historical_gate(initial,selection); reference_info=None; metadata=[]
    def result(points=(),gaps=(),status='restricted'):
        return XYWindowResult(str(id),str(pair.version),pair.method_version,reference_info,tuple(points),tuple(gaps),mode,count,budget,status,tuple(sorted(set(reasons))),
                              metadata[0] if metadata else None,metadata[1] if len(metadata)>1 else None)
    chosen={c['role']:c for c in selection['channels']}
    if not {'displacement','load'}<=chosen.keys(): reasons.append('reference_role_mismatch'); return result()
    ref_choice=chosen['displacement']; target_choice=chosen['load']
    if not int(ref_choice['first_index'])<=start<start+count<=int(ref_choice['last_index'])+1: raise invalid('XY window outside selected reference')
    refs=references(selection); sources={}; frames={}
    for entry in selection['captures']:
        key=capture_key(entry['key']); frame,pin=windows.manifest(key)
        if frame is None or frame.raw_sha256!=entry['manifest_sha256']: reasons.append('source_changed'); return result()
        frames[key]=frame
        final=windows.queries.window_snapshot(key,evidence_ids=tuple(refs))
        if any(run['capture_pk']!=final['capture']['capture_pk'] for run in initial['runs'] if capture_key(run['source_key'])==key): integrity()
        if any(pin[k]!=final[k] for k in ('capture','completion','restrictions')): stale()
        sources[key]=(final,dict(evidence_ids=tuple(refs)))
        if final['capture']['state']!='complete' or not final['completion'] or final['completion']['status']!='complete' or final['restrictions']:
            reasons.append('source_restricted')
        for identity,ref in refs.items():
            value=final['evidence'].get(identity)
            if value is None or value.content_sha256!=ref['content_sha256'] or str(value.version)!=ref['version'] or value.review_state!='approved': reasons.append('selected_evidence_changed')
    data=[]; supports=[]
    ref_key=capture_key(ref_choice['capture']); anchor=utc_ns(frames[ref_key])
    for entry in (ref_choice,target_choice):
        key=capture_key(entry['capture']); frame=frames[key]; pin,params=sources[key]
        stream=next((s for s in frame.streams if s.stream_id==entry['stream_id']),None)
        channel=next((c for c in stream.channels if c.channel_id==entry['channel_id']),None) if stream else None
        if channel is None or channel.quantity!=entry['role']: reasons.append('reference_role_mismatch'); return result()
        metadata.append(source_metadata(frame,stream,channel))
        first,last=int(entry['first_index']),int(entry['last_index'])
        if not stream.first_sample_index<=first<=last<stream.first_sample_index+stream.frame_count: integrity()
        cal=Calibration(frame,stream,channel,pin['evidence'],first,last)
        if (entry['calibration'] is None or str(channel.calibration_id)!=entry['calibration']['id'] or cal.config is None
                or entry['sampling_config'] is None or str(cal.config.id)!=entry['sampling_config']['id']): reasons.append('config_mismatch')
        reasons.extend(cal.reasons)
        shift=Fraction(0) if key==ref_key else None if anchor is None or utc_ns(frame) is None else Fraction(utc_ns(frame)-anchor)
        if shift is None or cal.correction is None: reasons.append('unknown_clock')
        data.append(SparseChannel(key,stream,channel,first,last,cal.scale,cal.offset,cal.correction,shift))
        supports.append((frame,stream,cal))
    reference,target=data
    reference_info=XYReference(ref_key,reference.stream.stream_id,reference.channel.channel_id,supports[0][2].time_basis())
    if reasons:
        windows.check(); windows.queries.recheck_pair(id,initial,sources)
        return result()
    # Select only target support covering requested corrected reference times.
    period=Fraction(1000000000*target.stream.rate_den,target.stream.rate_num)
    target_first=max(target.first_index,floor((reference.time(start)-target.time(target.first_index))/period)+target.first_index)
    target_last=min(target.last_index,ceil((reference.time(start+count-1)-target.time(target.first_index))/period)+target.first_index)
    intervals=((start,start+count-1),(target_first,target_last)); ranges={key:[] for key in sources}
    for channel,(first,last) in zip(data,intervals):
        if first<=last:
            s=channel.stream; span=(s.stream_id,(first-s.first_sample_index)//s.chunk_frames,(last-s.first_sample_index)//s.chunk_frames)
            if span not in ranges[channel.key]: ranges[channel.key].append(span)
    # Merge overlapping intervals of a shared physical stream before selecting slots.
    for key in sources:
        merged={}
        for stream,lo,hi in ranges[key]:
            old=merged.get(stream,(lo,hi)); merged[stream]=(min(old[0],lo),max(old[1],hi))
        old,params=sources[key]; params=dict(params,ranges=tuple((s,*r) for s,r in merged.items()))
        pin=windows.queries.window_snapshot(key,**params)
        if any(pin[k]!=old[k] for k in ('capture','completion','restrictions','evidence')): stale()
        sources[key]=(pin,params)
    def source():
        a=_channel_samples(windows,*supports[0][:2],sources[reference.key][0],reference.channel.channel_id,*intervals[0])
        b=_channel_samples(windows,*supports[1][:2],sources[target.key][0],target.channel.channel_id,*intervals[1]) if target_first<=target_last else iter(())
        try: yield from xy_samples(windows,reference,target,a,b)
        finally:
            if hasattr(a,'close'): a.close()
            if hasattr(b,'close'): b.close()
    points=[]; gaps=[]; status='ok'
    if mode=='summary':
        state=SummaryState(budget)
        for sample in source(): state.feed(sample)
        state.plan()
        if state.status=='ok':
            for sample in source(): state.replay(sample)
        selected=state.finish(); points=[p.payload for p in selected.points]; gaps=selected.gaps; status=selected.status
    else:
        for sample in source():
            if sample.values is not None: points.append(sample.payload)
            elif gaps and int(gaps[-1].last_index)+1==sample.index:
                old=gaps[-1]; gaps[-1]=WindowGap(old.first_index,str(sample.index),tuple(sorted(set(old.reasons)|set(sample.reasons))))
            else: gaps.append(WindowGap(str(sample.index),str(sample.index),sample.reasons))
    windows.check(); windows.queries.recheck_pair(id,initial,sources)
    if pair.review_state!='approve': reasons.append('pair_not_approved')
    return result(points,gaps,status)
