"""Explicit serial synthetic experiments. Never invoked by import or discovery."""
import json
from pathlib import Path
import time
from uuid import uuid4

FAULTS=('receiver_precommit_kill','receiver_postcommit_kill','assembly_midread_kill',
    'publisher_postpublish_prefinish_kill','sender_kill','broker_restart',
    'dropped_receipt_publisher_link','stalled_gateway_reconnect','quota_gateway_reconnect_neighbor',
    'clock_rollback_assembly_restart','stale_lease_kill_reclaim','second_broker_restart')


class Progress:
    """Only completed real work advances cadence; idle time cannot be padded in."""
    def __init__(self,*,clock=time.monotonic):
        self.clock=clock; self.began=clock(); self.seconds=[]; self.messages=0; self.bytes=0

    def record(self,*,messages,bytes_):
        elapsed=self.clock()-self.began
        if type(messages) is not int or messages<1 or type(bytes_) is not int or bytes_<1:
            raise ValueError('Real positive work counts required')
        if (not self.seconds and elapsed>1) or (self.seconds and elapsed-self.seconds[-1]>90):
            raise RuntimeError('soak.cadence_gap')
        self.seconds.append(elapsed); self.messages+=messages; self.bytes+=bytes_
        return dict(monotonic_elapsed_seconds=elapsed,progress_seconds=list(self.seconds),messages=self.messages,bytes=self.bytes)


def save_result(root,name,value):
    path=Path(root)/(name+'.json')
    with path.open('x',encoding='utf-8') as output: json.dump(value,output,default=str,indent=2)
    return path.name


def record_recovery(root,value):
    """Progress is append-only; the completed/partial envelope is created once."""
    with (Path(root)/'recovery-progress.jsonl').open('a',encoding='utf-8') as output:
        output.write(json.dumps(value,default=str)+'\n')


def validate_completed_prefix(root,report):
    """Validate bound completed phase files, not the absent run1 scenarios array."""
    import math
    names={a['relative_path'] for a in report['artifacts']}
    def require(condition):
        if not condition: raise ValueError('Incomplete completed-prefix evidence')
    def positive(value): return type(value) in (int,float) and math.isfinite(value) and value>0
    def read(name,lines=False):
        require(name in names)
        text=(Path(root)/name).read_text(encoding='utf-8')
        return [json.loads(line) for line in text.splitlines()] if lines else json.loads(text)
    protocols=[read('ingress-'+gateway+'.json') for gateway in ('GW','A')]
    for p in protocols:
        require(p['facts']==2 and p['transports']==['file','mqtt'] and p['sender_state']=='archived' and p['receipts']==3 and
            all(p[k] is True for k in ('exact_bytes','digests_match','wrong_rejected','unknown_rejected','duplicate_safe')) and
            p['file_source_ref']!=p['mqtt_source_ref'] and positive(p['bytes']) and
            all(isinstance(p[k],str) and len(p[k])==64 for k in ('manifest_sha256','file_sha256')))
    transport=read('transport.json')
    require(transport['facts']==3 and all(transport[k] is True for k in ('cross_gateway_rejected','orphan_recovered','retained_rejected')) and
        sorted(transport['outcomes'])==[['A','quarantined','identity_mismatch'],['GW','quarantined','retained_publish'],['GW','stored','accepted'],['GW','stored','accepted']])
    ownership=[(str(Path(name).parent).replace('\\','/'),read(name)) for name in names if name.endswith('/processes.json')]
    cycles=[]
    for index,mechanism in enumerate(FAULTS):
        c=read('fault-'+str(index)+'.json'); cycles.append(c)
        expected_kind='transaction_fault' if index<2 else 'link_loss' if index in (5,7,8,11) else 'process_restart'
        require(c['index']==index and c['mechanism']==mechanism and c['kind']==expected_kind and c['instance_id'] and
            c['observed'] is True and c['recovered'] is True and c['cleanup']==dict(complete=True,residue=[]) and
            all(positive(c[k]) for k in ('facts','receipts','messages','bytes','duration_seconds')) and c['samples'] and c['storage'] and c['latencies_seconds'])
        require(c['events'] and all(e and e[-1]['event']=='stopped' and e[-1]['remaining_threads']==[] for e in c['events'].values()))
        for sample in c['samples']:
            require(sample['processes'] and all(positive(p['working_set_bytes']) and p['peak_working_set_bytes']>=p['working_set_bytes'] and
                positive(p['private_commit_bytes']) for p in sample['processes']))
        matches=[]
        for directory,processes in ownership:
            if (processes and all(p['run_id']==report['run_id'] and p['exit_code'] is not None for p in processes) and
                any(p['instance_id']==c['instance_id'] or ('pid' in c and p['pid']==c['pid']) for p in processes)):
                if all(directory+'/'+component+'.log' in names and read(directory+'/'+component+'.log',True)==events for component,events in c['events'].items()):
                    matches.append(directory)
        require(matches)
    require(cycles==report['fault_cycles'] and cycles[8]['quota_isolated'] is True)
    duration=report['duration']; points=read('soak-progress.jsonl',True); times=duration['progress_seconds']
    require(len(points)==len(times) and len(times)>1 and duration['monotonic_elapsed_seconds']>=1800 and duration['requested_seconds']>=1800 and
        times[0]<=1 and times[0]>=0 and all(positive(t) for t in times[1:]) and all(0<b-a<=90 for a,b in zip(times,times[1:])) and
        times[-1]<=duration['monotonic_elapsed_seconds']<=times[-1]+90)
    for index,p in enumerate(points):
        require(p['progress_seconds']==times[:index+1] and p['monotonic_elapsed_seconds']==times[index] and
            type(p['messages']) is int and type(p['bytes']) is int and positive(p['messages']) and positive(p['bytes']) and
            (index==0 or (p['messages']>points[index-1]['messages'] and p['bytes']>points[index-1]['bytes'])))
    require(all(points[-1][k]==duration[k]==sum(c[k] for c in cycles) for k in ('messages','bytes')))
    scenarios=[]
    for name,refs in (('online_then_offline',['ingress-GW.json']),('offline_then_online',['ingress-A.json']),
        ('receipt_chain',['ingress-GW.json','ingress-A.json']),('transport_judgments',['transport.json']),
        ('gateway_isolation',['fault-8.json']),('lease_recovery',['fault-2.json','fault-9.json','fault-10.json'])):
        scenarios.append(dict(id=name,status='passed',assertions=1,artifact_refs=['prior-run1/'+r for r in refs],segment_run_id=report['run_id']))
    return dict(duration=duration,fault_cycles=cycles,scenarios=scenarios,
        ingress=dict(online=True,offline=True,same_bytes=True,distinct_associations=True),quota=dict(isolated=True),
        receipts=dict(actual_protocol=True,published=6,parsed=6,matched=6,wrong_rejected=True,unknown_rejected=True,duplicate_safe=True,lost_recovered=True),
        integrity=dict(exact_bytes=True,digests_match=True,duplicates=0),backlog=dict(final=0))


