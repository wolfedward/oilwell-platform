"""Explicit v2 parameter/evidence binding; local UUIDs are NEVER approvals."""
from dataclasses import dataclass
from fractions import Fraction as Q
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from uuid import NAMESPACE_URL,uuid5
import struct

from backend.inversion.contracts import canonical_bytes,strict_json
from backend.inversion.coverage_contracts import (
    ChannelEvidence,PredictionEvidence,CoverageContract,channel_evidence_bytes,
    parameter_core_bytes,parse_coverage,pin_wire)
from backend.inversion.coverage_sources import checker_sha256
from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
from backend.inversion.policy_v2 import POLICY_BODY
from backend.inversion.series import ChannelPin,Sample,SampleSeries,ReferenceInputs
from backend.inversion.synthetic_registry import case_definition
from backend.waveforms.files import scan_file
from backend.waveforms.wire import parse_frame
from .contracts import Channel,Cycle,EvidenceBinding

ROLES=('active_power','crank_angle','load','displacement')
UNITS=('W','rad','kN','m')
SCALES=('0.01','0.00000001','0.001','0.000001')
EPOCH=1790784000000000000


def _body(case,pins):
    def evidence(purpose):
        return [{**pins['attachment'],'purpose':purpose,
            'source_description':'Synthetic software definition, not instrument certification: '+purpose}]
    domain={name:{'min':lo,'max':hi} for name,lo,hi in (
        ('power_W','-250000','250000'),('speed_rad_s','0.1','2'),('acceleration_rad_s2','-2','2'),
        ('load_N','0','100000'),('temperature_K','250','350'),('supply_voltage_V','300','500'),
        ('supply_frequency_Hz','45','55'),('window_ns','1','10000000'),('bandwidth_Hz','1','1000'))}
    domain['evidence_refs']=evidence('domain')
    drive={k:v for k,v in case.body['drive'].items() if k not in ('loss_W','loss_error_W')}
    drive.update(supply_kind='direct_line_induction',evidence_refs=evidence('drive'),
        loss_grid={'power_W':['-250000','250000'],'motor_speed_rad_s':['1','250'],
            'loss_W':[['1200']*2 for _ in range(2)],'error_W':[['0.02']*2 for _ in range(2)],
            'evidence_refs':evidence('loss_grid')})
    policy=dict(POLICY_BODY)
    return {'schema':'inversion.parameter-set/2','assembly_id':pins['assembly_id'],
        'assembly_version':pins['assembly_version'],'source_kind':'synthetic',
        'valid_from':pins['valid_from'],'valid_to':pins['valid_to'],'parent_version':None,
        'geometry':dict(case.body['geometry'])|{'evidence_refs':evidence('geometry')},
        'bodies':dict(case.body['bodies'])|{'evidence_refs':evidence('bodies'),
            'component_ledger':[{'id':'motor','name':'Software upstream inertia','owner':'upstream'},
                                {'id':'crank','name':'Software crank and balance moment','owner':'crank'}]},
        'phase':{'channel_pin':pins['channels']['crank_angle'],'zero_reading_rad':'0',
            'encoder_direction':1,'unwrap_kind':'unwrapped','evidence_refs':evidence('phase')},
        'drive':drive,'domain':domain,'uncertainty':{'torque_bound_Nm':'100','factor_bound_m':'0.0001',
            'speed_bound_rad_s':'0.00001','numerical_budget_Nm':'0.01','numerical_factor_budget_m':'0.0000001',
            'evidence_refs':evidence('uncertainty')},
        'policy':{'body':policy,'content_sha256':sha256(canonical_bytes(policy)).hexdigest()},
        'provenance':{'source_organization':'OWP software laboratory','source_date':pins['valid_from'][:10],
            'notes':pins['notes'],'attachment_pins':evidence('provenance'),
            'calibration_cycles':[{'start_ns':str(pins['cycle_start_ns']),'end_ns':str(pins['cycle_end_ns']),
                'physical_cycle_id':pins['physical_cycle_id']}],
            'operating_conditions':[{'id':'condition-1','valid_from':pins['valid_from'],'valid_to':pins['valid_to'],
                'temperature_K':{'min':'290','max':'310'},'supply_voltage_V':{'min':'380','max':'420'},
                'supply_frequency_Hz':{'min':'49','max':'51'},'source_description':'Synthetic fixed conditions',
                'attachment_pins':evidence('operating_conditions')}],
            'channel_bindings':[{'role':role,'source':'native_waveform','modbus':None,
                'channel_pin':pins['channels'][role],'installation_evidence_refs':evidence('installation')} for role in ROLES]}}


