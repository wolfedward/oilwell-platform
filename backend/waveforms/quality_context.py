"""Controlled immutable input preparation and current dependency rechecks.

Factories must admit an operator and a reader connection simultaneously. The
history lock is cooperative: updated registry writers must be rolled out together.
"""
from dataclasses import asdict, replace
from contextlib import nullcontext
from datetime import datetime
from fractions import Fraction
from hashlib import sha256
from uuid import UUID

from psycopg.types.json import Jsonb

from . import business
from .capacity import prepare_transaction,row
from .contracts import CaptureKey,ManifestFrame,ChunkFrame
from .evidence import EvidenceService
from .evidence_contracts import canonical_bytes,decimal_value,sync_proof,thaw
from .errors import WaveformError,ContractError
from .pair_contracts import capture_key,key_dict,selection_hash
from .quality import QualityContext,ProfileVersion,channel_identity
from .registry_context import RegistryContext,datetime_ns
from .samples import iter_samples
from .timing import ChannelData,SyncBudget,sample_time_ns
from .wire import parse_frame

COMPLETION_COLUMNS='capture_pk,manifest_fact_id,status,reason_code,result,completed_at'


def unavailable(message): raise WaveformError('waveform.quality_unavailable',message)
def stale(message): raise WaveformError('waveform.stale_dependency',message,409)
def source_identity(source): return tuple(source[k] for k in ('gateway_id','device_id','boot_id'))
def stream_identity(key,stream): return (key,stream.stream_id)
def utc_ns(frame):
    text=frame.body['utc_start']
    return None if text is None else datetime_ns(datetime.fromisoformat(text.replace('Z','+00:00')))