def validate_completed_resources(root,report):
    """Read only the hash-bound run3 middle; never infer lost/unfinished phases."""
    import math
    from hashlib import sha256
    from tests.waveform_measurements import safety_check
    from tests.waveform_resource_scenarios import storage_check
    root=Path(root); items={a['relative_path']:a for a in report['artifacts']}
    def require(condition):
        if not condition: raise ValueError('Incomplete middle resource evidence')
    def number(value,low=0,high=float('inf')):
        return type(value) in (int,float) and math.isfinite(value) and low<=value<=high
    def read(name,lines=False):
        require(name in items)
        text=(root/name).read_text(encoding='utf-8')
        return [json.loads(line) for line in text.splitlines()] if lines else json.loads(text)
    def relative(value):
        path=Path(value)
        require(path.is_absolute() and path.resolve()==path and path.is_relative_to(root))
        name=path.relative_to(root).as_posix(); require(name in items)
        return name
    def memory(sample):
        require(number(sample['working_set_bytes'],1) and number(sample['private_commit_bytes'],1) and
            number(sample['peak_working_set_bytes'],sample['working_set_bytes'],1.5*1024**3))
    ownership=[]
    try:
        for name in items:
            if name.endswith('/processes.json') and not name.startswith('prior-run1/'):
                ownership.append((str(Path(name).parent).replace('\\','/'),read(name)))
        def envelope(value,sender=False):
            require(type(value) is dict and value['status']=='passed' and read(relative(value['envelope_path']))==value)
            require(number(value['elapsed_seconds'],0,120*value['captures'] if sender else 120) and value['samples'])
            for s in value['samples']: memory(s)
            cleanup=value['cleanup'] if sender else value['owned_cleanup']
            require(cleanup['complete'] is True and cleanup['processes'])
            processes=cleanup['processes']
            require(all(type(p['pid']) is int and p['pid']>0 and p['instance_id'] and
                type(p['exit_code']) is int and (p['component']=='broker' or p['exit_code']==0) for p in processes))
            matches=[(directory,owned) for directory,owned in ownership if
                all(p['run_id']==report['run_id'] for p in owned) and
                all(any(all(q[k]==v for k,v in p.items()) for q in owned) for p in processes)]
            require(len(matches)==1)
            component='sender_measure' if sender else 'resource_measure' if value['kind']=='resource' else 'http_measure'
            events=value['events']; require(read(matches[0][0]+'/'+component+'.log',True)==events)
            require(events[-1]['event']=='stopped' and events[-1]['remaining_threads']==[])
            rows=read(relative(value['samples_path']),True); require(rows)
            for row in rows:
                owned=row['owned'] if sender else row['processes']; require(owned)
                for s in owned: memory(s)
                safety_check(row['host'],owned)
            if sender:
                require([row['memory'] for row in rows]==value['samples'] and [row['physical'] for row in rows]==value['physical'])
                require(all(number(p['database_bytes'],1) and number(p['journal_bytes']) for p in value['physical']))
            else:
                phases=read(relative(value['phases_path']),True)
                require(phases==[dict(component=component,**e) for e in events])
                if value['kind']=='output_owners': require(rows==value['continuous_samples'])
                else: require([s for row in rows for s in row['processes']]==value['samples'])
                if component=='http_measure':
                    require(all(value['cleanup'][k]==v for k,v in dict(event='stopped',remaining_threads=[],connections=0,output_owners=0).items()))
            return events
        maximum=read('maximum.json'); xy=read('xy.json'); audit=read('audit.json')
        inputs=maximum['inputs']; require(len(inputs)==3 and len({v['path'] for v in inputs})==3)
        for v in inputs:
            name=relative(v['path']); require(read(name+'.input.json')==v)
            require(all(v[k]==n for k,n in dict(frames=104857,channels=32,decoded_bytes=16777120,chunks=129,buckets=500,generator='nonflat-buckets/1').items()))
            require(v['file_bytes']==items[name]['size'] and v['file_sha256']==items[name]['sha256'])
            with (root/name).open('rb') as source: line=source.readline(131073).rstrip(b'\r\n')
            manifest=json.loads(line); require(manifest['kind']=='manifest' and manifest['schema']=='OWP-WAVE/1.0' and len(manifest['streams'])==1)
            stream=manifest['streams'][0]
            require(sha256(line).hexdigest()==v['manifest_sha256'] and all(stream[k]==n for k,n in
                dict(frame_count=104857,frame_bytes=160,chunk_frames=819,chunk_count=129,decoded_bytes=16777120).items()))
            require(len(stream['channels'])==32 and all(c['channel_id']=='c'+str(i) and c['encoding']=='i32le' for i,c in enumerate(stream['channels'])))
        for key,count in (('sender_one',1),('sender_three',3)):
            v=maximum[key]; events=envelope(v,True)
            require(v['captures']==count and v['bounds']['operation_seconds']==120 and v['bounds']['aggregate_seconds']==120*count and
                v['durability']['journal_mode']=='delete' and v['durability']['synchronous']>=2 and v['exit_code']==0 and
                v['logical_status']['captures']==count and v['logical_status']['retained_capture_bytes']==sum(i['file_bytes'] for i in inputs[:count]) and
                0<v['logical_status']['charged_bytes']<=v['logical_status']['max_bytes']==134217728 and v['diagnostic_events']<=128)
            started={e['operation_id']:e for e in events if e['event']=='sender_phase_started'}
            ended={e['operation_id']:e for e in events if e['event']=='sender_phase_complete'}
            require(started and set(started)==set(ended) and {e['phase'] for e in started.values()}=={'prepare','run_once'})
            for key,start in started.items():
                end=ended[key]; require(end['phase']==start['phase'] and number(end['elapsed_seconds'],0,120) and number(end['monotonic']-start['monotonic'],0,120))
            progress=[e for e in events if e['event']=='sender_progress']; require(progress and progress[-1]['pending_messages']==0)
            require(progress[-1]['status']['captures']==count and progress[-1]['status']['states']=={'awaiting_capture_receipt':count})
            require(any(e['event']=='sender_batch' and 1<=e['captures']<=count for e in events))
            require(all(w['exit_code']==0 and w['stopped']['remaining_threads']==[] for w in v['workers'].values()))
        envelope(maximum['intake']); assemblies=maximum['intake']['result']['assemblies']
        require(len(assemblies)==3 and len({a['capture_pk'] for a in assemblies})==3 and
            all(number(a['elapsed_seconds'],0,120) for a in assemblies) and maximum['lease_comparison']==dict(lease_seconds=30,cumulative_cap_seconds=300,assemblies=assemblies))
        def http(v,points,query):
            events=envelope(v)
            require(v['kind']=='http' and v['points']==points and number(v['bytes'],1) and v['exit_code']==0 and query in v['metadata']['route'])
            completed=[e for e in events if e['event']=='http_request_complete']
            require(len(completed)==1 and completed[0]['status']==200 and completed[0]['error_code'] is None and number(completed[0]['elapsed_seconds'],0,120))
        for key,points,count in (('raw',640000,20000),('summary',64000,104857)):
            v=maximum[key]; http(v,points,f'count={count}&mode={key}'+('&budget=2000' if key=='summary' else ''))
            require(v['channels']==32 and v['metadata']['expected_points']==points and len(v['sample_witnesses'])==32)
            for index,witness in enumerate(v['sample_witnesses']):
                require(witness['channel_id']=='c'+str(index) and witness['count']==points//32 and
                    witness['minimum']==-50000+index*13 and witness['maximum']==50000+index*13)
        owners=maximum['output_owners']; envelope(owners)
        require(owners['healthy'] is True and owners['reset'] is True and owners['healthy_bytes']==owners['half_close_bytes']==maximum['raw']['bytes'])
        storage_check(maximum['storage_before'],maximum['storage_after'])
        http(xy['raw'],20000,'count=20000&mode=raw')
        require(number(xy['summary']['points'],1,2000)); http(xy['summary'],xy['summary']['points'],'count=20001&mode=summary&budget=2000')
        require(len(audit['levels'])==3 and audit['timeout_seconds']==3)
        for level,count in zip(audit['levels'],(100,1000,10000)):
            v=level['response']; http(v,0,'page_size=100')
            require(level['target_rows']==level['actual_rows']==count and level['stale_page_rejected'] is True and
                v['items']==100 and v['snapshot_revision'] and v['elapsed_seconds']<=3)
        scenarios=[dict(id=name,status='passed',assertions=1,artifact_refs=['prior-run3/'+ref],segment_run_id=report['run_id'])
            for name,ref in [(name,'maximum.json') for name in ('sender_storage','sender_maximum','raw_maximum','summary_maximum','output_owners')]+[('xy','xy.json'),('audit_volume','audit.json')]]
        return dict(scenarios=scenarios)
    except (KeyError,TypeError,IndexError,OSError,json.JSONDecodeError) as error:
        raise ValueError('Malformed middle resource evidence') from error
    except RuntimeError as error:
        # The existing pure safety/storage guards use RuntimeError for limits.
        raise ValueError('Middle resource limit violation') from error


