"""Pure archived-byte fixture. No database/environment configuration is read.

Raw export framing, parsing, calibrated ChannelData and context construction are
real. Only database metadata, audit persistence and current approval boundaries
are doubled. This is not a substitute for the later isolated HTTP test.
"""
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime,timedelta
from fractions import Fraction as Q
from hashlib import sha256
import json
import math
from types import SimpleNamespace as N
from unittest.mock import patch
from uuid import uuid4,UUID

from backend.inversion.contracts import canonical_bytes
from backend.inversion.coverage_contracts import ChannelEvidence,PredictionEvidence,channel_evidence_bytes,pin_wire,parameter_core_bytes
from backend.inversion.input_contracts import parse_run_request
from backend.inversion.input_acquisition import InputAcquisition
from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
from backend.inversion.series import ChannelPin,EvidencePin,ProducerEndpoint
from backend.waveforms.config import Quota,CapacityLimits
from backend.waveforms.dataset_context import PreparedMember
from backend.waveforms.dataset_contracts import snapshot_hash
from backend.waveforms.member_exports import MemberResearchExporter,MemberExportRecord
from backend.waveforms.pair_contracts import key_dict
from backend.waveforms.wire import parse_frame
from tests.waveform_pairing_support import evidence,config_body,calibration_body,COMMON,CREATOR
from tests.inversion_v2_support import declared_fixture
from tools.testpack.contracts import Channel,Cycle
from tools.testpack.config_v2 import PackConfigV2
from tools.testpack.waveforms import encode_cycle

EPOCH=1790784000000000000


class Connection:
    autocommit=False
    def __init__(self,fixture): self.fixture=fixture; self.connection=self; self.answer=None
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def cursor(self): return self
    def close(self): pass
    def commit(self): self.fixture.events.append('commit')
    def rollback(self): self.fixture.events.append('rollback')
    def fetchone(self): return self.answer
    def fetchall(self): return self.answer
    def execute(self,sql,args=()):
        if sql.startswith('SET '): self.answer=None
        elif 'SELECT set_config(' in sql: self.answer=('1',)
        elif "current_setting('fsync')" in sql: self.answer=('on','on','on')
        elif "current_setting('synchronous_commit')" in sql: self.answer=('on',)
        elif 'pg_catalog.pg_class' in sql:
            self.answer=[(name,'p') for name in ('facts','message_outcomes','capacity','captures','receipts','observations','state_head')]
        elif 'SELECT waveforms.audited_raw(' in sql:
            self.fixture.events.append(('audited_raw',str(args[0])))
            self.answer=(self.fixture.facts[args[0]],)
        elif sql.startswith('INSERT INTO waveforms.business_audit'):
            self.fixture.events.append('download_audit'); self.answer=None
        else: raise AssertionError('Unexpected fake database SQL: '+str(sql))
        return self