def coverage_for(case,pins,evidence):
    body=_body(case,pins); inputs={}
    for channel,scale,unit,pre in ((evidence.power,SCALES[0],'W','0.01'),
                                 (evidence.angle,SCALES[1],'rad','0.00000000000000000001')):
        inputs[channel.role]={'pin':pin_wire(channel.pin),'original_file_sha256':channel.original_file_sha256,
            'bound_file_sha256':channel.bound_file_sha256,'channel_sha256':channel.channel_sha256,
            'raw_sha256':sha256(canonical_bytes(channel.raw)).hexdigest(),
            'time_sha256':sha256(canonical_bytes([str(t) for t in channel.time_ns])).hexdigest(),
            'quality_sha256':sha256(canonical_bytes(channel.quality)).hexdigest(),'count':len(channel.raw),
            'encoding':{'unit':unit,'scale':scale,'offset':'0','rounding':'nearest_even','sample_format':'i32',
                'pre_encoding_error_bound':pre}}
    speed=case.body['motion']['epsilon']!='0'; noisy=case.body['variant']=='noise'
    contract={'schema':'inversion.coverage-contract/1','scope':'finite_synthetic_input/1',
        'source_kind':'synthetic','purpose':'software_pair_test','field_accuracy_certified':False,
        'validation_status':'not_field_validated','pack_id':evidence.pack_id,'case_id':case.case_id,
        'config_sha256':evidence.config_sha256,'generator_sha256':evidence.generator_sha256,'inputs':inputs,
        'cycle':{'start_ns':str(pins['cycle_start_ns']),'end_ns':str(pins['cycle_end_ns']),
            'physical_cycle_id':pins['physical_cycle_id']},
        'power_support':{'kind':'centered_mean','window_ns':'1000000'},
        'measurement':{'angle_quantization_rad':'0.000000005','angle_calibration_error_rad':'0',
            'angle_noise_error_rad':'0.000001' if noisy else '0','power_quantization_W':'0.005',
            'power_noise_error_W':'10' if noisy else '0','power_average_mismatch_W':'1','loss_error_W':'0.02',
            'parameter_semantics':'exact_registered_constants','unmodeled_terms':'none_in_registered_family','basis':'installed_case/1'},
        'time':{'common_offset_bound_ns':'15625000' if case.body['variant']=='shifted' else '0',
            'differential_bound_ns':'0','basis':'installed_case/1'},
        'motion':{'family':'linear_sinusoid/1','theta0_rad':'4.68689893991090706065',
            'rate_rad_s':'0.78539816339744830962','epsilon_rad':case.body['motion']['epsilon'],
            'omega_max_rad_s':'0.864' if speed else '0.786','acceleration_max_rad_s2':'0.062' if speed else '0',
            'third_derivative_max_rad_s3':'0.049' if speed else '0','fourth_derivative_max_rad_s4':'0.039' if speed else '0',
            'support_start_ns':str(EPOCH-31250000),'support_end_ns':str(EPOCH+8031250000),
            'basis':'installed_case/1','mechanical_family':'rigid_four_bar_crank_only/1'},
        'binding':{'parameter_core_sha256':sha256(parameter_core_bytes(body)).hexdigest(),
            'policy_sha256':body['policy']['content_sha256'],'checker_version':'finite-input/1',
            'checker_sha256':checker_sha256(),'budget_attachment_pin':pins['budget_attachment']}}
    return parse_coverage(canonical_bytes(contract))