def fault_cycle(fixture,root,index,*,tail_seconds=0,progress=None):
    """One real fault plus bounded periodic work; no timing-only success flags."""
    from dataclasses import replace
    from hashlib import sha256
    from io import BytesIO
    from backend.waveforms.sender_spool import WaveSenderSpool
    from backend.waveforms.contracts import WaveReceipt
    from tests.test_waveform_repository import capture
    from tests.waveform_stack import Stack
    from tests.waveform_support import LIMITS
    from tests.waveform_measurements import process_memory,host_resources,safety_check
    from tests.waveform_resource_scenarios import storage_sample,storage_check
    if type(index) is not int or not 0<=index<len(FAULTS): raise ValueError('Declared fault index required')
    name=FAULTS[index]; limits=replace(LIMITS,gateways=dict(GW=LIMITS.gateways['GW'],A=replace(LIMITS.gateways['GW'],incomplete_captures=1)))
    began=time.monotonic(); result=dict(index=index,mechanism=name,observed=False,recovered=False)
    expected={}; samples=[]; latencies=[]; storage=[]; messages=0; wirebytes=0
    with fixture.capacity_fixture(limits), Stack(fixture,limits=limits,evidence_root=root) as stack:
        initial=storage_sample(fixture,root)
        raw,chunk=capture(); payloads=(raw,chunk)
        def send(values=payloads,gateway='GW'):
            nonlocal messages,wirebytes
            start=time.monotonic()
            for payload in values:
                if stack.publish(gateway,payload)!=0: raise AssertionError('Fault experiment broker rejected source')
                expected[sha256(payload).hexdigest()]=payload; messages+=1; wirebytes+=len(payload)
            latencies.append(time.monotonic()-start)
            if progress: progress(len(values),sum(map(len,values)))
        def start_receive(fault=''):
            stack.launch('receive',fault=fault)
            stack.await_event('receive','subscribed',gateway='GW'); stack.await_event('receive','subscribed',gateway='A')
        def broker_restart():
            old=stack.broker.process; marker=uuid4().hex; before=len(stack.events('receive','subscribed',gateway='GW'))
            stack.broker.stop()
            if old.poll() is None: raise AssertionError('Owned broker did not exit')
            result.update(instance_id=marker,pid=old.pid,exit_code=old.returncode,kind='link_loss',observed=True)
            stack.broker.start()
            return before
        def kill(component):
            result.update(stack.kill_owned(component,reason=name),kind='transaction_fault' if index<2 else 'process_restart',observed=True)
        def expire(table):
            # Test fixture-only expiry after exact owned process death, not wall-clock or durability changes.
            with fixture.connect('migration_owner') as connection:
                connection.execute('UPDATE waveforms.'+table+" SET lease_until=clock_timestamp()-interval '1 second' WHERE lease_until IS NOT NULL")
        if index<2:
            start_receive('pause_before_commit' if index==0 else 'pause_after_commit'); send((raw,))
            stack.await_event('receive','transaction_paused')
            if fixture.scalar('SELECT count(*) FROM waveforms.facts',role='receiver')!=index: raise AssertionError('Transaction uncertainty state')
            kill('receive'); start_receive(); stack.reset_peer('GW'); send()
        elif index in (2,10):
            start_receive(); send(); stack.await_count('facts',2)
            stack.launch('assemble',fault='pause_assembly'); stack.await_event('assemble','assembly_paused')
            with fixture.connect('assembler') as connection:
                lease_before=connection.execute('SELECT lease_token,lease_generation FROM waveforms.assembly_jobs WHERE lease_until IS NOT NULL').fetchall()
            if not lease_before: raise AssertionError('Missing live assembly lease')
            kill('assemble'); expire('assembly_jobs'); stack.launch('assemble')
            result['lease_before']=[tuple(map(str,row)) for row in lease_before]
        elif index in (3,6):
            start_receive(); send(); stack.await_count('facts',2); stack.launch('assemble'); stack.await_count('completion',1,role='assembler')
            stack.launch('publish',fault='pause_publish_finish' if index==3 else 'drop_publish_ack')
            stack.await_event('publish','publisher_finish_paused' if index==3 else 'publish_ack_dropped')
            if fixture.scalar('SELECT count(*) FROM waveforms.deliveries WHERE requested_generation>sent_generation',role='receipt_publisher')<1:
                raise AssertionError('No observed unfinished receipt')
            kill('publish'); expire('deliveries'); stack.launch('publish')
        elif index in (4,11):
            start_receive()
            path=stack.root/'soak-sender.db'; budget=16*1024**2
            stack.sections['sender_spool']=dict(path=str(path),max_bytes=budget); stack.save_config()
            with WaveSenderSpool.initialize(path,max_bytes=budget) as spool:
                spool.bind('GW','oil'); spool.enqueue(BytesIO(b'\n'.join(payloads)))
            sender_config=stack.sender_config('GW')
            stack.launch('sender_measure',config=sender_config); stack.await_event('sender_measure','sender_progress'); stack.await_count('facts',2)
            expected.update({sha256(p).hexdigest():p for p in payloads}); messages+=2; wirebytes+=sum(map(len,payloads))
            if progress: progress(2,sum(map(len,payloads)))
            if index==4: kill('sender_measure'); stack.launch('sender_measure',config=sender_config)
            else:
                before=broker_restart(); stack.await_condition(lambda:len(stack.events('receive','subscribed',gateway='GW'))>before,'receiver reconnect')
        elif index==7:
            start_receive('pause_deliveries'); send((raw,)); stack.await_event('receive','delivery_paused')
            before=broker_restart(); stack.command('receive','release')
            stack.await_condition(lambda:len(stack.events('receive','subscribed',gateway='GW'))>before,'stalled callback reconnect')
            # Complete the old transaction, then replace the injection worker using its private lifecycle.
            stack.await_count('facts',1); stack.stop('receive'); start_receive(); stack.reset_peer('GW'); send()
        elif index==8:
            start_receive(); blocked=capture('A')[0]; send((blocked,),gateway='A'); stack.await_count('facts',1)
            stack.publish('A',capture('A')[0]); stack.await_event('receive','retry',gateway='A')
            send(); stack.await_count('facts',3)
            if len(stack.events('receive','subscribed',gateway='GW'))!=1: raise AssertionError('Healthy neighbor disconnected')
            result.update(instance_id=stack.instances[-1]['instance_id'],kind='link_loss',observed=True,quota_isolated=True)
        elif index==9:
            start_receive(); stack.launch('assemble'); stack.await_event('assemble','idle')
            stack.stop('assemble'); send(); stack.await_count('facts',2)
            stack.launch('assemble',fault='clock_rollback')
            stack.await_condition(lambda:fixture.scalar("SELECT count(*) FROM waveforms.worker_health WHERE component='assembler' AND error_code='clock_rollback'",role='assembler')>0,'actual rollback highwater')
            kill('assemble'); stack.launch('assemble')
        else:
            start_receive(); before=broker_restart()
            stack.await_condition(lambda:len(stack.events('receive','subscribed',gateway='GW'))>before,'receiver reconnect'); send()
        for component in ('assemble','publish'):
            if component not in stack.processes: stack.launch(component)
        stack.await_condition(lambda:fixture.scalar("SELECT count(*) FROM waveforms.completion WHERE status='complete'",role='assembler')>=1,'recovered completion')
        stack.await_condition(lambda:fixture.scalar('SELECT count(*) FROM waveforms.deliveries WHERE requested_generation>sent_generation',role='receipt_publisher')==0,'recovered receipt backlog')
        if 'sender_measure' in stack.processes:
            stack.await_condition(lambda:any(e['status']['states'].get('archived')==1 for e in stack.events('sender_measure','sender_progress')),'sender replay archived')
        # Do not create new A work after its intentional incomplete-capacity fault.
        next_send=time.monotonic(); until=began+tail_seconds
        while time.monotonic()<until:
            if time.monotonic()>=next_send:
                send(capture()); next_send+=5
            owned=[process_memory(p) for p in [stack.broker.process,*stack.processes.values()] if p.poll() is None]
            samples.append(dict(monotonic=time.monotonic(),processes=owned)); safety_check(host_resources(root),owned)
            measurement=storage_sample(fixture,root); storage_check(initial,measurement); storage.append(measurement)
            time.sleep(.5)
        stack.await_condition(lambda:fixture.scalar('SELECT count(*) FROM waveforms.deliveries WHERE requested_generation>sent_generation',role='receipt_publisher')==0,'final receipt backlog')
        with fixture.connect('receiver') as connection:
            rows=connection.execute('SELECT payload_sha256,raw_payload FROM waveforms.facts').fetchall()
            receipts=connection.execute('SELECT payload FROM waveforms.receipts').fetchall()
        if len(rows)!=len(expected) or any(expected.get(digest)!=bytes(payload) for digest,payload in rows): raise AssertionError('Fault replay changed exact facts')
        parsed=[WaveReceipt.from_bytes(bytes(row[0])) for row in receipts]
        if not parsed: raise AssertionError('No real fault recovery protocol receipts')
        for component in stack.processes: stack.stop(component)
        cleanup=[]
        for component,process in stack.processes.items():
            final=stack.events(component,'stopped',current=True)
            if process.returncode!=0 or not final or final[-1].get('remaining_threads'): cleanup.append(component)
        if cleanup: raise AssertionError('Fault worker shutdown residue')
        result.update(recovered=True,facts=len(rows),receipts=len(parsed),messages=messages,bytes=wirebytes,
            samples=samples,storage=storage,latencies_seconds=latencies,cleanup=dict(complete=True,residue=[]),
            events={name:stack.events(name) for name in stack.processes},duration_seconds=time.monotonic()-began)
        save_result(root,'fault-'+str(index),result)
    return result


