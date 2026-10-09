"""Explicit synthetic experiment producers; importing performs no work."""
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import time
from unittest.mock import patch
from uuid import uuid4


class Counters(ctypes.Structure):
    # Stable identities avoid growing ctypes' POINTER cache on every sample.
    _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in (
        'PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
        'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage','PrivateUsage')]


class Memory(ctypes.Structure):
    _fields_=[('length',wintypes.DWORD),('load',wintypes.DWORD)]+[(name,ctypes.c_ulonglong) for name in
        ('total_physical','available_physical','total_pagefile','available_pagefile','total_virtual','available_virtual','extended_virtual')]


def process_memory(process):
    """Query the retained owned process HANDLE, never a potentially reused PID."""
    if os.name!='nt' or process.poll() is not None: raise ValueError('Live owned Windows process required')
    return _memory_for_handle(int(process._handle),process.pid)


def current_process_memory():
    """Diagnostic controller counters; the pseudo-HANDLE is never owned/closed."""
    if os.name!='nt': raise ValueError('Windows current process required')
    current=ctypes.WinDLL('kernel32',use_last_error=True).GetCurrentProcess
    current.argtypes=(); current.restype=wintypes.HANDLE
    return _memory_for_handle(current(),os.getpid())


def _memory_for_handle(handle,pid):
    api=ctypes.WinDLL('psapi',use_last_error=True).GetProcessMemoryInfo
    api.argtypes=(wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD); api.restype=wintypes.BOOL
    value=Counters(); value.cb=ctypes.sizeof(value)
    if not api(wintypes.HANDLE(handle),ctypes.byref(value),value.cb):
        raise OSError('Process memory query failed')
    return dict(monotonic=time.monotonic(),pid=pid,working_set_bytes=value.WorkingSetSize,
        peak_working_set_bytes=value.PeakWorkingSetSize,private_commit_bytes=value.PrivateUsage)


def host_resources(path):
    import shutil
    value=Memory(); value.length=ctypes.sizeof(value)
    api=ctypes.WinDLL('kernel32',use_last_error=True).GlobalMemoryStatusEx
    api.argtypes=(ctypes.POINTER(Memory),); api.restype=wintypes.BOOL
    if not api(ctypes.byref(value)): raise OSError('Host memory query failed')
    import platform
    return dict(physical_total_bytes=value.total_physical,physical_available_bytes=value.available_physical,
                disk_available_bytes=shutil.disk_usage(path).free,cpu_count=os.cpu_count(),monotonic=time.monotonic(),
                platform=platform.platform(),processor=platform.processor(),python=platform.python_version())


def safety_check(host,samples=(),*,preflight=False):
    gib=1024**3
    ram,disk=(3*gib,10*gib) if preflight else (2*gib,8*gib)
    if host['physical_available_bytes']<ram or host['disk_available_bytes']<disk: raise RuntimeError('safety.host_reserve')
    if sum(s['working_set_bytes'] for s in samples)>2*gib or any(max(s['working_set_bytes'],s.get('peak_working_set_bytes',0))>1.5*gib for s in samples):
        raise RuntimeError('safety.owned_rss')


class OwnedSampler:
    """Continuous owned-handle sampler; failed queries abort rather than become zero."""
    def __init__(self,root,processes,abort,*,seconds=120,sink=None,began=None):
        import threading
        self.root,self.processes,self.abort,self.seconds=root,processes,abort,seconds
        self.samples=[]; self.errors=[]; self.failure=None; self.stop=threading.Event()
        self.began=time.monotonic() if began is None else began
        self.sink=sink
        self.thread=threading.Thread(target=self._run,name='task17-owned-sampler')

    def _run(self):
        while not self.stop.is_set():
            try:
                samples=[process_memory(p) for p in self.processes()]
                host=host_resources(self.root); controller=current_process_memory()
                value=dict(monotonic=time.monotonic(),processes=samples,host=host,controller=controller)
                self.samples.append(value)
                if self.sink: self.sink(value)
                safety_check(host,samples)
                self.check()
            except BaseException as error:
                self.failure=error
                self.errors.append(str(error) if isinstance(error,RuntimeError) else type(error).__name__)
                try: self.abort()
                except BaseException as cleanup: error.add_note('Owned sampler abort: '+type(cleanup).__name__)
                return
            self.stop.wait(.1)

    def __enter__(self): self.thread.start(); return self
    def check(self):
        if self.failure is not None: raise self.failure
        if time.monotonic()-self.began>self.seconds: raise RuntimeError('safety.operation_deadline')
    def __exit__(self,kind,*_):
        self.stop.set(); self.thread.join(5)
        if self.thread.is_alive(): raise RuntimeError('safety.sampler_residue')
        if kind is None: self.check()


