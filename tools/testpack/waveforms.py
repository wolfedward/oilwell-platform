"""OWP-WAVE encoding with immutable native samples and external scale metadata."""
import base64
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
import struct
from uuid import NAMESPACE_URL,UUID,uuid5

from backend.waveforms.files import scan_file
from backend.waveforms.samples import iter_samples
from backend.waveforms.wire import parse_frame
from .contracts import Artifact,Case,Channel,Cycle,EvidenceBinding,PackConfig
from .storage import canonical_bytes,generator_fingerprint,pack_identity


def wire_bytes(value):
    return canonical_bytes(value)[:-1]  # LF belongs to the file, never the wire frame.


def identity(config,case_id,purpose):
    return str(uuid5(NAMESPACE_URL,pack_identity(config,generator_fingerprint())+'/'+case_id+'/'+purpose))


def _encode(case_id,config,channels,rate,window_ns,cycle=None,binding=None):
    count=len(channels[0].raw)
    if any(len(c.raw)!=count or len(c.quality)!=count for c in channels):
        raise ValueError('inconsistent channel frame count')
    common={'schema':'OWP-WAVE/1.0','gateway_id':'REVIEW-GW','device_id':'TESTPACK-PUMP-001',
            'capture_id':identity(config,case_id,'capture')}
    # A separately bound capture gets its own identity; original package is never replaced.
    if binding is not None:
        common['capture_id']=str(uuid5(NAMESPACE_URL,common['capture_id']+'/'+sha256(canonical_bytes(asdict(binding))).hexdigest()))
    boot=identity(config,case_id,'boot')
    descriptors=[]
    for channel in channels:
        sampling=calibration=None
        if binding is not None:
            pins=[mapping.get(channel.role) for mapping in (binding.sampling,binding.calibration)]
            for pin in pins:
                if not isinstance(pin,dict) or set(pin)!={'id','content_sha256','review_version'}:
                    raise ValueError('actual evidence pins required')
                UUID(pin['id'])
                if type(pin['review_version']) is not int or pin['review_version']<2:
                    raise ValueError('evidence review version required')
            sampling,calibration=(pin['id'] for pin in pins)
            if binding.reported_configs is not None:
                sampling=binding.reported_configs[channel.role]
        descriptors.append({'channel_id':channel.role,'quantity':'total_active_power' if channel.role=='active_power' else channel.role,
            'unit':channel.unit,'encoding':'i32le','measurement_source':'synthetic',
            'signal_kind':'window_mean' if channel.role=='active_power' else 'instantaneous',
            'config_id':sampling,'calibration_id':calibration})
    data=b''.join(struct.pack('<iB',c.raw[i],c.quality[i]) for i in range(count) for c in channels)
    chunk_frames=256
    stream={'stream_id':'native','source':{'gateway_id':common['gateway_id'],'device_id':common['device_id'],'boot_id':boot},
            'clock_domain_id':'synthetic-clock','rate_num':rate,'rate_den':1,'first_sample_index':'0','offset_ns':'0',
            'frame_count':count,'frame_bytes':5*len(channels),'chunk_frames':chunk_frames,
            'chunk_count':(count+chunk_frames-1)//chunk_frames,'decoded_bytes':len(data),'data_sha256':sha256(data).hexdigest(),
            'channels':descriptors}
    manifest={**common,'kind':'manifest','boot_id':boot,'capture_kind':'cycle' if cycle else 'event','source_kind':'synthetic',
              'producer':{'manufacturer':'OWP synthetic software model','recorder_version':'owp.testpack.generator/1'},
              'source_evidence':binding.origin_id if binding else None,'utc_start':config.start_utc,'utc_uncertainty_ns':'0',
              'clock_domain_id':'synthetic-clock','sync_evidence_id':binding.sync_id if binding else None,
              'window_ns':str(window_ns),'cycle':cycle,'streams':[stream]}
    raw=wire_bytes(manifest)
    lines=[raw]
    for index in range(stream['chunk_count']):
        start=index*chunk_frames
        size=min(chunk_frames,count-start)
        piece=data[start*stream['frame_bytes']:(start+size)*stream['frame_bytes']]
        lines.append(wire_bytes({**common,'kind':'chunk','stream_id':'native','chunk_index':index,
            'sample_start':start,'sample_count':size,'manifest_sha256':sha256(raw).hexdigest(),
            'data_sha256':sha256(piece).hexdigest(),'data_base64':base64.b64encode(piece).decode()}))
    result=b'\n'.join(lines)+b'\n'
    scan_file(BytesIO(result))
    return result


def _metadata(case_id,config,channels,extra):
    return {'schema':'owp.testpack.wave-metadata/1','case_id':case_id,
            'pack_id':pack_identity(config,generator_fingerprint()),'source_kind':'synthetic',
            'channels':[{k:getattr(c,k) for k in ('role','unit','scale','offset')} for c in channels],**extra}