def prepare_recovery(fixture,root,*,volumes=(1,100,1000),deadline=None,run_id=None,source_sha256=None):
    """Populated final source remains live until consistent backup completes."""
    if (type(volumes) not in (tuple,list) or not volumes or
        any(type(count) is not int or not 1<=count<=1000 for count in volumes) or
        any(left>=right for left,right in zip(volumes,volumes[1:]))):
        raise ValueError('Strictly ascending bounded recovery volumes required')
    from tests.waveform_resource_scenarios import RecoveryBudget,RecoveryStorage,validate_recovery_measurement
    budget=RecoveryBudget(until=deadline.until if deadline else None)
    budget.preparation.check(root,'recovery-start')
    import re
    if not isinstance(run_id,str) or not re.fullmatch('[0-9a-f]{32}',run_id) or not isinstance(source_sha256,str) or not re.fullmatch('[0-9a-f]{64}',source_sha256):
        raise ValueError('Explicit recovery run/source identity required')
    measurement=dict(schema='waveform.recovery-downloads/2',policy='R88-two-stage',status='partial',run_id=run_id,source_sha256=source_sha256,
        volumes=list(volumes),prepared=[],downloads=[],owned_cleanup=False)
    try:
        monitor=RecoveryStorage(fixture,root,deadline=budget.preparation,outer=budget.outer)
        budget.preparation.storage_guard=monitor
        with monitor:
            result=_prepare_recovery(fixture,root,volumes,budget,run_id,source_sha256,measurement)
            budget.finish(root)
        if not monitor.closure(): raise RuntimeError('safety.storage_monitor_residue')
        clock=budget.download.check(root,'recovery-clean-complete')
        measurement['clock']['completed']=clock['monotonic']
        measurement['monitor_cleanup']=True
        measurement['status']='passed'
        validate_recovery_measurement(measurement,run_id=run_id,source_sha256=source_sha256,volumes=volumes)
        save_result(root,'recovery-downloads',measurement)
        result.update(references={p['version_id']:p['export_root'] for p in measurement['prepared']},
            preparation_clock=clock,recovery_downloads=measurement)
        for count,measured in zip(volumes,result['volumes']): save_result(root,'dataset-'+str(count),measured)
        first=measurement['prepared'][0]
        result['source_exports']=[dict(version_id=first['version_id'],export_id=first['export_id'],files=result.pop('prelude_download')['result']['file_inventory'])]
        return result
    except BaseException as error:
        measurement['status']='partial'
        try:
            record_recovery(root,measurement)
            if not (root/'recovery-downloads.json').exists(): save_result(root,'recovery-downloads',measurement)
        except BaseException as cleanup: error.add_note('Recovery retention failure: '+type(cleanup).__name__)
        try: (budget.download or budget.preparation).record(root,'recovery-abort')
        except BaseException as cleanup: error.add_note('Clock retention failure: '+type(cleanup).__name__)
        raise


