"""Exact nominal/physical coordinates and bounded provenance-preserving preview."""
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from math import floor
import struct

from .contracts import CaptureKey, ChannelSpec, StreamSpec
from .errors import ContractError

METHOD_VERSION = 'linear_adjacent/1'


def sample_time_ns(stream, index: int, *, correction_ns: int) -> Fraction:
    if (type(stream) is not StreamSpec or type(index) is not int or type(correction_ns) is not int
            or not stream.first_sample_index <= index < stream.first_sample_index + stream.frame_count):
        raise ContractError('contract_invalid')
    return Fraction(stream.offset_ns + correction_ns) + Fraction(
        (index-stream.first_sample_index)*1000000000*stream.rate_den, stream.rate_num)


@dataclass(frozen=True)
class ChannelData:
    """A channel view over shared packed stream bytes; no per-sample object array."""
    key: CaptureKey
    stream: StreamSpec
    channel: ChannelSpec
    data: bytes
    first_index: int
    last_index: int
    correction_ns: int | None = None
    anchor_shift_ns: Fraction | None = Fraction(0)
    role: str = ''
    scale: Fraction | None = Fraction(1)
    offset: Fraction | None = Fraction(0)

    def __post_init__(self):
        if (type(self.data) is not bytes or len(self.data)!=self.stream.decoded_bytes
                or not self.stream.first_sample_index <= self.first_index <= self.last_index
                < self.stream.first_sample_index+self.stream.frame_count or self.channel not in self.stream.channels):
            raise ContractError('contract_invalid')

    def sample(self, index):
        if type(index) is not int or not self.first_index<=index<=self.last_index:
            raise ContractError('contract_invalid')
        byte_offset=(index-self.stream.first_sample_index)*self.stream.frame_bytes+self.stream.channels.index(self.channel)*5
        raw,quality=struct.unpack_from('<iB' if self.channel.encoding=='i32le' else '<fB',self.data,byte_offset)
        return (None if quality==1 else raw),quality

    def value(self, index):
        raw,quality=self.sample(index)
        if quality or self.scale is None or self.offset is None: return None
        return Fraction(raw)*self.scale+self.offset

    def time(self, index):
        if self.correction_ns is None or self.anchor_shift_ns is None: return None
        return sample_time_ns(self.stream,index,correction_ns=self.correction_ns)+self.anchor_shift_ns


@dataclass(frozen=True)
class InterpolationPoint:
    time_ns: Fraction
    value: Fraction | None
    indices: tuple
    interpolated: bool
    qualities: tuple
    gap: str | None


def interpolate(channel: ChannelData, time_ns: Fraction) -> InterpolationPoint:
    if type(time_ns) not in (int,Fraction): raise ContractError('contract_invalid')
    time=Fraction(time_ns)
    def result(value=None,indices=(),interpolated=False,qualities=(),gap=None):
        return InterpolationPoint(time,value,indices,interpolated,qualities,gap)
    first,last=channel.time(channel.first_index),channel.time(channel.last_index)
    if first is None: return result(gap='unknown_clock')
    if time<first or time>last: return result(gap='outside_support')
    period=Fraction(1000000000*channel.stream.rate_den,channel.stream.rate_num)
    position=(time-first)/period+channel.first_index
    low=floor(position)
    indices=(low,) if position.denominator==1 else (low,low+1)
    qualities=tuple(channel.sample(i)[1] for i in indices)
    if any(qualities): return result(indices=indices,qualities=qualities,gap='non_good_sample')
    values=tuple(channel.value(i) for i in indices)
    if any(v is None for v in values): return result(indices=indices,qualities=qualities,gap='unknown_calibration')
    value=values[0] if len(indices)==1 else values[0]+(values[1]-values[0])*(position-low)
    return result(value,indices,len(indices)==2,qualities)


@dataclass(frozen=True)
class SyncBudget:
    endpoints: tuple
    channel_endpoints: tuple
    channel_residuals: tuple


def sync_bound_ns(context, duration_ns: int) -> Fraction | None:
    if type(duration_ns) not in (int,Fraction) or duration_ns<0: raise ContractError('contract_invalid')
    if context is None: return None
    if type(context) is not SyncBudget: raise ContractError('contract_invalid')
    if len(context.channel_endpoints)!=len(context.channel_residuals) or not context.channel_endpoints: return None
    if any(value is None for value in context.channel_residuals): return None
    rows={r['identity']:r for r in context.endpoints}
    def endpoint_bound(identity):
        row=rows.get(identity)
        if row is None or any(row.get(k) is None for k in ('reference_ns','start_ns','end_ns','offset_ns','residual_ns','ppm')):
            return None
        age=max(abs(row['start_ns']-row['reference_ns']),abs(row['end_ns']-row['reference_ns']))
        return Fraction(row['offset_ns']+row['residual_ns'])+age*row['ppm']/1000000
    maximum=Fraction(0)
    for a,b in combinations(range(len(context.channel_endpoints)),2):
        left,right=context.channel_endpoints[a],context.channel_endpoints[b]
        mapping=Fraction(0)
        if left!=right:
            values=(endpoint_bound(left),endpoint_bound(right))
            if None in values: return None
            mapping=sum(values)
        elif left not in rows: return None
        maximum=max(maximum,mapping+context.channel_residuals[a]+context.channel_residuals[b])
    return maximum


@dataclass(frozen=True)
class AlignmentPreview:
    reference_times: tuple
    reference_indices: tuple
    left: tuple
    right: tuple
    method_version: str = METHOD_VERSION


def align_pair(context, *, reference_times: tuple[Fraction,...]) -> AlignmentPreview:
    """Context is (reference sensor ChannelData, left ChannelData, right ChannelData)."""
    if (type(context) is not tuple or len(context)!=3 or type(reference_times) is not tuple
            or not 1<=len(reference_times)<=20000 or any(type(t) is not Fraction for t in reference_times)
            or any(a>=b for a,b in zip(reference_times,reference_times[1:]))):
        raise ContractError('contract_invalid')
    reference,left,right=context
    points=tuple(interpolate(reference,time) for time in reference_times)
    if any(p.gap or p.interpolated for p in points): raise ContractError('contract_invalid')
    return AlignmentPreview(reference_times,tuple(p.indices[0] for p in points),
        tuple(interpolate(left,t) for t in reference_times),tuple(interpolate(right,t) for t in reference_times))
