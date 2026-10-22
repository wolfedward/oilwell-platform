"""Audited complete-file acquisition, bounded to one frozen pair and two captures.

Raw reads finish on independent WP04 connections before the inversion guarded
transaction. Prediction and reference values remain separate, with exact times.
"""
from dataclasses import asdict,replace
from fractions import Fraction
from hashlib import sha256
from math import pi
from uuid import UUID,uuid4
from backend.waveforms.commands import Actor,Command
from backend.waveforms.contracts import ManifestFrame,ChunkFrame,CaptureKey
from backend.waveforms.member_exports import MemberResearchExporter,member_request_hash
from backend.waveforms.pair_contracts import capture_key,public
from backend.waveforms.quality_context import utc_ns
from backend.waveforms.samples import iter_samples
from backend.waveforms.wire import parse_frame
from .contracts import canonical_bytes,fail
from .input_contracts import RunRequest,InputSnapshot,PreparedInput
from .series import EvidencePin,ChannelPin,ProducerEndpoint,Sample,SampleSeries,PredictionInputs,ReferenceInputs,CycleWindow
from .history import applicability_with_cursor,recheck_with_cursor
from .input_timing import absolute_support,relative_alignment
from .parameters import refuse
from .transactions import lock,LockPlan

ROLES=frozenset(('active_power','crank_angle','load','displacement'))
UNITS={'active_power':{'W':Fraction(1),'kW':Fraction(1000)},
       'crank_angle':{'rad':Fraction(1),'deg':Fraction(pi)/180},
       'load':{'N':Fraction(1),'kN':Fraction(1000)},
       'displacement':{'m':Fraction(1),'mm':Fraction(1,1000)}}
QUANTITIES=dict(active_power='total_active_power',crank_angle='crank_angle',load='load',displacement='displacement')