def _prepare_recovery(fixture,root,volumes,budget,run_id,source_sha256,measurement):
    from dataclasses import asdict,replace
    from uuid import UUID
    from backend.waveforms.commands import Command,export_request_hash
    from backend.waveforms.export import DatasetExporter
    from backend.waveforms.errors import WaveformError
    from tests.waveform_pairing_support import REVIEWER
    from tests.waveform_support import LIMITS
    from tests.waveform_resource_scenarios import prepare_members,measured_dataset,measured_job,prepared_export,validate_recovery_measurement,RecoveryStorage
    deadline=budget.preparation
    record_recovery(root,measurement)
    deadline.check(root,'frozen-prelude')
    service,version,vector=fixture.frozen(two=True)
    exporter=DatasetExporter(lambda:fixture.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:fixture.connect('reader'))
    def export(intent):
        deadline.check(root,'prelude-export-'+intent)
        body=dict(version_id=str(version.id),purpose=intent); command=Command(REVIEWER,uuid4(),0,'Synthetic recovery prior export','0'*64)
        return exporter.create(body,replace(command,request_hash=export_request_hash(version.id,command,body=body)))
    research=export('research'); evidence=export('evidence')
    deadline.check(root,'prelude-revoke')
    calibration=UUID(vector['selection']['channels'][-1]['calibration']['id'])
    vector['evidence_service'].revoke(calibration,vector['evidence_command']('revoke',calibration,2))
    deadline.check(root,'prelude-restriction')
    try: exporter.open(research.id,actor=REVIEWER)
    except WaveformError as error:
        if error.code!='waveform.dataset_export_denied': raise
    else: raise AssertionError('Source revoked export remained permitted')
    records=[dict(version_id=str(version.id),export_id=str(evidence.id),actor=asdict(REVIEWER))]
    first=prepared_export(exporter,evidence,version,root/'source-exports'/str(version.id),members=version.member_count)
    originals={}
    from backend.waveforms.wire import parse_frame
    for index,lines in enumerate(vector['wire_captures']):
        path=root/('prelude-original-'+str(index)+'.owpwave'); path.write_bytes(b'\n'.join(lines)+b'\n')
        key=parse_frame(lines[0]).key; originals[(key.gateway_id,key.device_id,str(key.capture_id))]=str(path)
    measurement['prepared'].append(first)
    def progress(count,seconds):
        with (root/'population.jsonl').open('a',encoding='utf-8') as output:
            output.write(json.dumps(dict(count=count,elapsed_seconds=seconds))+'\n')
    members=prepare_members(fixture,root,count=max(volumes),progress=progress,deadline=deadline)
    for index in range(max(volumes)):
        path=root/('reference-'+str(index)+'.owpwave')
        with path.open('rb') as handle: key=parse_frame(handle.readline().rstrip(b'\n')).key
        originals[(key.gateway_id,key.device_id,str(key.capture_id))]=str(path)
    measured_volumes=[]
    for count in volumes:
        deadline.check(root,'dataset-'+str(count))
        measured=measured_dataset(fixture,root,members[:count],deadline=deadline,stage='prepare'); measured_volumes.append(measured)
        value=measured['result']; records.append(dict(version_id=value['version_id'],export_id=value['export_id'],actor=asdict(REVIEWER)))
        measurement['prepared'].append(value); save_result(root,'dataset-prepared-'+str(count),measured); record_recovery(root,measurement)
    download=budget.start_download(root)
    measurement['clock']=dict(began=budget.began,outer=budget.outer,preparation_deadline=deadline.until,
        prepared=budget.prepared['monotonic'],download_started=download.began,download_deadline=download.until)
    downloads=[]
    try:
        for prepared in measurement['prepared']:
            prepared.update(run_id=run_id,source_sha256=source_sha256)
            for file in prepared['expected']:
                if 'key' in file:
                    key=file['key']; file['original_path']=originals[(key['gateway_id'],key['device_id'],key['capture_id'])]
            record_recovery(root,measurement)
            measured=measured_job(fixture,root,dict(kind='download',prepared=prepared),deadline=download); downloads.append(measured)
            for file in measured['result']['downloads']:
                file['relative_path']=str((Path(prepared['export_root'])/file['name']).relative_to(root)).replace('\\','/')
                measurement['downloads'].append(file)
            record_recovery(root,measurement)
        measurement['owned_cleanup']=all(m['owned_cleanup']['complete'] for m in [*measured_volumes,*downloads])
    except BaseException as error:
        measurement['status']='partial'
        try: record_recovery(root,measurement)
        except BaseException as cleanup: error.add_note('Recovery retention failure: '+type(cleanup).__name__)
        raise
    return dict(records=records,volumes=downloads[1:],prelude_download=downloads[0],
        restriction=dict(version_id=str(version.id),research_export_id=str(research.id),evidence_export_id=str(evidence.id),actor=asdict(REVIEWER),code='waveform.dataset_export_denied'))