def archive_fixture(*,power_offset='0',purpose='software_pair_test',two_capture=False):
    fixture=N(events=[],facts={},records={},purpose=purpose)
    roles=('active_power','crank_angle','load','displacement')
    raw_values=(tuple(0 if i==400 else i*10 for i in range(1025)),
        tuple(round(2*math.pi*i/1024*10**8) for i in range(1025)),(42000,)*1025,
        tuple(min(i,1024-i)*1000 for i in range(1025)))
    qualities=(tuple(1 if i==400 else 0 for i in range(1025)),(0,)*1025,(0,)*1025,(0,)*1025)
    channels=tuple(Channel(role,unit,scale,'0',raw,quality) for role,unit,scale,raw,quality in zip(
        roles,('W','rad','kN','m'),('0.01','0.00000001','0.001','0.000001'),raw_values,qualities))
    cycle=Cycle('cycle-base',128,tuple(i*7812500 for i in range(1025)),channels,512,{}, {'category':'mechanistic'})
    raw=encode_cycle(cycle,PackConfigV2()).artifacts[0].content
    lines=raw.splitlines(); frame=parse_frame(lines[0]); key=frame.key; stream=frame.streams[0]
    archives={key:(raw,frame)};power_key=key
    if two_capture:
        body=json.loads(lines[0]);body['capture_id']=str(uuid4())
        body['utc_start']=(datetime.fromisoformat(body['utc_start'].replace('Z','+00:00'))-timedelta(seconds=1)).isoformat(timespec='microseconds').replace('+00:00','Z')
        body['window_ns']='9000000000';body['streams'][0]['offset_ns']='1000000000'
        body['cycle']['cycle_id']=str(uuid4())
        for field in ('start_ns','turn_ns','end_ns'):body['cycle'][field]=str(int(body['cycle'][field])+10**9)
        other_lines=[canonical_bytes(body)];manifest_sha=sha256(other_lines[0]).hexdigest()
        for line in lines[1:]:
            chunk=json.loads(line);chunk.update(capture_id=body['capture_id'],manifest_sha256=manifest_sha)
            other_lines.append(canonical_bytes(chunk))
        other_raw=b'\n'.join(other_lines)+b'\n';other_frame=parse_frame(other_lines[0]);power_key=other_frame.key
        archives[power_key]=(other_raw,other_frame)
    fixture.raw,fixture.frame,fixture.raw_values,fixture.qualities=raw,frame,raw_values,qualities
    connection=lambda:Connection(fixture)
    quota=Quota(10000000,10000,1000000,10,1000000,1000000,10000,100000)
    exporter=MemberResearchExporter(connection,read_connect=connection,limits=CapacityLimits(quota,{'test-v2':quota}),shared_accounting_bucket='test-v2')
    exporter._guard=lambda cur:None;exporter.recheck_member=lambda *_,**__:()
    selected=[]; pins={}; records=fixture.records
    def save(item): records[str(item.id)]=item; return item
    def ref(item): return dict(id=str(item.id),content_sha256=item.content_sha256,version=str(item.version))
    configs={};calibrations={}
    for channel in channels:
        role=channel.role; quantity='total_active_power' if role=='active_power' else role
        config=config_body(quantity,channel.unit,stream='native',channel=role,rate=128,
            signal='window_mean' if role=='active_power' else 'instantaneous',device=key.device_id)
        config['endpoint']['gateway_id']=key.gateway_id;config['clock']['clock_domain_id']='synthetic-clock'
        if role=='active_power':
            config['power'].update(averaging_window_ns='1000000',timestamp_convention='center',bandwidth_hz='64')
            config['window']=dict(definition='Centered mean',averaging_window_ns='1000000',timestamp_convention='center')
            config['wiring']=dict(connection='motor_terminal_three_phase',voltage_reference='line_to_line',phase='total',phase_sequence='ABC',current_direction='positive_into_motor')
        config=save(evidence('sampling_config',config));configs[role]=config
        calibration=calibration_body(config);calibration.update(scale=channel.scale,offset=power_offset if role=='active_power' else '0')
        calibration=save(evidence('calibration',calibration));calibrations[role]=calibration
        selected.append(dict(role=role,capture=key_dict(power_key if role=='active_power' else key),stream_id='native',channel_id=role,
            first_index='0',last_index='1024',sampling_config=ref(config),calibration=ref(calibration)))
    endpoint=dict(capture=key_dict(key),source=dict(stream.source),stream_id='native',clock_domain_id=stream.clock_domain_id)
    endpoints=[]
    for archive_key,(_,archive_frame) in archives.items():
        shift=0 if archive_key==key else 10**9
        endpoints.append(dict(endpoint=endpoint|{'capture':key_dict(archive_key)},reference_sample_index='0',reference_event='Fixture start',
            nominal_reference_ns=str(shift),rate=dict(num=128,den=1),valid_start_ns=str(shift),valid_end_ns=str(shift+8000000001),
            offset_error_ns='0',frequency_error_ppm='0',residual_ns='0',clock_divisor=None))
    sync=save(evidence('sync',dict(COMMON,attachment_ids=[str(uuid4())],method='verified_linear_mapping',reference=endpoint,endpoints=endpoints)))
    body=declared_fixture();cov=body['coverage_contract'];origins={};files={};buffers={};captures={};manifests={}
    for archive_key,(archive_raw,archive_frame) in archives.items():
        imported=uuid4();archive_lines=archive_raw.splitlines();manifests[archive_key]=archive_frame
        mapping=dict(schema='owp.testpack.binding/1',pack_id=cov['pack_id'],case_id='cycle-base',config_sha256=cov['config_sha256'],
            generator_sha256=cov['generator_sha256'],original_file_sha256='c'*64,bound_file_sha256=sha256(archive_raw).hexdigest())
        origins[archive_key]=save(evidence('origin',dict(COMMON,capture=key_dict(archive_key),source_identity=dict(stream.source),
            provenance=dict(transport='file',source_ref=str(imported),import_id=str(imported),observation_id=str(uuid4()),
                outcome_id=str(uuid4()),fact_id=str(uuid4()),manifest_sha256=archive_frame.raw_sha256),
            verification_method='testpack_original_bound/1',verification_basis=canonical_bytes(mapping).decode(),
            credential_attachment_ids=[],broker_config_ref=None)))
        plan=[]
        for line in archive_lines:
            id=uuid4();fixture.facts[id]=line;plan.append(dict(fact_id=str(id),raw_length=len(line),raw_sha256=sha256(line).hexdigest()))
        descriptor=dict(key=key_dict(archive_key),capture_pk=str(uuid4()),filename=str(archive_key.capture_id)+'.owpwave',content_length=len(archive_raw),
            file_sha256=sha256(archive_raw).hexdigest(),manifest_sha256=archive_frame.raw_sha256,file_plan_sha256=sha256(canonical_bytes(plan)).hexdigest(),
            source_files=[dict(import_id=str(imported),file_sha256=sha256(archive_raw).hexdigest(),bytes=len(archive_raw),line_count=len(archive_lines),final_lf=True)])
        files[archive_key]=dict(descriptor=descriptor,plan=plan)
        buffers[(archive_key,'native')]=b''.join(parse_frame(line).data for line in archive_lines[1:])
        captures[archive_key]=dict(capture_pk=UUID(descriptor['capture_pk']),manifest_sha256=archive_frame.raw_sha256,completion_status='complete')
    origin=origins[key];descriptor=files[key]['descriptor']
    selection=dict(purpose=purpose,profile=None,captures=[dict(key=key_dict(k),manifest_sha256=f.raw_sha256,origins=[ref(origins[k])]) for k,f in manifests.items()],
        channels=selected,cycle=dict(capture=key_dict(key),cycle_id=frame.body['cycle']['cycle_id']),sync=ref(sync))
    prepared=dict(selection=selection,manifests=manifests,buffers=buffers,captures=captures,completions={k:dict(status='complete') for k in manifests},statistics={})
    context,_=exporter.context(prepared,records,{},(),registry=None)
    assert context.cycle['status']=='resolved'
    physical=context.cycle['physical_cycles'][0]; cov['cycle']['physical_cycle_id']=physical['physical_cycle_key']
    body['provenance']['calibration_cycles'][0]['physical_cycle_id']=physical['physical_cycle_key']
    evidence_channels=[]
    for index,data in enumerate(context.streams):
        role=data.role;cal=calibrations[role];config=configs[role]
        own_key=data.key;own_frame=manifests[own_key]
        pin=ChannelPin(own_key.gateway_id,own_key.device_id,own_key.capture_id,'native',role,own_frame.raw_sha256,
            EvidencePin(cal.id,cal.content_sha256,2),EvidencePin(config.id,config.content_sha256,2),0,1024,
            EvidencePin(sync.id,sync.content_sha256,2),ProducerEndpoint(key.gateway_id,key.device_id))
        pins[role]=pin
        next(b for b in body['provenance']['channel_bindings'] if b['role']==role)['channel_pin']=pin_wire(pin)
        if role=='crank_angle': body['phase']['channel_pin']=pin_wire(pin)
        if role in ('active_power','crank_angle'):
            values=dict(role=role,raw=raw_values[index],time_ns=tuple(Q(EPOCH+t) for t in cycle.time_ns),quality=qualities[index],
                scale=Q(channels[index].scale),offset=Q(power_offset) if role=='active_power' else Q(0),pin=pin,
                original_file_sha256='c'*64,bound_file_sha256=files[own_key]['descriptor']['file_sha256'])
            values['channel_sha256']=sha256(channel_evidence_bytes(N(**values))).hexdigest();ev=ChannelEvidence(**values);evidence_channels.append(ev)
            item=cov['inputs'][role];item.update(pin=pin_wire(pin),original_file_sha256=ev.original_file_sha256,
                bound_file_sha256=ev.bound_file_sha256,channel_sha256=ev.channel_sha256,
                raw_sha256=sha256(canonical_bytes(ev.raw)).hexdigest(),quality_sha256=sha256(canonical_bytes(ev.quality)).hexdigest(),
                time_sha256=sha256(canonical_bytes([str(t) for t in ev.time_ns])).hexdigest())
            item['encoding']['offset']=power_offset if role=='active_power' else '0'
    cov['binding']['parameter_core_sha256']=sha256(parameter_core_bytes(body)).hexdigest()
    parameters=parse_parameter_set_v2(canonical_bytes(body))
    request={key:str(uuid4()) for key in ('dataset_version_id','pair_id','model_id','parameter_id')}
    request.update(assembly_id=body['assembly_id'],dataset_sha256='a'*64,pair_sha256='b'*64,model_sha256='e'*64,
        parameter_sha256=parameters.raw_sha256,member_ordinal=0,pair_review_version=2,parameter_review_version=3,
        model_review_version=2,selection_version=1,operating_condition_ids=['condition-1'])
    req=parse_run_request(canonical_bytes(request));version=dict(content_sha256=req.dataset_sha256,purpose=purpose)
    certificate=dict(selection=selection,refs=exporter.references(selection),snapshot=dict(physical_cycles=context.cycle['physical_cycles'],
        pair=dict(id=str(req.pair_id),content_sha256=req.pair_sha256,review_version=2)))
    member=PreparedMember(req.dataset_version_id,0,certificate,captures,files,())
    export_record=MemberExportRecord(uuid4(),req.dataset_version_id,0,req.dataset_sha256,snapshot_hash('member',certificate['snapshot']),CREATOR)
    exporter._version_row=lambda *_:version;exporter.load_member_prepared=lambda *_:member
    exporter._get=lambda cur,id:records[str(id)]
    exporter._export=lambda *_:export_record;exporter.create=lambda *_:export_record
    acquisition=InputAcquisition(connection,read_connect=connection,exporter=exporter,authorize=lambda *_:None,accounting_bucket='test-v2')
    fixture.__dict__.update(acquisition=acquisition,request=req,context=context,selection=selection,member=member,pins=pins,
        manifests=manifests,parameters=parameters,evidence=PredictionEvidence(*evidence_channels,cov['pack_id'],cov['case_id'],cov['config_sha256'],cov['generator_sha256']),
        origin=origin,descriptor=descriptor)
    return fixture


def acquire(fixture):
    def approved(*_,**__):
        from backend.inversion.history import calibration_independence
        p=fixture.parameters;physical=fixture.context.cycle['physical_cycles'][0]
        reasons=calibration_independence(p,[],physical['physical_cycle_key'],Q(EPOCH),Q(EPOCH+8*10**9),Q(0),physical['source_intervals'])
        return p,dict(parameter_body=json.loads(p.raw),model_body={'artifact':{'installed_id':'beam_energy_balance/2',
            'algorithm':'beam_energy_balance/2','parameter_schema':'inversion.parameter-set/2','numerical_assumptions':[]}},
            independent_comparison=not reasons,independence_reasons=list(reasons),field_accuracy_certified=False)
    with patch('backend.inversion.input_acquisition.applicability_with_cursor',side_effect=approved),\
            patch('backend.inversion.input_acquisition.recheck_with_cursor',return_value=()),\
            patch('backend.inversion.input_acquisition.lock',return_value=None):
        return fixture.acquisition.prepare_input(fixture.request,CREATOR,check=lambda:None)
