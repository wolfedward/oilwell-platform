"""Synthetic compact vectors, independent expected values, owned-DB helpers."""
from dataclasses import replace
from fractions import Fraction
from hashlib import sha256
import struct
from uuid import UUID,uuid4

from backend.waveforms.commands import Actor
from backend.waveforms.contracts import CaptureKey,ChannelSpec,StreamSpec
from backend.waveforms.evidence_contracts import EvidenceVersion,evidence_content,freeze

CREATOR=Actor('local','creator','Synthetic creator','operator')
REVIEWER=Actor('local','reviewer','Synthetic reviewer','admin')
COMMON=dict(source_kind='synthetic',valid_from=None,valid_to=None,attachment_ids=[],notes='Synthetic test only')


def evidence(kind,body,id=None):
    _,digest=evidence_content(kind,body)
    return EvidenceVersion(id or uuid4(),kind,digest,freeze(body),CREATOR,2,'approved','GW')


def config_body(quantity,unit,*,stream='s',channel=None,rate=1,signal='instantaneous',device='D'):
    return dict(COMMON,endpoint=dict(gateway_id='GW',device_id=device,stream_id=stream,channel_id=channel or quantity),
        reported_config_id='vendor-label',sensor_id='synthetic sensor',quantity=quantity,unit=unit,
        encoding='i32le',measurement_source='synthetic',signal_kind=signal,effective_bits=24,
        input_range=dict(min='-100000',max='100000',unit=unit),gain='1',bias='0',wiring=None,
        analog_filter=None,digital_filter=None,rate=dict(num=rate,den=1),
        clock=dict(clock_domain_id='clock',mode='shared_sampling_clock',trigger='test edge'),window=None,
        power=dict(method='Synthetic total power',averaging_window_ns='0',timestamp_convention='instantaneous',
                   bandwidth_hz='1',bandwidth_basis='Synthetic bandwidth') if quantity=='total_active_power' else None,
        reported_point_map_version=None)


def calibration_body(config):
    return dict(COMMON,endpoint=dict(config.body['endpoint']),sampling_config_version_id=str(config.id),
        sampling_config_sha256=config.content_sha256,scale='1',offset='0',unit=config.body['unit'],
        calibrated_range=dict(min='-100000',max='100000',unit=config.body['unit']),zero='0',direction='positive',
        measurement_uncertainty='0',time_correction_ns='0',residual_ns='0',method='Synthetic affine',
        basis='Synthetic known values',calibrated_at='2026-09-29T00:00:00.000000Z')


def profile_body(purpose='software_pair_test'):
    quantities=[dict(role=r,quantity=q,unit=u,signal_kind='instantaneous',measurement_source='synthetic')
        for r,q,u in (('power','total_active_power','W'),('load','load','kN'),('displacement','displacement','m'))]
    return dict(COMMON,purpose=purpose,target_equipment_type='synthetic fixture',
        electrical_input='three_phase_total_active_power',required_quantities=quantities,
        min_rates=[dict(role=q['role'],rate=dict(num=1,den=1)) for q in quantities],
        max_sync_error_ns='0',ranges=[dict(role=q['role'],range=dict(min='-100000',max='100000',unit=q['unit'])) for q in quantities],
        required_evidence=['sampling_config','calibration','origin','sync'],trial_basis='Software test only')