def encode_cycle(cycle:Cycle,config:PackConfig,binding:EvidenceBinding|None=None)->Case:
    declaration={'cycle_id':identity(config,cycle.case_id,'cycle'),'start_ns':'0',
            'turn_ns':str(cycle.time_ns[cycle.turn_index]),'end_ns':str(cycle.time_ns[-1]),'direction':'clockwise',
            'zero_reference':'synthetic displacement minimum','reference_channel':'displacement',
            'reference_events':[{'stream_id':'native','sample_index':str(i),'event':label}
                                for i,label in ((0,'start'),(cycle.turn_index,'turn'),(len(cycle.time_ns)-1,'end'))]}
    raw=_encode(cycle.case_id,config,cycle.channels,cycle.sample_rate_hz,cycle.time_ns[-1],declaration,binding)
    metadata=_metadata(cycle.case_id,config,cycle.channels,{'model':cycle.model,'diagnostics':cycle.diagnostics,
            'sample_rate_hz':cycle.sample_rate_hz,'frame_count':len(cycle.time_ns),'turn_index':cycle.turn_index,
            'file_sha256':sha256(raw).hexdigest()})
    path=f'cycles/{cycle.case_id}.owpwave'
    return Case(cycle.case_id,cycle.diagnostics['category'],
        (Artifact(path,'application/x-owpwave',raw),Artifact(f'cycles/{cycle.case_id}.json','application/json',canonical_bytes(metadata))),
        {'data_kind':'cycle','pairing_evidence_bound':binding is not None,'frame_count':len(cycle.time_ns),
         'source_kind':'synthetic','purpose':'software_pair_test'},
        ({'check_id':cycle.case_id+'-wire','layer':'offline','assertion':'equals','target':path,'expected':'valid'},))


def encode_current(case_id,config,channels,rate,diagnostics):
    raw=_encode(case_id,config,channels,rate,16000000000)
    path=f'currents/{case_id}.owpwave'
    metadata=_metadata(case_id,config,channels,{'diagnostics':diagnostics,'sample_rate_hz':rate,
            'frame_count':len(channels[0].raw),'file_sha256':sha256(raw).hexdigest()})
    return Case(case_id,'signal_fault',(Artifact(path,'application/x-owpwave',raw),
        Artifact(f'currents/{case_id}.json','application/json',canonical_bytes(metadata))),
        {'data_kind':'current','source_kind':'synthetic','frame_count':len(channels[0].raw),
         'sample_rate_hz':rate,'duration_s':16,'fault_confirmation':False},
        ({'check_id':case_id+'-wire','layer':'offline','assertion':'equals','target':path,'expected':'valid'},))


def decode_channels(case:Case)->dict:
    raw=next(a.content for a in case.artifacts if a.path.endswith('.owpwave'))
    meta=json.loads(next(a.content for a in case.artifacts if a.path.endswith('.json')))
    parsed=scan_file(BytesIO(raw))
    scales={c['role']:c for c in meta['channels']}
    output={key:{'values':[],'quality':[],'unit':c['unit']} for key,c in scales.items()}
    streams={s.stream_id:s for s in parsed.manifest.streams}
    for line in raw.splitlines()[1:]:
        chunk=parse_frame(line)
        for sample in iter_samples(streams[chunk.stream_id],chunk.data,sample_start=chunk.sample_start):
            c=scales[sample.channel_id]
            value=None if sample.raw_value is None else float(Decimal(sample.raw_value)*Decimal(c['scale'])+Decimal(c['offset']))
            output[sample.channel_id]['values'].append(value)
            output[sample.channel_id]['quality'].append(sample.quality)
    return output


def waveform_negative_cases(valid:Case)->tuple[Case,...]:
    raw=next(a.content for a in valid.artifacts if a.path.endswith('.owpwave'))
    lines=raw.splitlines()
    missing=b'\n'.join(lines[:-1])+b'\n'
    bad=json.loads(lines[1]);bad['data_sha256']='0'*64
    digest=b'\n'.join([lines[0],wire_bytes(bad),*lines[2:]])+b'\n'
    bad=json.loads(lines[1]);data=bytearray(base64.b64decode(bad['data_base64']));data[4]=5
    bad['data_base64']=base64.b64encode(data).decode();bad['data_sha256']=sha256(data).hexdigest()
    quality=b'\n'.join([lines[0],wire_bytes(bad),*lines[2:]])+b'\n'
    truncated=b'\n'.join([*lines[:-1],lines[-1][:-1]])+b'\n'
    cases=[]
    for name,content,code in [('missing-chunk',missing,'layout_invalid'),('bad-digest',digest,'digest_mismatch'),
                               ('bad-quality',quality,'layout_invalid'),('truncated-json',truncated,'json_invalid')]:
        path=f'negative/{name}.owpwave'
        cases.append(Case('protocol-'+name,'protocol_fault',(Artifact(path,'application/x-owpwave',content),),
            {'base_case':valid.case_id,'mutation':name},
            ({'check_id':'protocol-'+name,'layer':'offline','assertion':'rejects','target':path,'expected':code},)))
    return tuple(cases)