def bind_v2(case,pins,contract:CoverageContract)->bytes:
    if case!=case_definition(case.config_sha256,case.case_id) or type(contract) is not CoverageContract:
        raise ValueError('installed definition and coverage required')
    raw=canonical_bytes(_body(case,pins)|{'coverage_contract':contract.body})
    parse_parameter_set_v2(raw)
    return raw


def decode_native(raw):
    """Read exact i32 codes, including zero-valued missing samples; never floats."""
    scanned=scan_file(BytesIO(raw)); manifest=scanned.manifest
    if len(manifest.streams)!=1: raise ValueError('single native stream required')
    stream=manifest.streams[0]
    if (stream.frame_count!=1025 or stream.rate_num!=128 or stream.rate_den!=1 or
        stream.first_sample_index!=0 or stream.offset_ns!=0 or
        tuple(c.channel_id for c in stream.channels)!=ROLES or
        tuple(c.unit for c in stream.channels)!=UNITS or any(c.encoding!='i32le' for c in stream.channels)):
        raise ValueError('registered native format required')
    rows={role:[[],[]] for role in ROLES}
    for chunk in sorted((parse_frame(line) for line in raw.splitlines()[1:]),key=lambda c:c.sample_start):
        for index in range(chunk.sample_count):
            for j,role in enumerate(ROLES):
                value,quality=struct.unpack_from('<iB',chunk.data,index*stream.frame_bytes+j*5)
                rows[role][0].append(value);rows[role][1].append(quality)
    return scanned,tuple(Channel(role,unit,scale,'0',tuple(rows[role][0]),tuple(rows[role][1]))
        for role,unit,scale in zip(ROLES,UNITS,SCALES))


def native_evidence(original_raw,bound_raw,channels,*,pack_id,case_id,config_sha256,generator_sha256):
    original,original_channels=decode_native(original_raw);bound,native=decode_native(bound_raw)
    if native!=original_channels: raise ValueError('binding must not alter native samples')
    def physical_manifest(value):
        body=strict_json(canonical_bytes(value.body))
        for key in ('capture_id','source_evidence','sync_evidence_id'): body.pop(key)
        for stream in body['streams']:
            for channel in stream['channels']:
                channel.pop('config_id');channel.pop('calibration_id')
        return body
    if physical_manifest(original.manifest)!=physical_manifest(bound.manifest):
        raise ValueError('binding must not alter timing or physical descriptors')
    manifest=bound.manifest
    from backend.waveforms.timing import sample_time_ns
    from backend.waveforms.quality_context import utc_ns
    epoch=utc_ns(manifest)
    stream=manifest.streams[0]
    times=tuple(epoch+sample_time_ns(stream,i,correction_ns=0) for i in range(1025))
    output=[]; references=[]
    for channel in native:
        pin=ChannelPin.from_wire(channels[channel.role])
        if (pin.gateway_id!=manifest.key.gateway_id or pin.device_id!=manifest.key.device_id or
            str(pin.capture_id)!=str(manifest.key.capture_id) or pin.stream_id!=stream.stream_id or
            pin.channel_id!=channel.role or pin.manifest_sha256!=bound.manifest_sha256 or
            pin.first_index!=0 or pin.last_index!=1024): raise ValueError('native pin mismatch')
        if channel.role in ROLES[:2]:
            values=dict(role=channel.role,raw=channel.raw,time_ns=times,quality=channel.quality,
                scale=Q(channel.scale),offset=Q(channel.offset),pin=pin,
                original_file_sha256=original.sha256,bound_file_sha256=bound.sha256)
            values['channel_sha256']=sha256(channel_evidence_bytes(SimpleNamespace(**values))).hexdigest()
            output.append(ChannelEvidence(**values))
        else:
            scale=Q(channel.scale)*(1000 if channel.role=='load' else 1)
            references.append(SampleSeries(channel.role,tuple(Sample(t,i,r*scale if q==0 else None,q)
                for i,(t,r,q) in enumerate(zip(times,channel.raw,channel.quality))),pin))
    return PredictionEvidence(*output,pack_id,case_id,config_sha256,generator_sha256),ReferenceInputs(*references)