def pure_case(*,count=3,bad_index=None,quality=0,signal='instantaneous'):
    from backend.waveforms.quality import QualityContext,ProfileVersion
    from backend.waveforms.timing import ChannelData,SyncBudget
    from backend.waveforms.registry_context import RegistrySnapshot
    key=CaptureKey('GW','D',UUID(int=1)); channels=[]; configs={}; calibrations={}; packed=[]
    rate=max(1,(count-1)//2)
    for role,quantity,unit in (('power','total_active_power','W'),('load','load','kN'),('displacement','displacement','m')):
        config=evidence('sampling_config',config_body(quantity,unit,rate=rate,signal=signal if role=='power' else 'instantaneous'))
        calibration=evidence('calibration',calibration_body(config))
        channel=ChannelSpec(quantity,quantity,unit,'i32le','synthetic',config.body['signal_kind'],'vendor-label',calibration.id)
        channels.append(channel); identity=(key,'s',quantity)
        configs[identity]=config; calibrations[identity]=calibration
    for i in range(count):
        for n in range(3):
            flag=quality if i==bad_index and n==1 else 0
            value=(-3+i*6//(count-1)) if n==0 else (10 if n==1 else min(i,count-1-i)*4//max(1,(count-1)//2))
            packed.append(struct.pack('<iB',0 if flag==1 else value,flag))
    raw=b''.join(packed)
    stream=StreamSpec('s',dict(gateway_id='GW',device_id='D',boot_id=str(UUID(int=2))),'clock',rate,1,0,0,count,
        15,min(count,8000),(count+7999)//8000,len(raw),sha256(raw).hexdigest(),tuple(channels))
    views=tuple(ChannelData(key,stream,c,raw,0,count-1,0,role=r) for c,r in zip(channels,('power','load','displacement')))
    endpoint=dict(capture=dict(gateway_id='GW',device_id='D',capture_id=str(key.capture_id)),source=stream.source,stream_id='s',clock_domain_id='clock')
    sync=evidence('sync',dict(COMMON,attachment_ids=[str(UUID(int=4))],method='verified_linear_mapping',reference=endpoint,
        endpoints=[dict(endpoint=endpoint,reference_sample_index='0',reference_event='synthetic edge',nominal_reference_ns='0',
            rate=dict(num=rate,den=1),valid_start_ns='0',valid_end_ns='3000000000',offset_error_ns='0',frequency_error_ppm='0',residual_ns='0',clock_divisor=None)]))
    origin=evidence('origin',dict(COMMON,capture=endpoint['capture'],source_identity=stream.source,
        provenance=dict(transport='mqtt',source_ref=str(UUID(int=5)),observation_id=str(UUID(int=6)),outcome_id=str(UUID(int=7)),
            fact_id=str(UUID(int=8)),manifest_sha256='a'*64,import_id=None),verification_method='synthetic',verification_basis='synthetic',
        credential_attachment_ids=[],broker_config_ref='test'))
    context=QualityContext(key,{key:dict(status='complete')},{key:'a'*64},{key:'synthetic'},
        {('GW','D',str(UUID(int=2))):dict(evidence=origin,proof=dict(status='resolved',reasons=()))},
        RegistrySnapshot('resolved',(),1,{},None,'0','2000000000','0','0','2000000000',(),{}),
        views,calibrations,configs,dict(evidence=sync,proof=dict(status='resolved',reasons=()),
            budget=SyncBudget((dict(identity='s',reference_ns=Fraction(0),start_ns=Fraction(0),end_ns=Fraction(2000000000),offset_ns=0,residual_ns=0,ppm=Fraction(0)),),('s','s','s'),(0,0,0))),
        dict(purpose='software_pair_test',status='resolved',start_ns=Fraction(0),turn_ns=Fraction(1000000000),end_ns=Fraction(2000000000),absolute_support={}),{},())
    return context,ProfileVersion(evidence('profile',profile_body()))


def three_phase_case(*,signal='instantaneous',missing_phase=False,wiring=True):
    """Eight-channel arithmetic fixture; no field or device qualification."""
    from backend.waveforms.quality import ProfileVersion,channel_identity
    from backend.waveforms.timing import ChannelData,SyncBudget
    context,_=pure_case(); channels=list(context.streams[1:]); configs=dict(context.sampling_configs); calibrations=dict(context.calibrations)
    old=channel_identity(context.streams[0]); configs.pop(old); calibrations.pop(old)
    for base,unit in (('voltage','V'),('current','A')):
        for phase in ('a','b','c'):
            quantity=base+'_'+phase
            if missing_phase and quantity=='current_c': continue
            body=config_body(quantity,unit,stream=quantity,signal=signal if quantity=='current_a' else 'instantaneous')
            body['wiring']=dict(connection='three_phase',voltage_reference='phase_to_neutral',phase=phase.upper(),
                                phase_sequence='abc',current_direction='source_to_load') if wiring else None
            config=evidence('sampling_config',body); calibration=evidence('calibration',calibration_body(config))
            channel=ChannelSpec(quantity,quantity,unit,'i32le','synthetic',body['signal_kind'],'vendor-label',calibration.id)
            raw=b''.join(struct.pack('<iB',value,0) for value in (-3,0,3))
            stream=replace(context.streams[0].stream,stream_id=quantity,frame_bytes=5,decoded_bytes=15,
                           channels=(channel,),data_sha256=sha256(raw).hexdigest())
            data=ChannelData(context.key,stream,channel,raw,0,2,0,role=quantity); channels.append(data)
            configs[channel_identity(data)]=config; calibrations[channel_identity(data)]=calibration
    body=profile_body(); body['electrical_input']='three_phase_instantaneous'
    body['required_quantities']=[dict(role=d.role,quantity=d.channel.quantity,unit=d.channel.unit,
        signal_kind=d.channel.signal_kind,measurement_source='synthetic') for d in channels]
    body['min_rates']=[dict(role=d.role,rate=dict(num=1,den=1)) for d in channels]
    body['ranges']=[dict(role=d.role,range=dict(min='-100000',max='100000',unit=d.channel.unit)) for d in channels]
    endpoints=tuple(dict(identity=s,reference_ns=Fraction(0),start_ns=Fraction(0),end_ns=Fraction(2000000000),
                         offset_ns=0,residual_ns=0,ppm=Fraction(0)) for s in dict.fromkeys(d.stream.stream_id for d in channels))
    sync=context.sync_evidence|{'budget':SyncBudget(endpoints,tuple(d.stream.stream_id for d in channels),(0,)*len(channels))}
    return replace(context,streams=tuple(channels),sampling_configs=configs,calibrations=calibrations,sync_evidence=sync),ProfileVersion(evidence('profile',body))


def seed_pair(case,*,two=False,count=3,bad_index=None,quality=0,same_source=False,
              inversion=False,reuse_evidence=None,utc_start='2026-09-29T00:00:00.000000Z'):
    """Actual intake, assembly and independent evidence approval; synthetic only."""
    import base64
    from backend.waveforms.evidence import EvidenceService
    from backend.waveforms.commands import Command,evidence_request_hash
    from backend.waveforms.repository import WaveformRepository
    from backend.waveforms.assembly import Assembler
    from backend.waveforms.wire import parse_frame
    from tests.waveform_support import LIMITS,FIXED_TIME
    from tests.test_waveform_repository import observation
    from tests.waveform_vectors import encoded
    service=EvidenceService(lambda:case.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:case.connect('reader'))
    def command(op,target=None,version=0,actor=CREATOR,**kwargs):
        c=Command(actor,uuid4(),version,'Synthetic test only','0'*64)
        return replace(c,request_hash=evidence_request_hash(op,target,c,**kwargs))
    def approved(kind,body):
        value=service.create(kind,body,command('create',kind=kind,body=body))
        return service.review(value.id,decision='approve',command=command('review',value.id,1,REVIEWER,decision='approve'))
    def ref(item): return dict(id=str(item.id),content_sha256=item.content_sha256,version=str(item.version))
    profile_data=profile_body()
    if inversion:
        profile_data['required_quantities'][0].update(role='active_power',signal_kind='window_mean')
        profile_data['min_rates'][0]['role']='active_power'; profile_data['ranges'][0]['role']='active_power'
        profile_data['required_quantities'].append(dict(role='crank_angle',quantity='crank_angle',unit='rad',signal_kind='instantaneous',measurement_source='synthetic'))
        profile_data['min_rates'].append(dict(role='crank_angle',rate=dict(num=1,den=1)))
        profile_data['ranges'].append(dict(role='crank_angle',range=dict(min='-10',max='10',unit='rad')))
    profile=approved('profile',profile_data); selected=[]; captures=[]; frames=[]; raws=[]; wire_captures=[]; endpoints=[]; cycle_ref=None; channel_evidence={}
    repo=WaveformRepository(case.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW')
    layouts=((('electrical',(('power','total_active_power','W'),)),('sensor',(('load','load','kN'),('displacement','displacement','m')))),)
    if inversion:
        layouts=((('electrical',(('active_power','total_active_power','W'),)),('sensor',(('load','load','kN'),('displacement','displacement','m'),('crank_angle','crank_angle','rad')))),)
    if two: layouts=((layouts[0][0],),(layouts[0][1],))
    rate=max(1,(count-1)//2); shared_boot=uuid4()
    for n,layout in enumerate(layouts):
        capture_id=uuid4(); boot=shared_boot if same_source else uuid4(); device='D'+str(0 if same_source else n); key=dict(gateway_id='GW',device_id=device,capture_id=str(capture_id))
        source=dict(gateway_id='GW',device_id=device,boot_id=str(boot)); streams=[]; payloads=[]
        for name,quantities in layout:
            channels=[]
            for role,quantity,unit in quantities:
                config_data=config_body(quantity,unit,stream=name,device=device,rate=rate)
                if inversion:
                    config_data['valid_from']='2026-01-01T00:00:00.000000Z'
                    if role=='crank_angle': config_data['input_range']=dict(min='-10000000',max='10000000',unit=unit)
                    if role=='active_power':
                        config_data.update(signal_kind='window_mean',
                            wiring=dict(connection='motor_terminal_three_phase',voltage_reference='line_to_line',phase='total',phase_sequence='ABC',current_direction='positive_into_motor'),
                            window=dict(definition='centered arithmetic mean',averaging_window_ns='1000000',timestamp_convention='center'))
                        config_data['power'].update(averaging_window_ns='1000000',timestamp_convention='center',bandwidth_hz='100')
                if reuse_evidence:
                    config,calibration=reuse_evidence[role]
                else:
                    config=approved('sampling_config',config_data)
                    calibration_data=calibration_body(config)
                    if inversion:
                        calibration_data.update(valid_from='2026-01-01T00:00:00.000000Z',time_correction_ns='17')
                        if role=='crank_angle': calibration_data['scale']='0.000001'
                        if role=='active_power': calibration_data.update(scale='2',offset='1')
                    calibration=approved('calibration',calibration_data)
                channel_evidence[role]=(config,calibration)
                channels.append(dict(channel_id=quantity,quantity=quantity,unit=unit,encoding='i32le',measurement_source='synthetic',
                    signal_kind=config.body['signal_kind'],config_id='vendor-label',calibration_id=str(calibration.id)))
                selected.append(dict(role=role,capture=key,stream_id=name,channel_id=quantity,first_index='0',last_index=str(count-1),
                    sampling_config=ref(config),calibration=ref(calibration)))
            raw=bytearray()
            for i in range(count):
                for role,quantity,unit in quantities:
                    flag=quality if role=='load' and i==bad_index else 0
                    value=(-3+i*6//(count-1)) if role in ('power','active_power') else (round(i*6283185/(count-1)) if role=='crank_angle' else 10 if role=='load' else min(i,count-1-i)*4//max(1,(count-1)//2))
                    raw.extend(struct.pack('<iB',0 if flag==1 else value,flag))
            data=bytes(raw); chunk_frames=min(count,32 if inversion else 4096)
            streams.append(dict(stream_id=name,source=source,clock_domain_id='clock',rate_num=rate,rate_den=1,first_sample_index='0',offset_ns='0',
                frame_count=count,frame_bytes=5*len(channels),chunk_frames=chunk_frames,chunk_count=(count+chunk_frames-1)//chunk_frames,
                decoded_bytes=len(data),data_sha256=sha256(data).hexdigest(),channels=channels)); payloads.append(data)
        has_sensor=any(s['stream_id']=='sensor' for s in streams)
        cycle=dict(cycle_id=str(uuid4()),start_ns='0',turn_ns='1000000000',end_ns='2000000000',direction='clockwise',zero_reference='start',
            reference_channel='displacement',reference_events=[dict(event=event,stream_id='sensor',sample_index=str(index))
                for event,index in (('start',0),('turn',(count-1)//2),('end',count-1))]) if has_sensor else None
        body=dict(key,schema='OWP-WAVE/1.0',kind='manifest',boot_id=str(boot),capture_kind='cycle' if cycle else 'event',source_kind='synthetic',
            producer=dict(manufacturer='Synthetic fixture',recorder_version='1'),source_evidence=None,utc_start=utc_start,utc_uncertainty_ns='0',
            clock_domain_id='clock',sync_evidence_id=None,window_ns='2000000000',cycle=cycle,streams=streams)
        manifest=encoded(body); observed=observation(manifest,topic=f'oil/v2/GW/{device}/waveform'); stored=repo.ingest(observed)
        wire=[manifest]
        case.assertEqual(stored.status,'stored')
        for stream,data in zip(streams,payloads):
            for i in range(stream['chunk_count']):
                start=i*stream['chunk_frames']; size=min(stream['chunk_frames'],count-start); piece=data[start*stream['frame_bytes']:(start+size)*stream['frame_bytes']]
                chunk=encoded(dict(key,schema='OWP-WAVE/1.0',kind='chunk',manifest_sha256=sha256(manifest).hexdigest(),stream_id=stream['stream_id'],
                    chunk_index=i,sample_start=start,sample_count=size,data_sha256=sha256(piece).hexdigest(),data_base64=base64.b64encode(piece).decode()))
                case.assertEqual(repo.ingest(observation(chunk,topic=f'oil/v2/GW/{device}/waveform')).status,'stored')
                wire.append(chunk)
        assembler=Assembler(lambda:case.connect('assembler'),clock=lambda:FIXED_TIME,limits=LIMITS)
        lease=assembler.claim(uuid4()); case.assertIsNotNone(lease); case.assertEqual(assembler.run(lease).status,'complete')
        observation_id=case.scalar('SELECT observation_id FROM waveforms.provenance_metadata WHERE source_ref=%s',(observed.source_ref,))
        origin=approved('origin',dict(COMMON,capture=key,source_identity=source,provenance=dict(transport='mqtt',source_ref=str(observed.source_ref),
            observation_id=str(observation_id),outcome_id=str(stored.outcome_id),fact_id=str(stored.fact_id),manifest_sha256=sha256(manifest).hexdigest(),import_id=None),
            verification_method='synthetic',verification_basis='synthetic',credential_attachment_ids=[],broker_config_ref='synthetic-ACL'))
        captures.append(dict(key=key,manifest_sha256=sha256(manifest).hexdigest(),origins=[ref(origin)]))
        frame=parse_frame(manifest); frames.append(frame); raws.append(manifest); wire_captures.append(tuple(wire))
        for stream in streams:
            ep=dict(capture=key,source=source,stream_id=stream['stream_id'],clock_domain_id='clock')
            endpoints.append(dict(endpoint=ep,reference_sample_index='0',reference_event='documented synthetic edge',nominal_reference_ns='0',rate=dict(num=rate,den=1),
                valid_start_ns='0',valid_end_ns='2000000001',offset_error_ns='0',frequency_error_ppm='0',residual_ns='0',clock_divisor=None))
        if cycle: cycle_ref=dict(capture=key,cycle_id=cycle['cycle_id'])
    payload=b'%PDF-synthetic reviewed timing correspondence'
    metadata=dict(media_type='application/pdf',payload_bytes=len(payload),payload_sha256=sha256(payload).hexdigest())
    attachment=service.upload_attachment(payload,media_type='application/pdf',command=command('upload',attachment=metadata))
    sync=approved('sync',dict(COMMON,attachment_ids=[str(attachment.id)],method='verified_linear_mapping',reference=endpoints[0]['endpoint'],endpoints=endpoints))
    selection=dict(purpose='software_pair_test',profile=ref(profile),captures=captures,channels=selected,cycle=cycle_ref,sync=ref(sync))
    return dict(selection=selection,frames=frames,raws=raws,wire_captures=wire_captures,evidence_service=service,evidence_command=command,profile=profile,sync=sync,channel_evidence=channel_evidence)