def transport_scenario(fixture,root,limits):
    from tests.waveform_stack import Stack
    from tests.test_waveform_repository import capture
    from hashlib import sha256
    with fixture.capacity_fixture(limits), Stack(fixture,limits=limits,evidence_root=root) as stack:
        stack.launch('receive'); stack.await_event('receive','subscribed',gateway='GW'); stack.await_event('receive','subscribed',gateway='A')
        manifest,chunk=capture()
        stack.publish('GW',manifest,retain=True); stack.await_count('message_outcomes',1)
        stack.publish('GW',chunk); stack.await_count('message_outcomes',2)
        if fixture.scalar("SELECT used_orphan_bytes FROM waveforms.capacity_metadata WHERE bucket='GW'")<=0: raise AssertionError('Orphan was not accounted')
        stack.publish('GW',manifest); stack.publish('A',manifest)
        stack.await_count('message_outcomes',4)
        stack.launch('assemble'); stack.await_count('completion',1,role='assembler')
        with fixture.connect('receiver') as conn:
            outcomes=conn.execute('SELECT intake_gateway_id,status,reason_code FROM waveforms.message_outcomes ORDER BY intake_gateway_id,status,reason_code').fetchall()
            facts=conn.execute('SELECT intake_gateway_id,payload_sha256,raw_payload FROM waveforms.facts').fetchall()
        if ('GW','quarantined','retained_publish') not in outcomes or ('A','quarantined','identity_mismatch') not in outcomes:
            raise AssertionError('Per-transport/gateway judgment not preserved')
        originals={sha256(p).hexdigest():p for p in (manifest,chunk)}
        if len(facts)!=3 or any(originals.get(digest)!=bytes(raw) for gateway,digest,raw in facts): raise AssertionError('Transport exact facts')
        return dict(outcomes=outcomes,facts=len(facts),cross_gateway_rejected=True,orphan_recovered=True,retained_rejected=True)