@dataclass(frozen=True)
class OfflineBinding:
    parameters: object
    evidence: PredictionEvidence
    reference: ReferenceInputs


def offline_binding(pack,case_id):
    """Local in-memory binding; placeholders must never enter a service approval."""
    from .storage import load_pack,strict_json as package_json
    from .config_v2 import parse_config_v2
    from .waveforms import encode_cycle
    pack=load_pack(pack.root)
    config=parse_config_v2(pack.manifest['configuration'])
    record=next(r for r in pack.manifest['cases'] if r['case_id']==case_id and r['facts'].get('data_kind')=='cycle')
    raw=(pack.root/next(p for p in record['artifact_paths'] if p.endswith('.owpwave'))).read_bytes()
    metadata=package_json((pack.root/next(p for p in record['artifact_paths'] if p.endswith('.json'))).read_bytes())
    original,native=decode_native(raw)
    descriptors=[{key:getattr(channel,key) for key in ('role','unit','scale','offset')} for channel in native]
    if (metadata.get('channels')!=descriptors or metadata.get('file_sha256')!=original.sha256 or
        metadata.get('case_id')!=case_id or metadata.get('pack_id')!=pack.manifest['pack_id'] or
        metadata.get('source_kind')!='synthetic' or metadata.get('sample_rate_hz')!=128 or metadata.get('frame_count')!=1025):
        raise ValueError('native metadata mismatch')
    cycle=Cycle(case_id,128,tuple(i*7812500 for i in range(1025)),native,metadata['turn_index'],metadata['model'],metadata['diagnostics'])
    prefix=pack.manifest['pack_id']+'/'+case_id+'/OFFLINE/'
    identity=lambda purpose:str(uuid5(NAMESPACE_URL,prefix+purpose))
    evidence_pin=lambda purpose:{'id':identity(purpose),'content_sha256':sha256((prefix+purpose).encode()).hexdigest(),'review_version':2}
    sampling={role:evidence_pin(role+'/sampling') for role in ROLES}
    calibration={role:evidence_pin(role+'/calibration') for role in ROLES}
    binding=EvidenceBinding(sampling,calibration,identity('origin'),identity('sync'))
    bound_raw=encode_cycle(cycle,config,binding).artifacts[0].content
    bound=scan_file(BytesIO(bound_raw)); m=bound.manifest
    pins={'assembly_id':identity('assembly'),'assembly_version':1,'valid_from':'2026-01-01T00:00:00.000000Z',
        'valid_to':'2027-01-01T00:00:00.000000Z','cycle_start_ns':EPOCH,'cycle_end_ns':EPOCH+8000000000,
        'physical_cycle_id':m.body['cycle']['cycle_id'],
        'notes':'OFFLINE synthetic software binding. Placeholder IDs are not service approvals. Same-cycle comparison is not independent field validation.',
        'attachment':{'id':identity('physical'),'content_sha256':sha256((prefix+'physical').encode()).hexdigest()},
        'budget_attachment':{'id':identity('budget'),'content_sha256':sha256((prefix+'budget').encode()).hexdigest(),
            'purpose':'uncertainty','source_description':'OFFLINE budget placeholder; no approval or instrument certificate'},
        'channels':{role:{'gateway_id':m.key.gateway_id,'device_id':m.key.device_id,'capture_id':str(m.key.capture_id),
            'stream_id':'native','channel_id':role,'manifest_sha256':bound.manifest_sha256,
            'sampling_pin':sampling[role],'calibration_pin':calibration[role],'first_index':0,'last_index':1024} for role in ROLES}}
    config_sha=sha256(canonical_bytes(pack.manifest['configuration'])).hexdigest()
    case=case_definition(config_sha,case_id)
    evidence,reference=native_evidence(raw,bound_raw,pins['channels'],pack_id=pack.manifest['pack_id'],
        case_id=case_id,config_sha256=config_sha,generator_sha256=pack.manifest['generator']['source_sha256'])
    contract=coverage_for(case,pins,evidence)
    return OfflineBinding(parse_parameter_set_v2(bind_v2(case,pins,contract)),evidence,reference)