class ContextBuilder(EvidenceService):
    """Reuse reviewed evidence access/identity; never call inspect under locks."""
    def _capture(self,cur,key):
        cur.execute('SELECT capture_pk,manifest_fact_id,manifest_sha256,state FROM waveforms.capture_metadata WHERE gateway_id=%s AND device_id=%s AND capture_id=%s',
                    (key.gateway_id,key.device_id,key.capture_id))
        return row(cur)

    def _completion(self,cur,id):
        if id is None: return None
        cur.execute('SELECT '+COMPLETION_COLUMNS+' FROM waveforms.completion WHERE capture_pk=%s',(id,))
        return row(cur)

    def _raw(self,fact,actor,*,connection=None):
        # A dataset preparation may lend its operation-local reader. Every fact
        # still has its own durable audit commit before any bytes leave here.
        with self._reader() if connection is None else nullcontext(connection) as conn:
            cur=conn.cursor(); prepare_transaction(cur)
            cur.execute('SELECT waveforms.audited_raw(%s,%s)',(fact,Jsonb(asdict(actor))))
            payload=bytes(cur.fetchone()[0]); prepare_transaction(cur); conn.commit()
        return payload

    def references(self,selection):
        refs=[selection['profile'],selection['sync']]
        refs.extend(r for c in selection['captures'] for r in c['origins'])
        refs.extend(r for c in selection['channels'] for r in (c['sampling_config'],c['calibration']))
        distinct={}
        for ref in refs:
            if ref is not None:
                if ref['id'] in distinct and distinct[ref['id']]!=ref: raise ContractError('contract_invalid')
                distinct[ref['id']]=ref
        return tuple(distinct.values())

    def read_evidence(self,cur,refs,*,strict):
        evidence={}; current=True
        for ref in refs:
            item=self._get(cur,UUID(ref['id']))
            if item.content_sha256!=ref['content_sha256']: stale('Selected evidence content differs')
            if item.version!=int(ref['version']):
                current=False
                if strict: stale('Selected evidence review revision changed')
            evidence[str(item.id)]=item
        return evidence,current

    def prepare(self,selection,actor,*,strict=True):
        refs=self.references(selection); captures={}; completions={}; manifests={}; buffers={}
        with self._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            cur=conn.cursor(); evidence,current=self.read_evidence(cur,refs,strict=strict)
            for entry in selection['captures']:
                key=capture_key(entry['key']); capture=self._capture(cur,key); captures[key]=capture
                completions[key]=self._completion(cur,capture['capture_pk'] if capture else None)
                actual=capture['manifest_sha256'] if capture else None
                if actual!=entry['manifest_sha256']: stale('Selected manifest identity differs')
        # Audit each fact separately, commit before parse/use/next source.
        for key,capture in captures.items():
            if not capture or not capture['manifest_fact_id']: continue
            frame=parse_frame(self._raw(capture['manifest_fact_id'],actor))
            if type(frame) is not ManifestFrame or frame.key!=key or frame.raw_sha256!=capture['manifest_sha256']:
                unavailable('Controlled manifest identity differs')
            manifests[key]=frame
            wanted={c['stream_id'] for c in selection['channels'] if capture_key(c['capture'])==key}
            if not wanted or not completions[key] or completions[key]['status']!='complete': continue
            with self._reader() as conn:
                conn.execute('SET TRANSACTION READ ONLY')
                slots=conn.execute('SELECT stream_id,chunk_index,fact_id FROM waveforms.chunk_slots WHERE capture_pk=%s ORDER BY stream_id,chunk_index LIMIT 513',
                                   (capture['capture_pk'],)).fetchall()
            if len(slots)>512: unavailable('Slot bound exceeded')
            slots={(s,i):fact for s,i,fact in slots}
            for stream in frame.streams:
                if stream.stream_id not in wanted: continue
                packed=bytearray(); digest=sha256()
                for index in range(stream.chunk_count):
                    fact=slots.get((stream.stream_id,index))
                    if fact is None: unavailable('Completed capture lacks selected slot')
                    chunk=parse_frame(self._raw(fact,actor))
                    count=min(stream.chunk_frames,stream.frame_count-index*stream.chunk_frames)
                    if (type(chunk) is not ChunkFrame or chunk.key!=key or chunk.manifest_sha256!=frame.raw_sha256
                            or chunk.stream_id!=stream.stream_id or chunk.chunk_index!=index
                            or chunk.sample_start!=index*stream.chunk_frames or chunk.sample_count!=count):
                        unavailable('Selected slot differs from immutable manifest')
                    for sample in iter_samples(stream,chunk.data,sample_start=chunk.sample_start): pass
                    packed.extend(chunk.data); digest.update(chunk.data)
                if len(packed)!=stream.decoded_bytes or digest.hexdigest()!=stream.data_sha256: unavailable('Selected stream digest differs')
                buffers[(key,stream.stream_id)]=bytes(packed)
                del packed
        prepared=dict(selection=selection,refs=refs,captures=captures,completions=completions,manifests=manifests,
                      buffers=buffers,evidence=evidence,current=current,statistics={})
        context,profile=self.context(prepared,evidence,{},(),registry=None)
        # Full selected data statistics are computed once, outside business locks.
        for data in context.streams:
            counts=[0]*5; minimum=maximum=cycle_minimum=cycle_maximum=None; cycle_count=0
            start,end=context.cycle.get('start_ns'),context.cycle.get('end_ns')
            for index in range(data.first_index,data.last_index+1):
                counts[data.sample(index)[1]]+=1; value=data.value(index)
                if value is not None:
                    minimum=value if minimum is None else min(minimum,value)
                    maximum=value if maximum is None else max(maximum,value)
                    time=data.time(index)
                    if start is not None and time is not None and start<=time<=end:
                        cycle_count+=1
                        cycle_minimum=value if cycle_minimum is None else min(cycle_minimum,value)
                        cycle_maximum=value if cycle_maximum is None else max(cycle_maximum,value)
            prepared['statistics'][channel_identity(data)]=dict(counts=tuple(counts),minimum=minimum,maximum=maximum,
                cycle_minimum=cycle_minimum,cycle_maximum=cycle_maximum,cycle_good_count=cycle_count)
        prepared['registry']=self.registry(context,prepared)
        return prepared

    def context(self,prepared,evidence,origins,restrictions,*,registry):
        selection=prepared['selection']; manifests=prepared['manifests']
        def item(ref,kind):
            value=evidence.get(ref['id']) if ref else None
            if value is not None and value.kind!=kind: raise ContractError('contract_invalid')
            return value
        profile=item(selection['profile'],'profile'); profile=ProfileVersion(profile) if profile else None
        reference_key=capture_key(selection['cycle']['capture']) if selection['cycle'] else capture_key(selection['captures'][0]['key'])
        reference=manifests.get(reference_key); reference_utc=utc_ns(reference) if reference else None
        channels=[]; calibrations={}; configs={}; support={}; unresolved=[]
        for chosen in selection['channels']:
            key=capture_key(chosen['capture']); frame=manifests.get(key)
            identity=(key,chosen['stream_id'],chosen['channel_id'])
            config=item(chosen['sampling_config'],'sampling_config'); calibration=item(chosen['calibration'],'calibration')
            calibrations[identity]=calibration; configs[identity]=config
            if frame is None:
                unresolved.append(chosen['role']); continue
            # Membership is an immutable-manifest fact, not a sample-availability fact.
            stream=next((s for s in frame.streams if s.stream_id==chosen['stream_id']),None)
            channel=next((c for c in stream.channels if c.channel_id==chosen['channel_id']),None) if stream else None
            if channel is None: raise ContractError('contract_invalid')
            if not stream.first_sample_index<=int(chosen['first_index'])<=int(chosen['last_index'])<stream.first_sample_index+stream.frame_count:
                raise ContractError('contract_invalid')
            if (key,chosen['stream_id']) not in prepared['buffers']:
                unresolved.append(chosen['role']); continue
            b=calibration.body if calibration else {}
            correction=int(b['time_correction_ns']) if b.get('time_correction_ns') is not None else None
            anchor=utc_ns(frame); shift=Fraction(0) if key==reference_key else None if anchor is None or reference_utc is None else Fraction(anchor-reference_utc)
            data=ChannelData(key,stream,channel,prepared['buffers'][(key,stream.stream_id)],int(chosen['first_index']),int(chosen['last_index']),
                correction,shift,chosen['role'],decimal_value(b['scale']) if b.get('scale') is not None else None,
                decimal_value(b['offset']) if b.get('offset') is not None else None)
            channels.append(data)
            uncertainty=frame.body['utc_uncertainty_ns']
            if anchor is not None and uncertainty is not None and correction is not None:
                support[identity]=(Fraction(anchor)+sample_time_ns(stream,data.first_index,correction_ns=correction)-int(uncertainty),
                                   Fraction(anchor)+sample_time_ns(stream,data.last_index,correction_ns=correction)+int(uncertainty))
        if support and len(support)==len(channels): support['joint']=(min(v[0] for v in support.values()),max(v[1] for v in support.values()))
        cycle=dict(purpose=selection['purpose'],status='pending',start_ns=None,turn_ns=None,end_ns=None,
                   absolute_support=support,physical_cycles=(),selection_sha256=selection_hash(selection),
                   selected_roles=tuple(c['role'] for c in selection['channels']),unresolved_roles=tuple(unresolved))
        if reference and selection['cycle'] and reference.body['cycle'] and reference.body['cycle']['cycle_id']==selection['cycle']['cycle_id']:
            declaration=reference.body['cycle']; events=declaration['reference_events']; times=[]; identities=[]
            for event,name in zip(events,('start_ns','turn_ns','end_ns')):
                data=next((d for d in channels if d.key==reference_key and d.stream.stream_id==event['stream_id'] and d.channel.channel_id==declaration['reference_channel']),None)
                if data is None or event['sample_index'] is None: break
                index=int(event['sample_index'])
                if not data.first_index<=index<=data.last_index or data.sample(index)[1] or data.time(index) is None: break
                if sample_time_ns(data.stream,index,correction_ns=0)!=int(declaration[name]): break
                times.append(data.time(index)); identities.append(dict(source=dict(data.stream.source),stream_id=data.stream.stream_id,channel_id=data.channel.channel_id,index=str(index)))
            if len(times)==3 and times[0]<times[1]<times[2] and declaration['direction']!='unknown' and declaration['zero_reference'].strip():
                cycle.update(status='resolved',start_ns=times[0],turn_ns=times[1],end_ns=times[2])
                physical=dict(capture=key_dict(reference_key),manifest_sha256=reference.raw_sha256,cycle_id=declaration['cycle_id'],events=identities,
                    support=[str(times[0]),str(times[2])],physical_cycle_key=sha256(canonical_bytes(identities)).hexdigest(),
                    source_intervals=[dict(source=dict(d.stream.source),capture=key_dict(d.key),stream_id=d.stream.stream_id,channel_id=d.channel.channel_id,
                        first_index=str(d.first_index),last_index=str(d.last_index)) for d in channels])
                cycle['physical_cycles']=(physical,)
        sync=item(selection['sync'],'sync'); sync_context=None
        if sync:
            proof=sync_proof(thaw(sync.body),manifests,approved=sync.review_state=='approved')
            endpoints=[]; channel_endpoints=[]; residuals=[]; applicability=True
            for point in proof['endpoints']:
                ep=point['endpoint']; key=capture_key(ep['capture']); endpoint_id=(key,ep['stream_id'])
                used=[d for d in channels if stream_identity(d.key,d.stream)==endpoint_id]
                if not used: continue
                starts=[sample_time_ns(d.stream,d.first_index,correction_ns=0) for d in used]
                ends=[sample_time_ns(d.stream,d.last_index,correction_ns=0) for d in used]
                low,high=min(starts),max(ends)
                if point['valid_start_ns'] is None or point['valid_end_ns'] is None or not int(point['valid_start_ns'])<=low<=high<int(point['valid_end_ns']): applicability=False
                endpoints.append(dict(identity=endpoint_id,reference_ns=Fraction(point['nominal_reference_ns']),start_ns=low,end_ns=high,
                    offset_ns=int(point['offset_error_ns']) if point['offset_error_ns'] is not None else None,
                    residual_ns=int(point['residual_ns']) if point['residual_ns'] is not None else None,
                    ppm=decimal_value(point['frequency_error_ppm']) if point['frequency_error_ppm'] is not None else None))
            for data in channels:
                channel_endpoints.append(stream_identity(data.key,data.stream)); cal=calibrations[channel_identity(data)]
                residuals.append(int(cal.body['residual_ns']) if cal and cal.body['residual_ns'] is not None else None)
            budget=SyncBudget(tuple(endpoints),tuple(channel_endpoints),tuple(residuals)) if applicability else None
            sync_context=dict(evidence=sync,proof=proof,budget=budget)
        if not origins:
            origins={}
            for frame in manifests.values():
                for stream in frame.streams:
                    origins[(frame.key,*source_identity(stream.source))]=dict(evidence=None,proof=dict(status='pending',reasons=('unknown_origin',)))
        return QualityContext(reference_key,prepared['completions'],{k:c['manifest_sha256'] if c else None for k,c in prepared['captures'].items()},
            {k:f.body['source_kind'] if (f:=manifests.get(k)) else None for k in prepared['captures']},origins,registry,tuple(channels),
            calibrations,configs,sync_context,cycle,prepared['statistics'],tuple(restrictions)),profile

    def registry(self,context,prepared):
        channels=context.streams; manifests=prepared['manifests']
        reference=manifests.get(context.key)
        if not channels or reference is None: return None
        known=all(d.time(d.first_index) is not None and manifests[d.key].body['utc_uncertainty_ns'] is not None for d in channels)
        sources=tuple(dict.fromkeys(CaptureKey(d.stream.source['gateway_id'],d.stream.source['device_id'],d.key.capture_id) for d in channels))
        return RegistryContext(self._read_connect).resolve(sources,utc_start=reference.body['utc_start'],
            start_ns=min(d.time(d.first_index) for d in channels) if known else Fraction(0),
            end_ns=max(d.time(d.last_index) for d in channels) if known else Fraction(0),
            uncertainty_ns=max(int(manifests[d.key].body['utc_uncertainty_ns']) for d in channels) if known else None,revision=None)

    def recheck(self,cur,prepared,*,strict=True):
        ids=[c['capture_pk'] for c in prepared['captures'].values() if c]
        cur.execute('SELECT waveforms.lock_business_captures(%s)',(ids,))
        restrictions=[]
        for key,old in prepared['captures'].items():
            current=self._capture(cur,key)
            if current!=old: stale('Capture changed during preparation')
            completion=self._completion(cur,current['capture_pk'] if current else None)
            if completion!=prepared['completions'][key]: stale('Completion changed during preparation')
            if current:
                cur.execute('SELECT restriction_id FROM waveforms.capture_restrictions WHERE subject_id=%s ORDER BY restriction_id LIMIT 1025',(current['capture_pk'],))
                found=cur.fetchall()
                if len(found)>1024: unavailable('Restriction snapshot exceeds bounded result')
                restrictions.extend(str(r[0]) for r in found)
        evidence,current=self.read_evidence(cur,prepared['refs'],strict=strict); origins={}
        for chosen in prepared['selection']['captures']:
            key=capture_key(chosen['key']); frame=prepared['manifests'].get(key)
            if frame:
                for stream in frame.streams: origins[(key,*source_identity(stream.source))]=dict(evidence=None,proof=dict(status='pending',reasons=('unknown_origin',)))
            for ref in chosen['origins']:
                item=evidence[ref['id']]
                if item.kind!='origin' or capture_key(item.body['capture'])!=key: raise ContractError('contract_invalid')
                p=item.body['provenance']
                cur.execute('SELECT * FROM waveforms.provenance_metadata WHERE observation_id=%s AND outcome_id=%s AND fact_id=%s AND source_ref=%s',
                            tuple(UUID(p[n]) for n in ('observation_id','outcome_id','fact_id','source_ref')))
                proof=self._origin_proof(item.body,prepared['manifests'],row(cur),item.review_state)
                identity=(key,*source_identity(item.body['source_identity']))
                if identity not in origins or origins[identity]['evidence'] is not None: raise ContractError('contract_invalid')
                origins[identity]=dict(evidence=item,proof=proof)
        context,profile=self.context(prepared,evidence,origins,restrictions,registry=None)
        # Called only while operator owns SHARED history guard. Fresh reader RR.
        registry=self.registry(context,prepared)
        previous=prepared['registry']
        if strict and previous and registry and previous.revision!=registry.revision: stale('Registry revision changed during preparation')
        return replace(context,registry_snapshot=registry),profile,current