def build_capture(path,*,frames=104857,channels=32,chunk_frames=819,gateway='GW',capture_id=None,channel_specs=None):
    """Nonflat deterministic synthetic source; caller prepares outside measured child."""
    import base64
    import struct
    from backend.waveforms.files import scan_file
    from tests.waveform_vectors import encoded
    if type(frames) is not int or not 1<=frames<=104857 or type(channels) is not int or not 1<=channels<=32:
        raise ValueError('Bounded source dimensions required')
    if type(chunk_frames) is not int or not 1<=chunk_frames<=131072//(channels*5): raise ValueError('Whole-frame chunk bound')
    capture_id=str(capture_id or uuid4()); boot=str(uuid4()); frame_bytes=channels*5; rate=2000
    channel_list=[dict(channel_id='c'+str(i),quantity='synthetic',unit='1',encoding='i32le',measurement_source='synthetic',
        signal_kind='instantaneous',config_id=None,calibration_id=None) for i in range(channels)]
    if channel_specs is not None:
        if len(channel_specs)!=channels: raise ValueError('Explicit channel layout mismatch')
        channel_list=channel_specs
    data=bytearray(frames*frame_bytes)
    buckets=min(500,max(1,frames//4))
    for index in range(frames):
        bucket=((index+1)*buckets-1)//frames
        first=bucket*frames//buckets
        value=50000 if index==first+1 else -50000 if index==first+2 else index%997
        for channel in range(channels): struct.pack_into('<iB',data,index*frame_bytes+channel*5,value+channel*13,0)
    stream=dict(stream_id='resource',source=dict(gateway_id=gateway,device_id='PUMP-001',boot_id=boot),clock_domain_id='synthetic-clock',
        rate_num=rate,rate_den=1,first_sample_index='0',offset_ns='0',frame_count=frames,frame_bytes=frame_bytes,
        chunk_frames=chunk_frames,chunk_count=(frames+chunk_frames-1)//chunk_frames,decoded_bytes=len(data),
        data_sha256=sha256(data).hexdigest(),channels=channel_list)
    common=dict(schema='OWP-WAVE/1.0',gateway_id=gateway,device_id='PUMP-001',capture_id=capture_id)
    manifest=encoded(dict(common,kind='manifest',boot_id=boot,capture_kind='event',source_kind='synthetic',
        producer=dict(manufacturer='Synthetic Task17',recorder_version='1'),source_evidence=None,
        utc_start='2026-10-01T00:00:00.000000Z',utc_uncertainty_ns='0',clock_domain_id='synthetic-clock',
        sync_evidence_id=None,window_ns=str(max(1,(frames-1)*1000000000//rate)),cycle=None,streams=[stream]))
    manifest_digest=sha256(manifest).hexdigest(); path=Path(path)
    with path.open('xb') as output:
        output.write(manifest+b'\n')
        for index,start in enumerate(range(0,frames,chunk_frames)):
            count=min(chunk_frames,frames-start); raw=data[start*frame_bytes:(start+count)*frame_bytes]
            output.write(encoded(dict(common,kind='chunk',stream_id='resource',chunk_index=index,sample_start=start,
                sample_count=count,manifest_sha256=manifest_digest,data_sha256=sha256(raw).hexdigest(),
                data_base64=base64.b64encode(raw).decode('ascii')))+b'\n')
    del data
    with path.open('rb') as source: scanned=scan_file(source)
    result=dict(path=str(path),frames=frames,channels=channels,decoded_bytes=stream['decoded_bytes'],
        chunks=stream['chunk_count'],raw_bytes=sum(line.length for line in scanned.lines),file_bytes=path.stat().st_size,
        manifest_sha256=manifest_digest,file_sha256=scanned.sha256,generator='nonflat-buckets/1',buckets=buckets)
    with path.with_name(path.name+'.input.json').open('x',encoding='utf-8') as output: json.dump(result,output,indent=2)
    return result


def offline_import(fixture,payloads,limits):
    from backend.waveforms.commands import Actor,Command,import_request_hash
    from backend.waveforms.imports import ImportService
    from backend.waveforms.files import scan_file
    content=b'\n'.join(payloads); scan=scan_file(BytesIO(content)); key=scan.manifest.key
    metadata=dict(file_sha256=sha256(content).hexdigest(),bytes=len(content),line_count=len(payloads),
        final_lf=False,gateway_id=key.gateway_id,device_id=key.device_id,capture_id=str(key.capture_id))
    service=ImportService(fixture.connect,limits=limits,prefix='oil')
    actor=Actor('local','task17-importer','Synthetic verification','operator')
    def command(op,target=None,version=0,**body):
        value=Command(actor,uuid4(),version,'Synthetic Task17 import','0'*64)
        return replace(value,request_hash=import_request_hash(op,target,value,**body))
    session=service.create(metadata,command('create',metadata=metadata))
    for index,payload in enumerate(payloads):
        session=service.append(session.import_id,index,payload,command('append',session.import_id,session.version,line=index,payload=payload))
    session=service.seal(session.import_id,command('seal',session.import_id,session.version))
    if session.state!='sealed': raise AssertionError('Offline import did not seal')
    return session


def protocol_roundtrip(fixture,stack,*,offline_first=False,payloads=None):
    from backend.waveforms.config import load_waveform_config
    from backend.waveforms.files import scan_file
    from backend.waveforms.contracts import WaveReceipt
    from backend.waveforms.sender_spool import WaveSenderSpool
    from backend.waveforms.sender import WaveSender
    from tests.test_waveform_repository import capture
    payloads=payloads or capture()
    content=b'\n'.join(payloads); scan=scan_file(BytesIO(content)); key=scan.manifest.key
    if offline_first: session=offline_import(fixture,payloads,stack.limits)
    stack.launch('receive'); stack.await_event('receive','subscribed',gateway=key.gateway_id)
    stack.launch('assemble'); stack.launch('publish')
    with patch.dict(os.environ,{'TEST_WAVEFORM_MQTT':stack.broker.password}):
        config=load_waveform_config(stack.sender_config(key.gateway_id),component='sender')
    with WaveSenderSpool.initialize(config.sender_spool.path,max_bytes=config.sender_spool.max_bytes) as spool:
        spool.bind(key.gateway_id,'oil'); spool.enqueue(BytesIO(content))
        sender=WaveSender(spool,next(g for g in config.gateways if g.gateway_id==key.gateway_id))
        try:
            deadline=time.monotonic()+25
            while time.monotonic()<deadline and spool.capture_state(key)!='archived':
                sender.run_once(); time.sleep(.05)
            if spool.capture_state(key)!='archived': raise AssertionError('Actual capture receipt did not archive sender')
            state=spool.capture_state(key)
        finally: sender.close()
    if not offline_first: session=offline_import(fixture,payloads,stack.limits)
    with fixture.connect('receiver') as connection:
        facts=connection.execute('SELECT raw_payload,payload_sha256 FROM waveforms.facts ORDER BY payload_sha256').fetchall()
        observations=connection.execute('SELECT DISTINCT transport,source_ref FROM waveforms.observations ORDER BY transport').fetchall()
        receipts=connection.execute('SELECT payload FROM waveforms.receipts').fetchall()
    original={sha256(raw).hexdigest():raw for raw in payloads}
    exact=len(facts)==len(original) and all(original.get(digest)==bytes(raw) for raw,digest in facts)
    parsed=[WaveReceipt.from_bytes(bytes(row[0])) for row in receipts]
    digests=all(receipt.manifest_sha256==scan.manifest_sha256 and
        (receipt.scope=='capture' or receipt.payload_sha256 in original) for receipt in parsed)
    if not exact or not digests or len(parsed)!=len(payloads)+1: raise AssertionError('Protocol byte/receipt mismatch')
    actual_capture=next(r for r in parsed if r.scope=='capture'); topic=f'oil/v2/{key.gateway_id}/{key.device_id}/waveform-ack'
    with WaveSenderSpool.initialize(stack.root/'negative-receipts.db',max_bytes=config.sender_spool.max_bytes) as negative:
        negative.bind(key.gateway_id,'oil'); negative.enqueue(BytesIO(content))
        if negative.apply_receipt(topic,replace(actual_capture,capture_id=uuid4()).to_bytes()): raise AssertionError('Unknown receipt accepted')
        if negative.apply_receipt(topic,replace(actual_capture,manifest_sha256='0'*64).to_bytes()): raise AssertionError('Wrong digest receipt accepted')
        if negative.capture_state(key)!='awaiting_capture_receipt': raise AssertionError('Invalid receipt released capture')
        if not negative.apply_receipt(topic,actual_capture.to_bytes()) or not negative.apply_receipt(topic,actual_capture.to_bytes()):
            raise AssertionError('Actual capture receipt duplicate unsafe')
        if negative.capture_state(key)!='archived': raise AssertionError('Actual receipt did not archive')
    return dict(facts=len(facts),transports=sorted({r[0] for r in observations}),sender_state=state,
        receipts=len(parsed),exact_bytes=exact,digests_match=digests,
        wrong_rejected=True,unknown_rejected=True,duplicate_safe=True,
        file_source_ref=str(next(r[1] for r in observations if r[0]=='file')),
        mqtt_source_ref=str(next(r[1] for r in observations if r[0]=='mqtt')),
        manifest_sha256=scan.manifest_sha256,file_sha256=sha256(content).hexdigest(),bytes=len(content))


def run_http_worker(config_path,stop,emit,*,fault=''):
    """Real HTTP handler in a fresh owned process; token never emitted to logs."""
    import threading
    from backend.http_server import create_server
    from backend.seed import bootstrap_local_auth
    from backend.waveforms.api import WaveformApi
    from backend.waveforms.windows import WaveformWindows,SummaryState
    from backend.waveforms.queries import WaveformQueries
    import re
    local=threading.local()
    dispatch=WaveformApi._dispatch; json_response=WaveformApi._json; headers=WaveformApi._headers
    def traced_dispatch(self,*args,**kwargs):
        record=dict(request_id=uuid4().hex,pid=os.getpid(),status=None,error_code=None,sample_passes=[])
        local.record=record; began=time.monotonic()
        emit('http_request_started',request_id=record['request_id'],pid=record['pid'])
        try: return dispatch(self,*args,**kwargs)
        finally:
            record['elapsed_seconds']=time.monotonic()-began
            emit('http_request_complete',**record); del local.record
    def traced_json(self,status,body):
        record=getattr(local,'record',None); began=time.monotonic()
        if record is not None:
            record['json_started']=began
            error=body.get('error') if isinstance(body,dict) else None
            code=error.get('code') if isinstance(error,dict) else None
            if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_.]{0,79}',code): record['error_code']=code
        try: return json_response(self,status,body)
        finally:
            if record is not None:
                record['json_including_output_seconds']=time.monotonic()-began
                record.pop('json_started',None)
    def traced_headers(self,status,length,*args,**kwargs):
        record=getattr(local,'record',None)
        if record is not None and 'json_started' in record:
            record['json_before_headers_seconds']=time.monotonic()-record['json_started']
        result=headers(self,status,length,*args,**kwargs)
        if record is not None: record.update(status=status,response_bytes=length)
        return result
    def span(cls,name,label):
        original=getattr(cls,name)
        def measured(self,*args,**kwargs):
            began=time.monotonic()
            try: return original(self,*args,**kwargs)
            finally:
                record=getattr(local,'record',None)
                if record is not None:
                    value=record.setdefault(label,dict(calls=0,elapsed_seconds=0.0))
                    value['calls']+=1; value['elapsed_seconds']+=time.monotonic()-began
        setattr(cls,name,measured)
    span(WaveformWindows,'prepare','prepare'); span(WaveformWindows,'recheck','recheck')
    span(SummaryState,'plan','summary_plan'); span(WaveformQueries,'raw','raw_queries')
    samples=WaveformWindows.samples
    def traced_samples(self,*args,**kwargs):
        record=getattr(local,'record',None); began=time.monotonic(); count=0
        try:
            for value in samples(self,*args,**kwargs):
                count+=1; yield value
        finally:
            if record is not None:
                record['sample_passes'].append(dict(pass_number=len(record['sample_passes'])+1,values=count,
                    inclusive_seconds=time.monotonic()-began))
    WaveformWindows.samples=traced_samples
    WaveformApi._dispatch=traced_dispatch; WaveformApi._json=traced_json; WaveformApi._headers=traced_headers
    root=Path(config_path).parent
    original=WaveformApi._write
    def traced_write(self,data,deadline=None):
        runtime=self.h.server.waveform_runtime
        with runtime.connections.condition: active=len(runtime.connections.active)
        with runtime.lock: owners=len(runtime.streams)
        identity=getattr(local,'record',{}).get('request_id')
        emit('http_write_started',bytes=len(data),db_active=active,output_owners=owners,request_id=identity)
        began=time.monotonic()
        if fault=='slow_http_progress':
            for start in range(0,len(data),16):
                original(self,data[start:start+16],deadline)
                emit('http_slow_progress',bytes=min(16,len(data)-start),request_id=identity)
                if stop.wait(.02): break
            result=None
        else: result=original(self,data,deadline)
        emit('http_write',bytes=len(data),elapsed_seconds=time.monotonic()-began,request_id=identity)
        return result
    WaveformApi._write=traced_write
    with patch('backend.http_server.seed_database',bootstrap_local_auth):
        server=create_server('127.0.0.1',0,root/'http-auth.db',waveform_config_path=config_path)
    runtime=server.waveform_runtime
    if runtime.status!='ready': raise AssertionError('Owned HTTP runtime unavailable')
    token=server.auth.login('administrator','Admin@2026').token
    with (root/'http-token').open('x',encoding='utf-8') as handle: handle.write(token)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.02},name='owned-http'); thread.start()
    if server.server_port in (1883,5432,3822,8765):
        server.shutdown(); thread.join(5); server.server_close(); raise ValueError('Protected HTTP listener port')
    emit('http_ready',port=server.server_port)
    try:
        while not stop.wait(.1):
            with runtime.lock: owners=len(runtime.streams)
            with runtime.connections.condition:
                active=len(runtime.connections.active); available=runtime.connections.available
            emit('http_resources',output_owners=owners,db_active=active,permits_available=available,
                 threads=len(threading.enumerate()))
    finally:
        began=time.monotonic(); server.shutdown(); thread.join(5); server.server_close()
        emit('stopped',elapsed_seconds=time.monotonic()-began,remaining_threads=[t.name for t in threading.enumerate() if t is not threading.current_thread() and not t.daemon],
            connections=len(runtime.connections.active),output_owners=len(runtime.streams))
    return 0


def run_sender_worker(config_path,stop,emit):
    from backend.waveforms.config import load_waveform_config
    from backend.waveforms.sender_spool import WaveSenderSpool
    from backend.waveforms.sender import WaveSender
    config=load_waveform_config(config_path,component='sender'); local=config.sender_spool
    with WaveSenderSpool.open(local.path,max_bytes=local.max_bytes,gateway_id=config.gateways[0].gateway_id,topic_prefix=config.topic_prefix) as spool:
        sender=WaveSender(spool,config.gateways[0],stop=stop)
        prepare=spool.prepare_batch
        operation_id=0
        def start(phase):
            nonlocal operation_id
            operation_id+=1; began=time.monotonic()
            emit('sender_phase_started',phase=phase,operation_id=operation_id,monotonic=began)
            return operation_id,began
        def complete(phase,identity,began):
            ended=time.monotonic()
            emit('sender_phase_complete',phase=phase,operation_id=identity,monotonic=ended,elapsed_seconds=ended-began)
            if ended-began>120: raise RuntimeError('safety.sender_operation_deadline')
        def measured_batch(*args,**kwargs):
            identity,began=start('prepare'); messages=prepare(*args,**kwargs)
            complete('prepare',identity,began)
            emit('sender_batch',captures=len({message.queue_id for message in messages}),messages=len(messages),
                elapsed_seconds=time.monotonic()-began)
            return messages
        spool.prepare_batch=measured_batch
        emit('sender_ready',status=spool.status())
        try:
            while not stop.is_set():
                identity,started=start('run_once'); acknowledged=sender.run_once()
                complete('run_once',identity,started)
                emit('sender_progress',acknowledged=acknowledged,elapsed_seconds=time.monotonic()-started,status=spool.status(),
                    pending_messages=spool._db.execute("SELECT count(*) FROM sender_message WHERE state='pending'").fetchone()[0])
                stop.wait(.1)
        finally: sender.close()
        emit('stopped',remaining_threads=[])
    return 0


def calibrated_channels(fixture,count=32):
    from backend.waveforms.evidence import EvidenceService
    from backend.waveforms.commands import Command,evidence_request_hash
    from tests.waveform_pairing_support import CREATOR,REVIEWER,config_body,calibration_body
    from tests.waveform_support import LIMITS
    service=EvidenceService(lambda:fixture.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:fixture.connect('reader'))
    def approved(kind,body):
        command=Command(CREATOR,uuid4(),0,'Synthetic resource reference','0'*64)
        value=service.create(kind,body,replace(command,request_hash=evidence_request_hash('create',None,command,kind=kind,body=body)))
        command=Command(REVIEWER,uuid4(),1,'Synthetic resource reference review','0'*64)
        return service.review(value.id,decision='approve',command=replace(command,request_hash=evidence_request_hash('review',value.id,command,decision='approve')))
    result=[]
    for index in range(count):
        name='c'+str(index)
        config=approved('sampling_config',config_body('synthetic','1',stream='resource',channel=name,rate=2000,device='PUMP-001'))
        cal=approved('calibration',calibration_body(config))
        result.append(dict(channel_id=name,quantity='synthetic',unit='1',encoding='i32le',measurement_source='synthetic',
            signal_kind='instantaneous',config_id='vendor-label',calibration_id=str(cal.id)))
    return result


def ingest_file(fixture,path,limits=None):
    from backend.waveforms.repository import WaveformRepository
    from backend.waveforms.assembly import Assembler
    from backend.waveforms.wire import parse_frame
    from tests.waveform_support import LIMITS
    from tests.test_waveform_repository import observation
    limits=limits or LIMITS; source_ref=uuid4(); began=time.monotonic()
    with Path(path).open('rb') as source:
        frame=parse_frame(source.readline().rstrip(b'\n')); source.seek(0)
        repo=WaveformRepository(fixture.connect,limits=limits,prefix='oil',ingress_gateway_id=frame.key.gateway_id)
        for line in source:
            if time.monotonic()-began>120: raise RuntimeError('safety.intake_deadline')
            result=repo.ingest(observation(line.rstrip(b'\n'),gateway=frame.key.gateway_id,
                topic=f'oil/v2/{frame.key.gateway_id}/{frame.key.device_id}/waveform',source_ref=source_ref))
            if result.status!='stored': raise AssertionError('Validated synthetic source was not stored')
    from datetime import datetime,timezone
    assembler=Assembler(lambda:fixture.connect('assembler'),limits=limits,clock=lambda:datetime.now(timezone.utc))
    assemblies=[]
    while True:
        lease=assembler.claim(uuid4())
        if lease is None: break
        started=time.monotonic()
        result=assembler.run(lease)
        if result.status!='complete': raise AssertionError('Synthetic source assembly failed')
        assemblies.append(dict(capture_pk=str(lease.capture_pk),elapsed_seconds=time.monotonic()-started))
    pk=fixture.scalar('SELECT capture_pk FROM waveforms.capture_metadata WHERE capture_id=%s',(frame.key.capture_id,))
    return frame,pk,dict(elapsed_seconds=time.monotonic()-began,source_ref=str(source_ref),assemblies=assemblies)