def run_retained_restore(root,run_id,config_path,*,source_sha256,prefix,resources_prefix):
    """Only fresh target work; source preparation and historical runs stay intact."""
    from tools.waveform_restore import validate_retained_copy,load_source_authority,RetainedSource,restore_retained,digest_file
    from tests.waveform_measurements import host_resources,safety_check
    root=Path(root); result=dict(prefix); source=None; first=None
    try:
        bundle=validate_retained_copy(root,source_sha256)
        host=host_resources(root); safety_check(host,[],preflight=True)
        authority=load_source_authority(config_path,resolve=True)
        source=RetainedSource(authority,authority['raw_config'],run_id,bundle['fingerprint'])
        result['identity']=dict(verified=True,source_database='oilwell_waveforms_test',run_id=run_id)
        result['environment']=dict(ready=True,host=host)
        result['lease_measurement']=bundle['report']['lease_measurement']
        result['recovery_downloads']=bundle['report']['recovery_downloads']
        result['scenarios']=[*prefix['scenarios'],*resources_prefix['scenarios'],
            dict(id='dataset_1000',status='passed',assertions=9,segment_run_id=bundle['report']['run_id'],
                artifact_refs=['prior-r90/dataset-1.json','prior-r90/dataset-100.json','prior-r90/dataset-1000.json'])]
        prepared=dict(bundle['recovery'],references=bundle['references'])
        with source:
            save_result(root,'retained-source-live-check',dict(matched=True,before=source.before))
            target,recovery=restore_retained(source,root,run_id)
            result['restore']=finish_target_recovery(source,root,run_id,source_sha256,target,recovery,prepared,bundle['fingerprint']['content'],result)
        result['resources']=dict(measured=True,aborts=[],host=host,scope='authenticated_historical_and_fresh_target')
        result['limits']=['Four distinct source/run segments; historical R90 target suffix failed host reserve and remains partial',
            'Historical soak/maximum/source downloads are not final-source measurements; synthetic local evidence only']
    except BaseException as error: first=error
    finally:
        receipt=source.receipt if source is not None else dict(complete=False,errors=['source_not_entered'])
        result['cleanup']=dict(complete=receipt['complete'],residue=[] if receipt['complete'] else ['retained_source_cleanup'],source_guard=receipt)
        for name,value in (('retained-source-cleanup',receipt),('experiment-partial',result)):
            try: save_result(root,name,value)
            except BaseException as error:
                if first is None: first=error
        try:
            result['artifacts']=[dict(relative_path=p.relative_to(root).as_posix(),size=p.stat().st_size,sha256=digest_file(p))
                for p in sorted(root.rglob('*')) if p.is_file()]
        except BaseException as error:
            if first is None: first=error
    if first is not None: raise first
    if not result['cleanup']['complete']: raise ValueError('Retained source cleanup incomplete')
    return result


def finish_target_recovery(fixture,root,run_id,source_sha256,target,recovery,prepared,baseline,result):
    """One shared audit/export/restriction path for normal and retained restores."""
    from uuid import UUID
    from backend.waveforms.commands import Actor
    from backend.waveforms.export import DatasetExporter
    from backend.waveforms.errors import WaveformError
    from tests.waveform_support import LIMITS
    from tools.waveform_restore import verify_export_deltas
    from tests.waveform_resource_scenarios import supervised_target_exports
    def execute_restored_exports(_connect,records,destination,*,limits,reference):
        if Path(destination)!=root/'restored-exports' or records!=prepared['records'] or reference!=prepared['references']:
            raise ValueError('Restored export execution binding differs')
        supervised=supervised_target_exports(fixture,root,run_id,source_sha256,records,reference)
        result['target_export_supervision']=supervised
        save_result(root,'target-export-supervision',supervised)
        return supervised['result']['exports']
    delta=verify_export_deltas(target,baseline,prepared['records'],root/'restored-exports',limits=LIMITS,references=prepared['references'],_execute=execute_restored_exports)
    restriction=prepared['restriction']; actor=Actor(**restriction['actor'])
    exporter=DatasetExporter(lambda:target.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:target.connect('reader'))
    try: exporter.open(UUID(restriction['research_export_id']),actor=actor)
    except WaveformError as error:
        if error.code!=restriction['code']: raise
    else: raise AssertionError('Restored restriction bypassed')
    if exporter.describe(UUID(restriction['version_id']),purpose='evidence',actor=actor).restriction_state!='restricted': raise AssertionError('Restored restriction lost')
    recovery.update(exports=True,restrictions=True,deltas=delta,export_supervision=result['target_export_supervision']); result['restore']=recovery; save_result(root,'restore-result',recovery)
    return recovery



