"""Precise engineering display, bounded by channels, output and accepted chunks."""
from dataclasses import dataclass
from fractions import Fraction
import math

from .timing import sample_time_ns
from .errors import WaveformError
from .routes import invalid
from .contracts import CaptureKey,ManifestFrame,ChunkFrame
from .commands import Actor
from .wire import parse_frame
from .samples import iter_samples
from .evidence_contracts import decimal_value
from .registry_context import contains_window,datetime_ns
from datetime import datetime
from hashlib import sha256
import json
import time


def finite_float(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (OverflowError,ValueError):
        return None


def point_time(stream,index,correction):
    return str(sample_time_ns(stream,index,correction_ns=correction or 0))


@dataclass(frozen=True)
class WindowPoint:
    index:str
    relative_ns:str
    value:float|None
    quality:int
    raw_index:str


@dataclass(frozen=True)
class WindowGap:
    first_index:str
    last_index:str
    reasons:tuple


@dataclass(frozen=True)
class DisplaySample:
    index:int
    values:tuple|None
    reasons:tuple=()
    payload:object=None


@dataclass(frozen=True)
class Summary:
    points:tuple
    gaps:tuple
    status:str


class Envelope:
    def __init__(self,sample):
        self.first=self.last=sample
        self.low=[sample]*len(sample.values); self.high=self.low.copy()

    def add(self,sample):
        self.last=sample
        for n,value in enumerate(sample.values):
            if value<self.low[n].values[n]: self.low[n]=sample
            if value>self.high[n].values[n]: self.high[n]=sample

    def points(self):
        return tuple(sorted({p.index:p for p in (self.first,self.last,*self.low,*self.high)}.values(),key=lambda p:p.index))


class SummaryState:
    """Two passes; first bounded segment envelopes, second reserved buckets."""
    def __init__(self,budget):
        if type(budget) is not int or not 1<=budget<=2000: raise invalid()
        self.budget=budget; self.segments=[]; self.gaps=[]; self.status='ok'
        self.essential=0; self.current=None; self.previous=None
        self.output=[]; self.bucket=None; self.bucket_key=None; self.position=0

    def gap(self,first,last,reasons):
        if self.gaps and int(self.gaps[-1].last_index)+1==first:
            old=self.gaps[-1]
            self.gaps[-1]=WindowGap(old.first_index,str(last),tuple(sorted(set(old.reasons)|set(reasons))))
        else: self.gaps.append(WindowGap(str(first),str(last),tuple(sorted(set(reasons)))))
        self.current=None
        if len(self.gaps)>self.budget+1: self.fail('gap_overflow')

    def fail(self,status):
        self.status=status; self.segments.clear(); self.gaps.clear(); self.output.clear(); self.current=None

    def feed(self,sample):
        if self.status!='ok': return
        if self.previous is not None and sample.index!=self.previous+1: raise ValueError('Consecutive source positions required')
        self.previous=sample.index
        if sample.values is None:
            self.gap(sample.index,sample.index,sample.reasons); return
        if self.current is None:
            self.current=[sample.index,1,Envelope(sample),1,0]
            self.segments.append(self.current); self.essential+=1
        else:
            if self.current[1]==1: self.essential+=1
            self.current[1]+=1; self.current[2].add(sample)
        if self.essential>self.budget: self.fail('gap_overflow')

    def plan(self):
        if self.status!='ok': return
        for s in self.segments: s[4]=len(s[2].points())
        cost=sum(s[4] for s in self.segments)
        if cost>self.budget: self.fail('budget_insufficient'); return
        while True:
            selected=None; replacement=0
            for s in self.segments:
                width=2+2*len(s[2].first.values)
                new=min(s[1],width*(s[3]+1))
                if s[3]>=s[1] or cost-s[4]+new>self.budget: continue
                if selected is None or s[1]*selected[3]>selected[1]*s[3]: selected,replacement=s,new
            if selected is None: break
            cost+=replacement-selected[4]; selected[4]=replacement; selected[3]+=1

    def replay(self,sample):
        if self.status!='ok' or sample.values is None: return
        while self.position<len(self.segments) and sample.index>=self.segments[self.position][0]+self.segments[self.position][1]:
            self.position+=1
        if self.position==len(self.segments): raise ValueError('Summary support changed')
        s=self.segments[self.position]; offset=sample.index-s[0]
        if offset<0: raise ValueError('Summary support changed')
        # Inverse of floor(j*L/k) boundaries, including unequal bucket lengths.
        key=(self.position,((offset+1)*s[3]-1)//s[1])
        if key!=self.bucket_key:
            if self.bucket is not None: self.output.extend(self.bucket.points())
            self.bucket=Envelope(sample); self.bucket_key=key
        else: self.bucket.add(sample)

    def finish(self):
        if self.bucket is not None:
            self.output.extend(self.bucket.points()); self.bucket=None
        if len(self.output)>self.budget: raise WaveformError('waveform.window_integrity','Summary budget invariant failed')
        return Summary(tuple(self.output),tuple(self.gaps),self.status)


def select_summary(source,budget):
    state=SummaryState(budget)
    for sample in source(): state.feed(sample)
    state.plan()
    if state.status=='ok':
        for sample in source(): state.replay(sample)
    return state.finish()


@dataclass(frozen=True)
class EvidenceIdentity:
    id:str
    content_sha256:str
    version:str
    review_state:str


@dataclass(frozen=True)
class ChannelMetadata:
    channel_id:str
    quantity:str
    unit:str
    encoding:str
    measurement_source:str
    signal_kind:str
    reported_config_id:str|None
    calibration:EvidenceIdentity|None
    sampling_config:EvidenceIdentity|None


@dataclass(frozen=True)
class TimeBasis:
    reference_capture:CaptureKey
    reference:str
    unit:str
    utc_start:str|None
    utc_uncertainty_ns:str|None
    coordinate:str
    time_correction_ns:str|None


@dataclass(frozen=True)
class ChannelWindow:
    metadata:ChannelMetadata
    points:tuple
    gaps:tuple
    mode:str
    total_frames:int
    calibration_id:str|None
    time_basis:TimeBasis
    status:str
    restrictions:tuple
    suggested_count:int|None


@dataclass(frozen=True)
class SourceIdentity:
    gateway_id:str
    device_id:str
    boot_id:str


@dataclass(frozen=True)
class WindowResult:
    key:CaptureKey
    manifest_sha256:str
    stream_id:str
    start:str
    count:int
    mode:str
    budget:int
    total_frames:int
    channels:tuple
    support_revision:str
    restrictions:tuple
    source:SourceIdentity
    source_kind:str
    verification:str='selected_chunks'


def stale():
    raise WaveformError('waveform.stale_dependency','Window dependencies changed; request again',409)


def integrity():
    raise WaveformError('waveform.window_integrity','Accepted source differs from immutable manifest')


def identity(item):
    return EvidenceIdentity(str(item.id),item.content_sha256,str(item.version),item.review_state) if item else None


def validate_request(start,count,mode,budget):
    if (type(start) is not int or not -2**63<=start<2**63 or type(count) is not int
            or not 1<=count<=2147483647 or mode not in ('raw','summary') or type(budget) is not int
            or mode=='raw' and (count>20000 or budget!=20000)
            or mode=='summary' and not 1<=budget<=2000): raise invalid()


class Calibration:
    """Pinned affine conversion; review restrictions never rewrite source flags."""
    def __init__(self,frame,stream,channel,evidence,first,last):
        self.channel=channel; self.stream=stream; self.frame=frame
        self.reasons=set(); self.scale=self.offset=self.correction=None; self.range=None
        self.calibration=evidence.get(str(channel.calibration_id))
        cal=self.calibration
        self.config=evidence.get(cal.body['sampling_config_version_id']) if cal and cal.kind=='calibration' else None
        config=self.config
        if cal is None or cal.kind!='calibration': self.reasons.add('unknown_calibration'); return
        b=cal.body; c=config.body if config and config.kind=='sampling_config' else None
        endpoint=dict(gateway_id=stream.source['gateway_id'],device_id=stream.source['device_id'],stream_id=stream.stream_id,channel_id=channel.channel_id)
        if (c is None or config.content_sha256!=b['sampling_config_sha256'] or str(config.id)!=b['sampling_config_version_id']
                or dict(c['endpoint'])!=endpoint or dict(b['endpoint'])!=endpoint or channel.config_id is None
                or c['reported_config_id']!=channel.config_id or b['unit']!=channel.unit
                or any(c[k]!=getattr(channel,k) for k in ('quantity','unit','encoding','measurement_source','signal_kind'))
                or c['rate'] is None or dict(c['rate'])!={'num':stream.rate_num,'den':stream.rate_den}
                or c['source_kind']!=frame.body['source_kind'] or b['source_kind']!=frame.body['source_kind']):
            self.reasons.add('config_mismatch'); return
        if any(b[k] is None for k in ('scale','offset','time_correction_ns')):
            self.reasons.add('unknown_calibration'); return
        self.scale=decimal_value(b['scale']); self.offset=decimal_value(b['offset']); self.correction=int(b['time_correction_ns'])
        self.range=b['calibrated_range']
        if self.range and self.range['unit']!=channel.unit: self.reasons.add('unknown_units')
        if any(b[k] is None or b[k]=='' for k in ('calibrated_range','measurement_uncertainty','method','basis','direction','residual_ns')):
            self.reasons.add('unknown_calibration')
        for item in (config,cal):
            reason=self.validity(item,first,last)
            if reason: self.reasons.add(reason)

    def validity(self,item,first,last):
        if item.review_state in ('rejected','revoked'): return 'calibration_expired_or_revoked'
        if item.review_state!='approved': return 'evidence_unavailable'
        b=item.body
        if b['valid_from'] is None and b['valid_to'] is None: return None
        anchor=self.frame.body['utc_start']; uncertainty=self.frame.body['utc_uncertainty_ns']
        if anchor is None or uncertainty is None or self.correction is None: return 'evidence_unavailable'
        def ns(value): return datetime_ns(datetime.fromisoformat(value.replace('Z','+00:00')))
        lower=ns(anchor)+sample_time_ns(self.stream,first,correction_ns=self.correction)-int(uncertainty)
        upper=ns(anchor)+sample_time_ns(self.stream,last,correction_ns=self.correction)+int(uncertainty)
        return None if contains_window(ns(b['valid_from']) if b['valid_from'] else -10**30,ns(b['valid_to']) if b['valid_to'] else None,lower,upper) else 'calibration_expired_or_revoked'

    def value(self,raw,quality):
        if quality: return None,('missing_sample','clipped','bad','uncertain')[quality-1]
        if self.scale is None: return None,'config_mismatch' if 'config_mismatch' in self.reasons else 'unknown_calibration'
        value=Fraction(raw)*self.scale+self.offset
        if finite_float(value) is None:
            self.reasons.add('engineering_nonfinite'); return None,'engineering_nonfinite'
        if self.range and (value<decimal_value(self.range['min']) or value>decimal_value(self.range['max'])): self.reasons.add('out_of_range')
        return value,None

    def metadata(self):
        c=self.channel
        return ChannelMetadata(c.channel_id,c.quantity,c.unit,c.encoding,c.measurement_source,c.signal_kind,c.config_id,identity(self.calibration),identity(self.config))

    def time_basis(self):
        return TimeBasis(self.frame.key,'capture_start','ns',self.frame.body['utc_start'],self.frame.body['utc_uncertainty_ns'],
                         'corrected' if self.correction is not None else 'nominal',str(self.correction) if self.correction is not None else None)


class WaveformWindows:
    def __init__(self,queries,*,actor,check=None):
        if type(actor) is not Actor: raise ValueError('Trusted Actor required')
        self.queries=queries; self.actor=actor; self._check=check
        self.deadline=time.monotonic()+120

    def check(self):
        if self._check: self._check()
        if time.monotonic()>=self.deadline: raise WaveformError('waveform.window_timeout','Window computation deadline exceeded')

    def manifest(self,key,*,summary=False):
        self.check(); pin=self.queries.window_snapshot(key,summary=summary)
        fact=pin['capture']['manifest_fact_id']
        if not fact: return None,pin
        payload,_=self.queries.raw(fact,self.actor); self.check(); frame=parse_frame(payload)
        if type(frame) is not ManifestFrame or frame.key!=key or frame.raw_sha256!=pin['capture']['manifest_sha256']: integrity()
        return frame,pin

    def recheck(self,key,pin,**kwargs):
        self.check(); fresh=self.queries.window_snapshot(key,**kwargs)
        # Detail contains time-derived stalled only; compare the durable inputs.
        if any(fresh[k]!=pin[k] for k in ('capture','completion','restrictions','slots','evidence')): stale()

    def prepare(self,key,stream_id,start,count,channel_ids):
        if type(key) is not CaptureKey or channel_ids is not None and (type(channel_ids) is not tuple or not channel_ids or len(set(channel_ids))!=len(channel_ids)): raise invalid()
        frame,initial=self.manifest(key)
        if frame is None: raise WaveformError('waveform.manifest_unavailable','Accepted manifest is unavailable',409)
        stream=next((s for s in frame.streams if s.stream_id==stream_id),None)
        if stream is None: raise invalid('Unknown stream')
        channel_ids=tuple(c.channel_id for c in stream.channels) if channel_ids is None else channel_ids
        channels=tuple(c for c in stream.channels if c.channel_id in channel_ids)
        if len(channels)!=len(channel_ids) or not stream.first_sample_index<=start<start+count<=stream.first_sample_index+stream.frame_count: raise invalid('Window outside declared support')
        lo=(start-stream.first_sample_index)//stream.chunk_frames; hi=(start+count-1-stream.first_sample_index)//stream.chunk_frames
        params=dict(ranges=((stream_id,lo,hi),),evidence_ids=tuple(c.calibration_id for c in channels if c.calibration_id))
        pin=self.queries.window_snapshot(key,**params)
        if any(initial[k]!=pin[k] for k in ('capture','completion','restrictions')): stale()
        return frame,stream,channels,pin,params

    def samples(self,frame,stream,pin,start,count):
        slots={(s,i):fact for s,i,fact in pin['slots']}
        lo=(start-stream.first_sample_index)//stream.chunk_frames; hi=(start+count-1-stream.first_sample_index)//stream.chunk_frames
        for index in range(lo,hi+1):
            self.check(); fact=slots.get((stream.stream_id,index))
            relative=index*stream.chunk_frames; size=min(stream.chunk_frames,stream.frame_count-relative)
            if fact is None:
                if pin['capture']['state']=='complete' or pin['completion'] and pin['completion']['status']=='complete': integrity()
                for original in range(max(start,stream.first_sample_index+relative),min(start+count,stream.first_sample_index+relative+size)):
                    if original%256==0: self.check()
                    yield original,None
                continue
            payload,_=self.queries.raw(fact,self.actor); self.check(); chunk=parse_frame(payload); del payload
            if (type(chunk) is not ChunkFrame or chunk.key!=frame.key or chunk.manifest_sha256!=frame.raw_sha256
                    or chunk.stream_id!=stream.stream_id or chunk.chunk_index!=index or chunk.sample_start!=relative or chunk.sample_count!=size
                    or len(chunk.data)!=size*stream.frame_bytes): integrity()
            for sample in iter_samples(stream,chunk.data,sample_start=chunk.sample_start):
                if sample.index%256==0: self.check()
                if start<=sample.index<start+count: yield sample.index,sample

    def read(self,key,stream_id,*,start,count,channel_ids,mode,budget):
        if type(channel_ids) is not tuple or not channel_ids: raise invalid()
        return self._read(key,stream_id,start=start,count=count,channel_ids=channel_ids,mode=mode,budget=budget)

    def read_http(self,id,stream_id,*,start,count,mode,budget=20000):
        return self._read(self.queries.capture_key(id),stream_id,start=start,count=count,mode=mode,budget=budget,channel_ids=None)

    def describe(self,id):
        from .queries import public
        key=self.queries.capture_key(id); frame,pin=self.manifest(key,summary=True)
        value=pin['detail']; streams=[]
        if frame:
            for s in frame.streams:
                streams.append(dict(stream_id=s.stream_id,source=s.source,clock_domain_id=s.clock_domain_id,rate=dict(num=s.rate_num,den=s.rate_den),
                    first_sample_index=str(s.first_sample_index),frame_count=s.frame_count,chunk_frames=s.chunk_frames,chunk_count=s.chunk_count,
                    offset_ns=str(s.offset_ns),channels=tuple(dict(channel_id=c.channel_id,quantity=c.quantity,unit=c.unit,encoding=c.encoding,
                        measurement_source=c.measurement_source,signal_kind=c.signal_kind,config_id=c.config_id,
                        calibration_id=str(c.calibration_id) if c.calibration_id else None) for c in s.channels)))
            value.update(source_kind=frame.body['source_kind'],utc_start=frame.body['utc_start'],utc_uncertainty_ns=frame.body['utc_uncertainty_ns'],cycle=frame.body['cycle'])
        value.update(streams=tuple(streams),restrictions=('manifest_unavailable',) if frame is None else ())
        self.recheck(key,pin,summary=True)
        return public(value)

    def _read(self,key,stream_id,*,start,count,channel_ids,mode,budget):
        validate_request(start,count,mode,budget)
        frame,stream,channels,pin,params=self.prepare(key,stream_id,start,count,channel_ids)
        calibrations={c.channel_id:Calibration(frame,stream,c,pin['evidence'],start,start+count-1) for c in channels}
        states={c.channel_id:SummaryState(budget) if mode=='summary' else None for c in channels}
        points={c.channel_id:[] for c in channels}; gaps={c.channel_id:[] for c in channels}
        def raw_gap(id,index,reasons):
            values=gaps[id]
            if values and int(values[-1].last_index)+1==index:
                old=values[-1]; values[-1]=WindowGap(old.first_index,str(index),tuple(sorted(set(old.reasons)|set(reasons))))
            else: values.append(WindowGap(str(index),str(index),reasons))
        for pass_number in range(2 if mode=='summary' else 1):
            if pass_number:
                for state in states.values(): state.plan()
                if all(s.status!='ok' for s in states.values()): break
            for index,sample in self.samples(frame,stream,pin,start,count):
                ids=calibrations if sample is None else (sample.channel_id,) if sample.channel_id in calibrations else ()
                for id in ids:
                    cal=calibrations[id]; state=states[id]
                    if state and state.status!='ok': continue
                    value,reason=(None,'missing_chunk') if sample is None else cal.value(sample.raw_value,sample.quality)
                    reasons=(reason,) if reason else ()
                    if mode=='summary':
                        display=DisplaySample(index,(value,) if value is not None else None,reasons)
                        (state.replay if pass_number else state.feed)(display)
                    else:
                        if reason: raw_gap(id,index,reasons)
                        if sample is not None:
                            points[id].append(WindowPoint(str(index),point_time(stream,index,cal.correction),finite_float(value) if value is not None else None,sample.quality,str(index)))
        result=[]
        for c in channels:
            cal=calibrations[c.channel_id]; reasons=set(cal.reasons); status='ok'
            if reasons: reasons.add('historical_only')
            if mode=='summary':
                selected=states[c.channel_id].finish(); status=selected.status; gaps[c.channel_id]=selected.gaps
                points[c.channel_id]=tuple(WindowPoint(str(p.index),point_time(stream,p.index,cal.correction),finite_float(p.values[0]),0,str(p.index)) for p in selected.points)
            result.append(ChannelWindow(cal.metadata(),tuple(points[c.channel_id]),tuple(gaps[c.channel_id]),mode,count,
                str(c.calibration_id) if c.calibration_id else None,cal.time_basis(),status,tuple(sorted(reasons)),max(1,count//2) if status!='ok' else None))
        self.recheck(key,pin,**params)
        restrictions=tuple(['historical_only','capture_conflict'] if pin['restrictions'] or pin['capture']['state']=='invalid' else ['incomplete'] if pin['capture']['state']!='complete' else [])
        digest=sha256(repr((pin['capture'],pin['completion'],pin['slots'],pin['evidence'],pin['restrictions'])).encode()).hexdigest()
        return WindowResult(key,frame.raw_sha256,stream_id,str(start),count,mode,budget,count,tuple(result),digest,restrictions,
                            SourceIdentity(**stream.source),frame.body['source_kind'])