class InputAcquisition:
    def __init__(self,connect,*,read_connect,exporter,authorize,accounting_bucket):
        if not all(callable(value) for value in (connect,read_connect,authorize)) or type(exporter) is not MemberResearchExporter:
            raise ValueError('Explicit inversion connections, audited member exporter and authority provider required')
        if accounting_bucket!=exporter._bucket: raise ValueError('Shared accounting bucket differs')
        self.connect,self.read_connect,self.exporter,self.authorize,self.bucket=connect,read_connect,exporter,authorize,accounting_bucket

    def _authorize(self,actor):
        for capability in ('inversion.run','waveform.read','waveform.raw.read','waveform.export'):
            self.authorize(actor,capability)

    def _files(self,member,record,actor,check):
        manifests={}; buffers={}
        for key,file in member.files.items():
            check(); self._authorize(actor)
            context=self.exporter.open(record.id,actor=actor)
            stream=context.stream_capture(key); manifest=None; hashes={}; sizes={}; counts={}; wanted=set()
            file_hash=sha256();file_bytes=0
            try:
                for raw in stream:
                    check()
                    file_hash.update(raw);file_bytes+=len(raw)
                    if file_bytes>33554432: refuse('input_size_limit')
                    if raw==b'\n': continue
                    frame=parse_frame(raw)
                    if manifest is None:
                        if type(frame) is not ManifestFrame or frame.key!=key or frame.raw_sha256!=file['descriptor']['manifest_sha256']:
                            refuse('file_identity')
                        manifest=frame; manifests[key]=frame
                        wanted={c['stream_id'] for c in member.certificate['selection']['channels'] if capture_key(c['capture'])==key}
                        for spec in frame.streams:
                            hashes[spec.stream_id]=sha256(); sizes[spec.stream_id]=0; counts[spec.stream_id]=0
                            if spec.stream_id in wanted: buffers[(key,spec.stream_id)]=bytearray()
                        continue
                    if type(frame) is not ChunkFrame or frame.key!=key or frame.manifest_sha256!=manifest.raw_sha256: refuse('file_identity')
                    spec=next((s for s in manifest.streams if s.stream_id==frame.stream_id),None)
                    if spec is None: refuse('file_identity')
                    index=counts[spec.stream_id]; count=min(spec.chunk_frames,spec.frame_count-index*spec.chunk_frames)
                    if frame.chunk_index!=index or frame.sample_start!=index*spec.chunk_frames or frame.sample_count!=count: refuse('file_identity')
                    for index,_ in enumerate(iter_samples(spec,frame.data,sample_start=frame.sample_start)):
                        if index%256==0: check()
                    hashes[spec.stream_id].update(frame.data); sizes[spec.stream_id]+=len(frame.data); counts[spec.stream_id]+=1
                    if spec.stream_id in wanted: buffers[(key,spec.stream_id)].extend(frame.data)
                if manifest is None: refuse('file_identity')
                if file_bytes!=file['descriptor']['content_length'] or file_hash.hexdigest()!=file['descriptor']['file_sha256']:
                    refuse('file_identity')
                for spec in manifest.streams:
                    if counts[spec.stream_id]!=spec.chunk_count or sizes[spec.stream_id]!=spec.decoded_bytes or hashes[spec.stream_id].hexdigest()!=spec.data_sha256:
                        refuse('file_identity')
            finally:
                stream.close(); context.close()
        return manifests,{key:bytes(value) for key,value in buffers.items()}

    def prepare_input(self,request: RunRequest,actor: Actor,*,check) -> PreparedInput:
        if type(request) is not RunRequest or type(actor) is not Actor or not callable(check): fail()
        self._authorize(actor); check()
        # WP04 metadata only; no original reads or inversion locks in this phase.
        with self.exporter._reader() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'); cur=conn.cursor()
            version=self.exporter._version_row(cur,request.dataset_version_id)
            member=self.exporter.load_member_prepared(cur,request.dataset_version_id,request.member_ordinal)
            selection=member.certificate['selection']; pair=member.certificate['snapshot']['pair']
            if version['content_sha256']!=request.dataset_sha256: refuse('dataset_pin_changed')
            if (pair['id']!=str(request.pair_id) or pair['content_sha256']!=request.pair_sha256
                or int(pair['review_version'])!=request.pair_review_version): refuse('pair_pin_changed')
            if member.applicability: refuse('member_restricted')
            selected={c['role']:c for c in selection['channels']}
            if not ROLES<=selected.keys(): refuse('required_role_missing')
            if any(int(selected[role]['last_index'])-int(selected[role]['first_index'])+1>32768 for role in ROLES):
                refuse('selection_sample_limit')
            evidence,_=self.exporter.read_evidence(cur,member.certificate['refs'],strict=True)
        command=Command(actor,uuid4(),0,'Inversion bounded member input','0'*64)
        command=replace(command,request_hash=member_request_hash(request.dataset_version_id,request.member_ordinal,command))
        record=self.exporter.create(request.dataset_version_id,request.member_ordinal,command)
        manifests,buffers=self._files(member,record,actor,check)
        prepared=dict(selection=selection,manifests=manifests,buffers=buffers,captures=member.captures,
            completions={key:dict(status=value['completion_status']) for key,value in member.captures.items()},statistics={})
        context,_=self.exporter.context(prepared,evidence,{},(),registry=None)
        if context.cycle['status']!='resolved' or len(context.cycle['physical_cycles'])!=1: refuse('cycle_unresolved')
        physical=context.cycle['physical_cycles'][0]
        if canonical_bytes(context.cycle['physical_cycles'])!=canonical_bytes(member.certificate['snapshot']['physical_cycles']): refuse('cycle_changed')
        cycle=CycleWindow(context.cycle['start_ns'],context.cycle['end_ns'],physical['physical_cycle_key'])
        budget,relative_terms=relative_alignment(context,manifests,cycle.end_ns-cycle.start_ns)
        uncertainty,absolute_terms=absolute_support(context,manifests)
        series={}; pins={}; power_config=None
        for data in context.streams:
            check()
            if data.role not in ROLES: continue
            role=data.role; chosen=selected[role]; calibration=evidence[chosen['calibration']['id']]; sampling=evidence[chosen['sampling_config']['id']]
            if (data.channel.quantity!=QUANTITIES[role] or calibration.body['unit'] not in UNITS[role]
                or data.channel.signal_kind!=('window_mean' if role=='active_power' else 'instantaneous')):
                refuse('channel_semantics_unknown')
            if role=='active_power':
                power_config=sampling.body
                power=power_config['power']; window=power_config['window']; wiring=power_config['wiring']
                if (not power or not window or not wiring or power['timestamp_convention']!='center' or window['timestamp_convention']!='center'
                    or power['averaging_window_ns']!=window['averaging_window_ns'] or int(power['averaging_window_ns'])<=0
                    or any(not wiring.get(key) for key in ('connection','voltage_reference','phase','phase_sequence','current_direction'))
                    or wiring['phase']!='total' or wiring['connection']!='motor_terminal_three_phase'
                    or wiring['current_direction']!='positive_into_motor'
                    or not power['method'] or not power['bandwidth_basis']): refuse('power_time_semantics_unknown')
            pin=ChannelPin(data.key.gateway_id,data.key.device_id,data.key.capture_id,data.stream.stream_id,data.channel.channel_id,
                manifests[data.key].raw_sha256,EvidencePin(calibration.id,calibration.content_sha256,calibration.version),
                EvidencePin(sampling.id,sampling.content_sha256,sampling.version),data.first_index,data.last_index,
                EvidencePin(context.sync_evidence['evidence'].id,context.sync_evidence['evidence'].content_sha256,context.sync_evidence['evidence'].version),
                ProducerEndpoint(data.stream.source['gateway_id'],data.stream.source['device_id']))
            values=[]
            for index in range(data.first_index,data.last_index+1):
                if index%256==0: check()
                value=data.value(index); quality=data.sample(index)[1]; time=data.time(index)
                if time is None: refuse('timing_unknown')
                values.append(Sample(time,index,None if value is None else value*UNITS[role][calibration.body['unit']],quality))
            series[role]=SampleSeries(role,tuple(values),pin); pins[role]=pin
        prediction=PredictionInputs(series['active_power'],series['crank_angle'],cycle,int(power_config['power']['averaging_window_ns']),Fraction(budget))
        reference=ReferenceInputs(series['load'],series['displacement'])
        anchor=utc_ns(manifests[context.key])
        if anchor is None: refuse('observation_utc_unknown')
        first=min(d.time(d.first_index) for d in context.streams); last=max(d.time(d.last_index) for d in context.streams)
        sources=list(dict.fromkeys(CaptureKey(d.stream.source['gateway_id'],d.stream.source['device_id'],d.key.capture_id) for d in context.streams))
        observation=dict(utc_start=manifests[context.key].body['utc_start'],start_ns=str(anchor+first),end_ns=str(anchor+last),
            relative_start_ns=str(first),relative_end_ns=str(last),uncertainty_ns=str(uncertainty),
            relative_alignment_ns=str(budget),relative_timing_terms=relative_terms,absolute_timing_terms=absolute_terms,
            physical_cycle_key=cycle.physical_cycle_id,sources=[public(key) for key in sources])
        self._authorize(actor); check()
        with self.read_connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            cur=conn.cursor(); cur.execute("SET LOCAL statement_timeout='3s'")
            cur.execute("SET LOCAL lock_timeout='1s'")
            parameters,applicability=applicability_with_cursor(cur,request,self.bucket,observation,pins,physical['source_intervals'])
            v2=parameters.schema=='inversion.parameter-set/2'
            native_evidence=None;source_binding=None
            if v2:
                from .input_contracts_v2 import extract_prediction_evidence,prediction_evidence_bytes,check_evidence_binding
                native_evidence,source_binding=extract_prediction_evidence(context,manifests,member.files,evidence,pins,selection,
                    purpose=version['purpose'],check=check)
                check_evidence_binding(native_evidence,parameters)
                # context times already include cross-capture UTC shifts. Add
                # the reference anchor exactly once to ALL four series.
                series={role:replace(item,samples=tuple(replace(sample,time_ns=sample.time_ns+anchor) for sample in item.samples))
                    for role,item in series.items()}
                cycle=replace(cycle,start_ns=cycle.start_ns+anchor,end_ns=cycle.end_ns+anchor)
                declared=parameters.coverage_contract.body['time']
                bound=Fraction(declared['common_offset_bound_ns'])+Fraction(declared['differential_bound_ns'])
                if budget>bound: refuse('timing_envelope')
                prediction=PredictionInputs(series['active_power'],series['crank_angle'],cycle,prediction.power_window_ns,bound)
                reference=ReferenceInputs(series['load'],series['displacement'])
            from .solver import _full_cycle
            complete,diagnostic=_full_cycle(prediction,parameters,parameters.policy)
            if not complete: refuse('full_cycle_'+diagnostic['reason'])
            for frame in manifests.values():
                if frame.body['source_kind']!=parameters.source_kind: refuse('source_mismatch')
            for value,domain in ((Fraction(power_config['power']['bandwidth_hz']),parameters.domain.bandwidth_Hz),
                                 (prediction.power_window_ns,parameters.domain.window_ns)):
                if not domain.min<=value<=domain.max: refuse('power_domain')
            for role,domain in (('active_power',parameters.domain.power_W),('load',parameters.domain.load_N)):
                if any(sample.value_si is not None and not domain.min<=sample.value_si<=domain.max for sample in series[role].samples): refuse('observed_domain')
            body=dict(schema='inversion.input/1',request=public(request),actor=asdict(actor),accounting_bucket=self.bucket,purpose=version['purpose'],
                member_snapshot=member.certificate['snapshot'],selection=selection,channel_pins={role:public(pin) for role,pin in pins.items()},
                evidence=[dict(id=str(e.id),kind=e.kind,content_sha256=e.content_sha256,version=e.version,body=public(e.body)) for e in sorted(evidence.values(),key=lambda e:str(e.id))],
                captures=[dict(descriptor=file['descriptor'],source_kind=manifests[key].body['source_kind'],
                               declared_sources=[dict(s.source) for s in manifests[key].streams]) for key,file in member.files.items()],
                observation=observation,source_ranges=physical['source_intervals'],applicability=applicability,
                numerical_assumptions=applicability['model_body']['artifact']['numerical_assumptions'],
                samples={role:[dict(index=str(s.index),time_ns=str(s.time_ns),value_si=str(s.value_si) if s.value_si is not None else None,quality=s.quality)
                               for s in item.samples] for role,item in series.items()})
            if v2:
                from .contracts import strict_json
                body.update(schema='inversion.input/2',parameter_core_sha256=parameters.parameter_core_sha256,
                    prediction_evidence=strict_json(prediction_evidence_bytes(native_evidence)),source_binding=source_binding)
            encoded=canonical_bytes(body)
            if len(encoded)>67108864: refuse('input_size_limit')
            snapshot=InputSnapshot(body,encoded,sha256(encoded).hexdigest())
        self._authorize(actor); check()
        with self.connect() as conn:
            cur=conn.cursor(); cur.execute("SET LOCAL statement_timeout='3s'"); cur.execute("SET LOCAL lock_timeout='1s'")
            lock(cur,LockPlan(waveform=True,registry=True))
            reasons=recheck_with_cursor(cur,snapshot)
            if reasons: refuse('input_changed')
            conn.rollback()
        check()
        if v2:
            from .input_contracts_v2 import PreparedInputV2
            return PreparedInputV2(prediction,reference,parameters,parameters.policy,snapshot,native_evidence)
        return PreparedInput(prediction,reference,parameters,parameters.policy,snapshot)