def run_experiments(root,run_id,config_path,duration_seconds,*,prefix=None,resources_prefix=None,source_sha256=None):
    """Explicit CLI-only sequence. Any failure stops before inclusive regression."""
    import os
    from dataclasses import replace
    from datetime import datetime,timezone
    from hashlib import sha256
    from uuid import UUID
    from tests.waveform_dataset_support import DatasetCase
    from tests.waveform_stack import Stack
    from tests.waveform_support import LIMITS
    from tests.test_waveform_repository import capture
    from tests.waveform_measurements import host_resources,safety_check,protocol_roundtrip
    from tests.waveform_resource_scenarios import maximum_scenarios,create_pair,measured_http,audit_scenarios,storage_sample,storage_check,measure_leases
    from tools.waveform_restore import load_source_authority,backup_restore,verify_export_deltas,digest_file
    from backend.waveforms.commands import Actor
    from backend.waveforms.export import DatasetExporter
    from backend.waveforms.errors import WaveformError
    if type(duration_seconds) is not int or not 1800<=duration_seconds<=7200: raise ValueError('Explicit bounded soak must be at least 1800s')
    if resources_prefix is not None and prefix is None: raise ValueError('Resources require protocol prefix')
    import re
    if not isinstance(source_sha256,str) or not re.fullmatch('[0-9a-f]{64}',source_sha256): raise ValueError('Pinned current source required')
    root=Path(root); load_source_authority(config_path,resolve=False)
    environment=host_resources(root); safety_check(environment,preflight=True)
    os.environ['OILWELL_WAVEFORM_TEST_CONFIG']=str(config_path)
    fixture=DatasetCase(); fixture.task17_run_id=run_id; result={}; scenarios=[]; cycles=[]; progress=None
    def completed_work(messages,bytes_):
        nonlocal progress
        if progress is None: progress=Progress()
        record=progress.record(messages=messages,bytes_=bytes_)
        with (root/'soak-progress.jsonl').open('a',encoding='utf-8') as output: output.write(json.dumps(record)+'\n')
    def scenario(name,refs,assertions=1):
        scenarios.append(dict(id=name,status='passed',assertions=assertions,artifact_refs=refs,segment_run_id=run_id))
        result['scenarios']=scenarios
    def milestone(phase):
        with (root/'phases.jsonl').open('a',encoding='utf-8') as output:
            output.write(json.dumps(dict(phase=phase,utc=datetime.now(timezone.utc).isoformat(),monotonic=time.monotonic()))+'\n')
    try:
        fixture.setUp(); result['identity']=dict(verified=True,source_database=fixture.manifest['database'],run_id=run_id)
        result['environment']=dict(ready=True,host=environment,scope='isolated local synthetic')
        if prefix is not None:
            result.update(prefix); scenarios=list(prefix['scenarios'])
            milestone('validated-prior-protocol-soak')
        else:
            limits=replace(LIMITS,gateways=dict(GW=LIMITS.gateways['GW'],A=LIMITS.gateways['GW']))
            protocols=[]
            for index,gateway in enumerate(('GW','A')):
                milestone('ingress-'+gateway)
                with fixture.capacity_fixture(limits), Stack(fixture,limits=limits,evidence_root=root,run_id=run_id) as stack:
                    value=protocol_roundtrip(fixture,stack,offline_first=bool(index),payloads=capture(gateway))
                    protocols.append(value); ref=save_result(root,'ingress-'+gateway,value)
                    scenario('offline_then_online' if index else 'online_then_offline',[ref],8)
            result['ingress']=dict(online=True,offline=True,same_bytes=all(p['exact_bytes'] for p in protocols),
                distinct_associations=all(p['file_source_ref']!=p['mqtt_source_ref'] for p in protocols))
            scenario('receipt_chain',['ingress-GW.json','ingress-A.json'],6)
            transport=transport_scenario(fixture,root,limits)
            scenario('transport_judgments',[save_result(root,'transport',transport)],5)
            milestone('soak')
            for index in range(len(FAULTS)):
                cycles.append(fault_cycle(fixture,root,index,tail_seconds=duration_seconds/len(FAULTS)+1,progress=completed_work))
            elapsed=time.monotonic()-progress.began
            result['duration']=dict(requested_seconds=duration_seconds,monotonic_elapsed_seconds=elapsed,
                progress_seconds=progress.seconds,messages=progress.messages,bytes=progress.bytes,
                cadence_seconds=5,actual_messages_per_second=progress.messages/elapsed,actual_bytes_per_second=progress.bytes/elapsed)
            result['fault_cycles']=cycles
            scenario('gateway_isolation',['fault-8.json'],4); scenario('lease_recovery',['fault-2.json','fault-9.json','fault-10.json'],6)
            result['quota']=dict(isolated=cycles[8]['quota_isolated'])
            result['receipts']=dict(actual_protocol=True,published=sum(p['receipts'] for p in protocols),
                parsed=sum(p['receipts'] for p in protocols),matched=sum(p['receipts'] for p in protocols),
                wrong_rejected=all(p['wrong_rejected'] for p in protocols),unknown_rejected=all(p['unknown_rejected'] for p in protocols),
                duplicate_safe=all(p['duplicate_safe'] for p in protocols),lost_recovered=cycles[6]['recovered'])
            result['integrity']=dict(exact_bytes=True,digests_match=True,duplicates=0); result['backlog']=dict(final=0)
        if resources_prefix is not None:
            scenarios.extend(resources_prefix['scenarios']); result['scenarios']=scenarios
            milestone('validated-prior-maximum-xy-audit')
        else:
            milestone('maximum-resources')
            with fixture.capacity_fixture(LIMITS):
                maximum=maximum_scenarios(fixture,root/'maximum'); ref=save_result(root,'maximum',maximum)
                for name in ('sender_storage','sender_maximum','raw_maximum','summary_maximum','output_owners'): scenario(name,[ref],3)
            milestone('xy-audit')
            with fixture.capacity_fixture(LIMITS):
                pair,_=create_pair(fixture)
                raw=measured_http(fixture,root,f'/pairs/{pair.id}/preview/window?start=0&count=20000&mode=raw',expected_points=20000,mode='xy')
                summary=measured_http(fixture,root,f'/pairs/{pair.id}/preview/window?start=0&count=20001&mode=summary&budget=2000',mode='xy')
                ref=save_result(root,'xy',dict(raw=raw,summary=summary)); scenario('xy',[ref],3)
                audit=audit_scenarios(fixture,root); ref=save_result(root,'audit',audit); scenario('audit_volume',[ref],6)
        milestone('fresh-lease-measurement')
        result['lease_measurement']=measure_leases(fixture,root,run_id=run_id,source_sha256=source_sha256)
        milestone('populated-recovery')
        with fixture.capacity_fixture(LIMITS):
            before=storage_sample(fixture,root); prepared=prepare_recovery(fixture,root,run_id=run_id,source_sha256=source_sha256)
            from tests.waveform_resource_scenarios import validate_recovery_measurement
            validate_recovery_measurement(prepared['recovery_downloads'],run_id=run_id,source_sha256=source_sha256)
            result['recovery_downloads']=prepared['recovery_downloads']
            storage_check(before,storage_sample(fixture,root)); save_result(root,'recovery-source',prepared)
            scenario('dataset_1000',['dataset-1.json','dataset-100.json','dataset-1000.json'],9)
            target,recovery=backup_restore(fixture,config_path,root,run_id)
            baseline=json.loads((root/'source-fingerprint.json').read_text(encoding='utf-8'))['content']
            finish_target_recovery(fixture,root,run_id,source_sha256,target,recovery,prepared,baseline,result)
        result['resources']=dict(measured=True,aborts=[],host=environment)
        result['scenarios']=scenarios
        result['limits']=['Synthetic local software evidence, not field accuracy or 72-hour stability',
            'No filled-drive/power-cut/production hosting/TLS/request admission certification; legacy SQLite patched rollout remains open']
    finally:
        fixture.doCleanups()
        if (root/'lease-measurement.json').is_file():
            result['lease_measurement']=json.loads((root/'lease-measurement.json').read_text(encoding='utf-8'))
        result['cleanup']=dict(complete=all(c.closed for c in getattr(fixture,'connections',())),residue=[str(i) for i,c in enumerate(getattr(fixture,'connections',())) if not c.closed])
        result['artifacts']=[dict(relative_path=str(p.relative_to(root)).replace('\\','/'),size=p.stat().st_size,sha256=digest_file(p))
            for p in sorted(root.rglob('*')) if p.is_file()]
        save_result(root,'experiment-partial',result)
    return result
