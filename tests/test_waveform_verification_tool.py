"""Acceptance gate tests use fabricated reports, never claim actual soak evidence."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def complete_report():
    ids = ['tests.test_waveform_example.Case.test_one', 'tests.test_registry_example.Case.test_one',
           'tests.test_ingest_example.Case.test_one', 'tests.test_legacy_example.Case.test_one']
    scenarios = ['online_then_offline', 'offline_then_online', 'transport_judgments',
        'receipt_chain', 'gateway_isolation', 'lease_recovery', 'sender_storage',
        'sender_maximum', 'raw_maximum', 'summary_maximum', 'xy', 'audit_volume',
        'dataset_1000', 'output_owners']
    report=dict(schema='waveform.verification/1', run_id='a'*32, evidence_kind='actual', status='completed',
        source={'unchanged':True, 'files_sha256':{'example.py':'b'*64}},
        identity={'verified':True}, environment={'ready':True},
        duration={'requested_seconds':1800, 'monotonic_elapsed_seconds':1800.1,
                  'progress_seconds':list(range(0,1801,60)), 'messages':180, 'bytes':10000},
        ingress={'online':True,'offline':True,'same_bytes':True,'distinct_associations':True},
        scenarios=[dict(id=i,status='passed',assertions=1,artifact_refs=['events.jsonl']) for i in scenarios],
        fault_cycles=[dict(index=i,observed=True,recovered=True,kind='process_restart',
                           instance_id=str(i),duration_seconds=1) for i in range(12)],
        receipts={'actual_protocol':True,'published':3,'parsed':3,'matched':3,'wrong_rejected':True,
                  'unknown_rejected':True,'duplicate_safe':True,'lost_recovered':True},
        integrity={'exact_bytes':True,'digests_match':True,'duplicates':0},
        backlog={'peak':2,'final':0}, quota={'isolated':True},
        resources={'measured':True,'aborts':[]},
        restore={k:True for k in ('snapshot','content','schema','acl','extension','exports','restrictions')},
        tests=dict(scope='whole_project',configured=True,discovered_ids=ids,started_ids=ids,
            terminal_results=[{'id':i,'outcome':'passed'} for i in ids],modules=['test_waveform_example.py',
            'test_registry_example.py','test_ingest_example.py','test_legacy_example.py'],
            loader_errors=[],testsRun=4,failures=0,errors=0,skipped=0,expected_failures=0,
            unexpected_successes=0,exit_code=0),
        protection={'unchanged':True},cleanup={'complete':True,'residue':[]},
        artifacts=[{'relative_path':'events.jsonl','size':1,'sha256':'c'*64}],
        limits=['synthetic; no field or production certification'])
    bind_fake_lease(report)
    return report


def bind_fake_recovery(report):
    """Pure final-gate vector, not measured files or timing evidence."""
    from tools.verify_waveforms import inventory_sha256
    value=dict(schema='waveform.recovery-downloads/2',policy='R88-two-stage',status='passed',run_id=report['run_id'],
        source_sha256=inventory_sha256(report['source']['files_sha256']),volumes=[1,100,1000],owned_cleanup=True,monitor_cleanup=True,
        clock=dict(began=0.,outer=3600.,preparation_deadline=1800.,prepared=10.,download_started=11.,download_deadline=1811.,completed=20.),prepared=[],downloads=[])
    for n,count in enumerate((2,1,100,1000)):
        p=dict(version_id=str(n),export_id=str(n+10),actor=dict(account='synthetic'),members=count,run_id=value['run_id'],source_sha256=value['source_sha256'],
            expected=[dict(name='manifest.json',sha256=sha256(b'{}').hexdigest(),size=2)]+
                [dict(name=str(i)+'.owpwave',sha256=sha256(b'x').hexdigest(),size=1,key=dict(capture_id=str(i))) for i in range(count)])
        value['prepared'].append(p)
        value['downloads'].extend(dict(f,version_id=p['version_id'],export_id=p['export_id'],relative_path=p['version_id']+'/'+f['name'],started=12.,completed=13.) for f in p['expected'])
    report['recovery_downloads']=value


def bind_fake_lease(report):
    """Fabricated acceptance vector, never an actual lease experiment."""
    from tools.verify_waveforms import inventory_sha256
    report['lease_measurement']=dict(schema='waveform.lease-measurement/1',status='passed',run_id=report['run_id'],
        source_sha256=inventory_sha256(report['source']['files_sha256']),
        assertions={k:True for k in ('one_claim','owned_64','full_unowned_refused','expired_owned_64','reclaimed_owned_only',
            'token_version_changed','stale_renew_refused','stale_run_refused','stale_completion_unchanged','replacement_completed',
            'rollback_refused','rollback_cleared_owned','two_workers_renewed','two_workers_completed','workers_clean','query_plans')},
        timings=[dict(operation='claim',elapsed_seconds=.1)],
        plans=[dict(cardinality=n,state=s,query='SELECT capture_pk FROM waveforms.assembly_jobs WHERE intake_gateway_id=%s',params=['GW'],
            client_seconds=.001,plan={'Execution Time':.01,'Plan':{'Actual Rows':1,'Shared Hit Blocks':1}})
            for n,s in ((1,'available'),(64,'available'),(66,'full-live'),(66,'expired-still-owned')) for _ in range(2)],
        dual_workers=dict(peak_connections=4,completed=2,configured_connections=4,sampler_joined=True,
            metadata=dict(run_id=report['run_id'],source_sha256=inventory_sha256(report['source']['files_sha256']),seconds=120),
            continuous_samples=[dict(monotonic=1.,processes=[dict(pid=p,working_set_bytes=1,peak_working_set_bytes=1,private_commit_bytes=1) for p in (1,2)],
                host=dict(physical_available_bytes=4*1024**3,disk_available_bytes=11*1024**3))],
            owned_cleanup=dict(complete=True),stopped=dict(remaining_threads=[],connections=0)),
        cleanup=dict(complete=True,processes=[dict(component='assemble',pid=1,instance_id='owned',exit_code=0)]))
    report['artifacts']=[a for a in report['artifacts'] if a['relative_path']!='lease-measurement.json']
    data=json.dumps(report['lease_measurement']).encode()
    report['artifacts'].append(dict(relative_path='lease-measurement.json',size=len(data),sha256=sha256(data).hexdigest()))
    bind_fake_recovery(report)


def prefix_fixture(root):
    """Small fabricated hash-bound gate vector, NEVER actual experiment evidence."""
    from tests.waveform_experiments import FAULTS
    run_id=root.name[-32:]; artifacts=[]
    def save(name,value,jsonl=False):
        path=root/name; path.parent.mkdir(parents=True,exist_ok=True)
        data=(''.join(json.dumps(v)+'\n' for v in value) if jsonl else json.dumps(value)).encode()
        path.write_bytes(data); artifacts.append(dict(relative_path=name,size=len(data),sha256=sha256(data).hexdigest()))
    save('run-identity.json',dict(version=1,run_id=run_id,root=str(root)))
    protocol=dict(facts=2,transports=['file','mqtt'],sender_state='archived',receipts=3,exact_bytes=True,digests_match=True,
        wrong_rejected=True,unknown_rejected=True,duplicate_safe=True,file_source_ref='a',mqtt_source_ref='b',
        manifest_sha256='a'*64,file_sha256='b'*64,bytes=20)
    save('ingress-GW.json',protocol); save('ingress-A.json',protocol)
    save('transport.json',dict(facts=3,cross_gateway_rejected=True,orphan_recovered=True,retained_rejected=True,
        outcomes=[['A','quarantined','identity_mismatch'],['GW','quarantined','retained_publish'],['GW','stored','accepted'],['GW','stored','accepted']]))
    cycles=[]
    for index,mechanism in enumerate(FAULTS):
        events=dict(receive=[dict(event='subscribed',gateway='GW'),dict(event='stopped',remaining_threads=[])])
        kind=('transaction_fault','transaction_fault','process_restart','process_restart','process_restart','link_loss',
              'process_restart','link_loss','link_loss','process_restart','process_restart','link_loss')[index]
        cycle=dict(index=index,mechanism=mechanism,observed=True,recovered=True,kind=kind,
            instance_id=f'{index:032x}',facts=2,receipts=3,messages=2,bytes=20,duration_seconds=150.,
            cleanup=dict(complete=True,residue=[]),events=events,samples=[dict(monotonic=index,processes=[dict(pid=index+1,
                working_set_bytes=1,peak_working_set_bytes=1,private_commit_bytes=1)])],storage=[dict(database_bytes=1,artifact_bytes=1,logical_raw_bytes=1)],latencies_seconds=[.1])
        if index==8: cycle['quota_isolated']=True
        cycles.append(cycle); save(f'fault-{index}.json',cycle)
        save(f'owned-{index}/receive.log',events['receive'],True)
        save(f'owned-{index}/processes.json',[dict(run_id=run_id,instance_id=cycle['instance_id'],pid=index+1,component='receive',exit_code=0)])
    seconds=[i*1800/23 for i in range(24)]
    progress=[dict(monotonic_elapsed_seconds=t,progress_seconds=seconds[:i+1],messages=i+1,bytes=10*(i+1)) for i,t in enumerate(seconds)]
    save('soak-progress.jsonl',progress,True)
    report=dict(schema='waveform.verification/1',evidence_kind='actual',status='partial',run_id=run_id,
        source=dict(unchanged=True,files_sha256={'backend/unchanged.py':'a'*64,'tests/waveform_measurements.py':'b'*64}),
        protection=dict(unchanged=True,before={'sentinel':1},after={'sentinel':1}),
        identity=dict(verified=True,run_id=run_id,source_database='oilwell_waveforms_test'),cleanup=dict(complete=True,residue=[]),
        duration=dict(requested_seconds=1800,monotonic_elapsed_seconds=1800.,progress_seconds=seconds,messages=24,bytes=240),
        fault_cycles=cycles,artifacts=artifacts)
    path=root/'report.json'; path.write_text(json.dumps(report),encoding='utf-8')
    return path,report


def resource_fixture(root):
    """Tiny fabricated semantic vector, not authenticated experiment evidence."""
    artifacts=[]; run_id=root.name[-32:]; sequence=0
    def save(name,value,lines=False,binary=False):
        path=root/name; path.parent.mkdir(parents=True,exist_ok=True)
        data=value if binary else (''.join(json.dumps(v)+'\n' for v in value) if lines else json.dumps(value)).encode()
        path.write_bytes(data); item=dict(relative_path=name,size=len(data),sha256=sha256(data).hexdigest())
        artifacts[:]=[a for a in artifacts if a['relative_path']!=name]; artifacts.append(item)
        return item
    host=dict(physical_available_bytes=4*1024**3,disk_available_bytes=11*1024**3)
    def envelope(kind,points=0,channels=0,route='',captures=None):
        nonlocal sequence
        sequence+=1; directory=f'measurement-{sequence}'; component='sender_measure' if captures else 'resource_measure' if kind=='resource' else 'http_measure'
        memory=dict(monotonic=1.,pid=sequence,working_set_bytes=100,peak_working_set_bytes=100,private_commit_bytes=100)
        stopped=dict(event='stopped',remaining_threads=[],connections=0,output_owners=0)
        events=([dict(event='http_request_complete',request_id=str(sequence),pid=sequence,status=200,error_code=None,elapsed_seconds=1.)] if kind=='http' else [])+[stopped]
        process=dict(instance_id=f'{sequence:032x}',component=component,pid=sequence,exit_code=0)
        cleanup=dict(complete=True,processes=[process])
        v=dict(kind=kind,status='passed',metadata=dict(route=route,expected_points=points,mode='ordinary'),
            envelope_path=str(root/directory/'result.json'),samples_path=str(root/directory/'samples.jsonl'),
            events=events,samples=[memory],elapsed_seconds=1.,exit_code=0,owned_cleanup=cleanup,cleanup=stopped)
        samples=[dict(processes=[memory],host=host)]
        if captures:
            status=dict(captures=captures,retained_capture_bytes=sum(i['file_bytes'] for i in inputs[:captures]),charged_bytes=100*captures,max_bytes=134217728,states={'awaiting_capture_receipt':captures})
            events[:0]=[dict(event='sender_phase_started',phase='run_once',operation_id=1,monotonic=1.),
                dict(event='sender_phase_complete',phase='run_once',operation_id=1,monotonic=2.,elapsed_seconds=1.),
                dict(event='sender_phase_started',phase='prepare',operation_id=2,monotonic=1.),
                dict(event='sender_phase_complete',phase='prepare',operation_id=2,monotonic=2.,elapsed_seconds=1.),
                dict(event='sender_batch',captures=captures,messages=16,elapsed_seconds=1.),
                dict(event='sender_progress',pending_messages=0,status=status)]
            physical=dict(database_bytes=100,journal_bytes=0)
            v.update(captures=captures,physical=[physical],bounds=dict(operation_seconds=120,aggregate_seconds=120*captures),
                logical_status=status,durability=dict(journal_mode='delete',synchronous=3),diagnostic_events=128,cleanup=cleanup,
                workers={component:dict(exit_code=0,stopped=stopped)})
            samples=[dict(memory=memory,physical=physical,owned=[memory],host=host)]
        else:
            v['phases_path']=str(root/directory/'phases.jsonl')
            save(directory+'/phases.jsonl',[dict(component=component,**e) for e in events],True)
        if kind=='http':
            v.update(points=points,channels=channels,bytes=50,response_sha256='a'*64,items=100,snapshot_revision='snapshot',
                sample_witnesses=[dict(channel_id=f'c{i}',count=points//channels,minimum=-50000+i*13,maximum=50000+i*13) for i in range(channels)])
        if kind=='output_owners': v.update(healthy=True,reset=True,healthy_bytes=50,half_close_bytes=50,continuous_samples=samples)
        save(directory+'/samples.jsonl',samples,True)
        save(directory+'/processes.json',[dict(process,run_id=run_id)])
        save(directory+'/'+component+'.log',events,True)
        save(directory+'/result.json',v)
        return v
    inputs=[]
    for i in range(3):
        manifest=dict(kind='manifest',schema='OWP-WAVE/1.0',streams=[dict(chunk_count=129,chunk_frames=819,decoded_bytes=16777120,
            frame_bytes=160,frame_count=104857,channels=[dict(channel_id=f'c{i}',encoding='i32le') for i in range(32)])])
        item=save(f'maximum/maximum-{i}.owpwave',json.dumps(manifest).encode()+b'\n',binary=True)
        v=dict(path=str(root/item['relative_path']),frames=104857,channels=32,decoded_bytes=16777120,chunks=129,
            file_bytes=item['size'],raw_bytes=item['size']-1,manifest_sha256=sha256(json.dumps(manifest).encode()).hexdigest(),file_sha256=item['sha256'],generator='nonflat-buckets/1',buckets=500)
        inputs.append(v); save(item['relative_path']+'.input.json',v)
    assemblies=[dict(capture_pk=str(i),elapsed_seconds=1.) for i in range(3)]
    intake=envelope('resource'); intake['result']=dict(assemblies=assemblies,capture_pk='0'); save(str(Path(intake['envelope_path']).relative_to(root)).replace('\\','/'),intake)
    maximum=dict(inputs=inputs,sender_one=envelope('sender',captures=1),sender_three=envelope('sender',captures=3),intake=intake,
        raw=envelope('http',640000,32,'/captures/0/streams/resource/window?start=0&count=20000&mode=raw'),
        summary=envelope('http',64000,32,'/captures/0/streams/resource/window?start=0&count=104857&mode=summary&budget=2000'),
        output_owners=envelope('output_owners'),storage_before=dict(database_bytes=1,artifact_bytes=0),
        storage_after=dict(database_bytes=100,artifact_bytes=100),lease_comparison=dict(lease_seconds=30,cumulative_cap_seconds=300,assemblies=assemblies))
    xy=dict(raw=envelope('http',20000,route='/pairs/0/preview/window?start=0&count=20000&mode=raw'),
        summary=envelope('http',673,route='/pairs/0/preview/window?start=0&count=20001&mode=summary&budget=2000'))
    audit=dict(timeout_seconds=3,levels=[dict(target_rows=n,actual_rows=n,stale_page_rejected=True,
        response=envelope('http',route='/audit?family=raw&actor_id=task17-audit-volume&page_size=100')) for n in (100,1000,10000)])
    for name,value in (('maximum',maximum),('xy',xy),('audit',audit)): save(name+'.json',value)
    return dict(run_id=run_id,artifacts=artifacts),maximum,xy,audit,save


class VerificationToolTests(unittest.TestCase):
    def test_live_snapshots_use_separate_read_transactions_and_keep_read_error_on_rollback_failure(self):
        from unittest.mock import Mock,patch
        from tools import waveform_restore as restore
        connection=Mock(); connection.info.server_version=180000
        connection.execute.return_value.fetchone.return_value=('UTF8','C','C','c')
        connection.execute.return_value.fetchall.return_value=[]
        with patch.object(restore,'content_inventory',side_effect=[{'facts':0},{'facts':1}]),patch.object(restore,'security_inventory',return_value={}):
            self.assertNotEqual(restore.source_state(connection),restore.source_state(connection))
        self.assertEqual(connection.rollback.call_count,4)
        self.assertEqual(sum(c.args==('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY',) for c in connection.execute.call_args_list),2)
        connection.rollback.side_effect=[None,OSError('rollback failure')]
        with patch.object(restore,'content_inventory',side_effect=ValueError('original read')):
            with self.assertRaisesRegex(ValueError,'original read'): restore.source_state(connection)

    def test_retained_guard_accepts_cleaned_source_but_refuses_new_snapshot_content_change(self):
        from unittest.mock import Mock,patch
        from tools import waveform_restore as restore
        historical=dict(content={'facts':1000},security={'acl':[['owner']]},properties=['UTF8','C','C','c'])
        live=dict(historical,content={'facts':0})
        for changed in (False,True):
            connection=Mock(); connection.closed=False; connection.execute.return_value.fetchone.return_value=(True,)
            connection.close.side_effect=lambda:setattr(connection,'closed',True)
            with patch.object(restore,'connect_source',return_value=connection),patch.object(restore,'verify_principals'),\
                patch.object(restore.RetainedSource,'guard_alive'),\
                patch.object(restore,'source_state',side_effect=[live,dict(live,content={'facts':1}) if changed else live]) as snapshots:
                context=restore.RetainedSource({}, {},'a'*32,historical)
                if changed:
                    with self.assertRaisesRegex(ValueError,'Source changed'):
                        with context: pass
                else:
                    with context: pass
                self.assertEqual(snapshots.call_count,2)
                self.assertEqual(context.receipt['complete'],not changed); self.assertTrue(connection.closed)
                self.assertEqual(context.receipt['source_before']['content'],{'facts':0})

    def test_retained_source_owns_connection_before_identity_check_and_preserves_error(self):
        from unittest.mock import Mock,patch
        from tools import waveform_restore as restore
        connection=Mock(); connection.execute.side_effect=ValueError('identity failure'); connection.close.side_effect=OSError('close failure')
        owners=[]; authority={'settings':{'admin':{'user':'owner'}}}
        with patch('psycopg.connect',return_value=connection):
            with self.assertRaisesRegex(ValueError,'identity failure'):
                restore.connect_source(authority,_created=owners.append)
        self.assertEqual(owners,[connection]); connection.close.assert_called_once()

    def test_retained_cli_bindings_and_exclusive_suffix_dispatch_before_fullgate(self):
        from unittest.mock import Mock,patch
        from contextlib import redirect_stdout
        import io
        from tools import verify_waveforms as tool,waveform_restore as restore
        base=['--config','unused','--evidence-dir','unused']
        with patch.object(tool,'source_inventory',side_effect=AssertionError('incomplete arguments must not spawn')):
            with self.assertRaises(ValueError): tool.main(base+['--continue-recovery-from','old.json'])
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); current={'a':'a'*64}; digest=tool.inventory_sha256(current)
            old=complete_report(); old['run_id']=restore.RETAINED_RUN_ID
            resources=dict(prior_segment={'kind':'prior'},segment={'kind':'middle'})
            binding=dict(bundle=dict(report=old),original={},resources=resources,transition={'id':'bound'})
            args=['--config','unused','--evidence-dir',str(root),'--include-existing-regression',
                '--continue-from','run1/report.json','--continue-sha256',tool.RUN1_REPORT_SHA256,
                '--continue-resources-from','run3/report.json','--continue-resources-sha256',tool.RUN3_REPORT_SHA256,
                '--continue-recovery-from','r90/report.json','--continue-recovery-sha256',restore.RETAINED_REPORT_SHA256,
                '--continue-source-sha256',digest]
            closed=[]
            def suffix(*a,**kw): closed.append(True); return dict(cleanup={'complete':True,'residue':[]})
            def regression(*a,**kw):
                self.assertEqual(closed,[True]); (root/'regression.json').write_text('{"exit_code":0}')
                return Mock(returncode=0)
            with patch.object(tool,'source_inventory',return_value=current),patch.object(tool,'validate_retained_continuation',return_value=binding),\
                patch.object(tool,'validate_continuation',side_effect=AssertionError('no alternate preflight')),\
                patch.object(restore,'load_source_authority',return_value={}),patch.object(restore,'create_run_root',return_value=(root,'b'*32)),\
                patch.object(tool,'copy_continuation',return_value={'scenarios':[]}),patch.object(tool,'copy_resources_continuation',return_value={'scenarios':[]}),\
                patch.object(tool,'copy_retained_continuation'),patch('tests.waveform_experiments.run_retained_restore',side_effect=suffix) as run,\
                patch('tests.waveform_experiments.run_experiments',side_effect=AssertionError('no old experiments')),\
                patch('tools.verify_ingest_reliability.protected_snapshot',return_value={}),patch.object(tool,'configured_environment',return_value={}),\
                patch.object(tool,'qualify',return_value=()),patch.object(tool,'verify_lease_artifact',return_value=()),\
                patch.object(tool,'verify_recovery_artifact',return_value=()),patch.object(tool.subprocess,'run',side_effect=regression) as full,redirect_stdout(io.StringIO()):
                self.assertEqual(tool.main(args),0); run.assert_called_once(); full.assert_called_once()
            report=json.loads((root/'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['evidence_mode'],'four_segment'); self.assertEqual(len(report['segments']),4)
            self.assertEqual(report['segments'][2]['historical_status'],'partial')
            self.assertEqual(report['source']['scope'],'retained_target_and_regression')

    def test_retained_preflight_wrong_external_pins_refuses_before_any_effect(self):
        from unittest.mock import patch
        from tools import verify_waveforms as tool,waveform_restore as restore
        self.assertTrue(hasattr(tool,'validate_retained_continuation'),'Retained preflight absent')
        current={'test.py':'a'*64}
        with patch.object(restore,'retained_bundle',side_effect=AssertionError('read should follow pins')),\
            patch.object(restore,'load_source_authority',side_effect=AssertionError('credentials forbidden')),\
            patch.object(tool.subprocess,'run',side_effect=AssertionError('spawn forbidden')):
            for wrong in ('report','run1','run3','source'):
                with self.subTest(wrong=wrong),self.assertRaises(ValueError):
                    tool.validate_retained_continuation(Path('unused'),
                        '0'*64 if wrong=='report' else restore.RETAINED_REPORT_SHA256,
                        '0'*64 if wrong=='source' else tool.inventory_sha256(current),current,
                        original_path=Path('unused'),original_sha256='0'*64 if wrong=='run1' else tool.RUN1_REPORT_SHA256,
                        resources_path=Path('unused'),resources_sha256='0'*64 if wrong=='run3' else tool.RUN3_REPORT_SHA256)

    def test_four_segment_gate_keeps_historical_lease_identity_and_requires_fresh_cleanup(self):
        from unittest.mock import patch
        from tools import verify_waveforms as tool,waveform_restore as restore
        self.assertTrue(hasattr(tool,'four_segment_assertions'),'Four-segment gate absent')
        historical=complete_report(); historical['run_id']=restore.RETAINED_RUN_ID; bind_fake_lease(historical)
        current={'current.py':'d'*64}; digest=tool.inventory_sha256(current)
        report=deepcopy(historical); report.update(run_id='e'*32,evidence_mode='four_segment')
        report['source']=dict(files_sha256=current,unchanged=True,scope='retained_target_and_regression')
        prior=dict(run_id=tool.RUN1_ID,report_sha256=tool.RUN1_REPORT_SHA256,report_ref='prior-run1/report.json')
        middle=dict(run_id=tool.RUN3_ID,report_sha256=tool.RUN3_REPORT_SHA256,report_ref='prior-run3/report.json')
        retained=dict(kind='reused_source_preparation_and_lease',run_id=restore.RETAINED_RUN_ID,report_sha256=restore.RETAINED_REPORT_SHA256,
            report_ref='prior-r90/report.json',files_sha256=historical['source']['files_sha256'],source_sha256=tool.inventory_sha256(historical['source']['files_sha256']),
            historical_status='partial',excluded_target='oilwell_waveforms_restore_'+restore.RETAINED_RUN_ID,
            excluded_reason='safety.host_reserve',validated=True)
        suffix=dict(kind='fresh_target_and_regression',run_id=report['run_id'],source_sha256=digest,approved_source_sha256=digest,source_transition={'id':'approved'})
        report['segments']=[prior,middle,retained,suffix]; report['tests'].update(run_id=report['run_id'],source_sha256=digest)
        report['cleanup']['source_guard']=dict(complete=True,source_unchanged=True,closed=[True],unlocked=[499001213,499001211],errors=[],first_error=None)
        report['restore'].update(target='oilwell_waveforms_restore_'+report['run_id'],archive_sha256=restore.RETAINED_ARCHIVE_SHA256,
            deltas=dict(unchanged_other_tables=True,business_audits=1107,raw_audits=1103),export_supervision=dict(status='passed',owned_cleanup={'complete':True},result=dict(run_id=report['run_id'],source_sha256=digest)))
        report['artifacts']=[dict(a,relative_path='prior-r90/'+a['relative_path']) for a in report['artifacts']]
        for segment in (prior,middle,retained): report['artifacts'].append(dict(relative_path=segment['report_ref'],size=1,sha256=segment['report_sha256']))
        refs={'online_then_offline':['ingress-GW.json'],'offline_then_online':['ingress-A.json'],'receipt_chain':['ingress-GW.json','ingress-A.json'],
            'transport_judgments':['transport.json'],'gateway_isolation':['fault-8.json'],'lease_recovery':['fault-2.json','fault-9.json','fault-10.json'],
            'xy':['xy.json'],'audit_volume':['audit.json'],'dataset_1000':['dataset-1.json','dataset-100.json','dataset-1000.json']}
        for case in report['scenarios']:
            segment,folder=(prior,'prior-run1/') if case['id'] in tool.PROTOCOL_SCENARIOS else (middle,'prior-run3/') if case['id'] in tool.RESOURCE_SCENARIOS else (retained,'prior-r90/')
            case.update(segment_run_id=segment['run_id'],artifact_refs=[folder+n for n in refs.get(case['id'],['maximum.json'])])
        for name in sorted({name for case in report['scenarios'] for name in case['artifact_refs']}):
            report['artifacts'].append(dict(relative_path=name,size=1,sha256='a'*64))
        with patch.object(restore,'RETAINED_SOURCE_SHA256',retained['source_sha256']),\
            patch.object(tool,'resource_lineage'),patch.object(tool,'retained_source_transition',return_value={'id':'approved'}):
            self.assertEqual(tool.qualify(report),())
            for change in ('lease','cleanup','target','exports','oldstatus','pin','container'):
                bad=deepcopy(report)
                if change=='lease': bad['lease_measurement']['run_id']=bad['run_id']
                elif change=='cleanup': bad['cleanup']['source_guard']['unlocked']=[499001211]
                elif change=='target': bad['restore']['target']=retained['excluded_target']
                elif change=='exports': bad['restore'].pop('export_supervision')
                elif change=='oldstatus': bad['segments'][2]['historical_status']='completed'
                elif change=='pin': bad['segments'][2]['report_sha256']='0'*64
                else: bad['segments'][2]=[]
                with self.subTest(change=change): self.assertTrue(tool.qualify(bad))

    def test_retained_orchestration_skips_all_source_work_and_closes_before_return(self):
        from unittest.mock import Mock,patch
        from tools import waveform_restore as restore
        from tests import waveform_experiments as experiments
        self.assertTrue(hasattr(experiments,'run_retained_restore'),'Retained suffix orchestrator absent')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); config=root/'config.toml'; config.write_text('')
            old=complete_report(); bundle=dict(report=old,recovery={},references={},fingerprint={'content':{}},failed_target='old-target')
            closed=[]; contexts=[]; validated_raw={'validated':'snapshot'}
            class Source:
                before={}
                receipt=dict(complete=True,source_unchanged=True,closed=[True],unlocked=[499001213,499001211],errors=[],first_error=None)
                def __init__(self,*a): contexts.append(a)
                def __enter__(self): return self
                def __exit__(self,*a): closed.append(True)
            def finish(*a,**k): self.assertFalse(closed); return {'exports':True,'restrictions':True}
            with patch.object(restore,'validate_retained_copy',return_value=bundle),\
                patch.object(restore,'load_source_authority',return_value={'raw_config':validated_raw}),patch.object(restore,'RetainedSource',Source),\
                patch.object(restore,'restore_retained',return_value=(object(),{})),\
                patch.object(experiments,'finish_target_recovery',side_effect=finish),\
                patch.object(experiments,'prepare_recovery',side_effect=AssertionError('source preparation forbidden')),\
                patch('tests.waveform_measurements.host_resources',return_value=dict(physical_available_bytes=4*1024**3,disk_available_bytes=11*1024**3)):
                result=experiments.run_retained_restore(root,'b'*32,config,source_sha256='c'*64,prefix={'scenarios':[]},resources_prefix={'scenarios':[]})
                self.assertEqual(closed,[True]); self.assertTrue(result['cleanup']['complete'])
                self.assertIs(contexts[0][1],validated_raw)
                self.assertEqual(result['lease_measurement'],old['lease_measurement'])
                self.assertEqual(result['recovery_downloads'],old['recovery_downloads'])
                self.assertEqual(result['restore'],{'exports':True,'restrictions':True})
                self.assertTrue((root/'experiment-partial.json').exists())

    def test_retained_target_request_uses_mapped_copy_without_relabeling_measurement(self):
        from unittest.mock import patch
        from tools import waveform_restore as restore
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); (root/'retained-source-binding.json').write_text('{}')
            measured=dict(run_id=restore.RETAINED_RUN_ID,source_sha256=restore.RETAINED_SOURCE_SHA256,
                prepared=[dict(version_id='v',expected=[dict(name='manifest.json',size=2,sha256='a'*64)])])
            bundle=dict(recovery=dict(recovery_downloads=measured),references={'v':str(root/'prior-r90/original-v')})
            with patch.object(restore,'validate_run_root',return_value=root),\
                patch.object(restore,'validate_retained_copy',return_value=bundle) as validate,\
                patch.object(restore,'_target_export_inputs'):
                request=restore.target_export_request(root,'b'*32,'c'*64,[],bundle['references'])
                self.assertEqual(request['references']['v']['relative_path'],'prior-r90/original-v')
                validate.assert_called_once_with(root,'c'*64)
                self.assertEqual(measured['run_id'],restore.RETAINED_RUN_ID)

    def test_retained_references_authenticate_all_1107_files_and_version_actor_paths(self):
        from unittest.mock import patch
        from uuid import UUID
        from tools import waveform_restore as restore
        self.assertTrue(hasattr(restore,'retained_references'),'Retained reference authentication absent')
        report=complete_report(); measured=report['recovery_downloads']; measured['run_id']=restore.RETAINED_RUN_ID
        measured['source_sha256']=restore.RETAINED_SOURCE_SHA256; measured['downloads']=[]
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); origin=root/'original'; origin.mkdir(); index={}; records=[]; refs={}
            for n,p in enumerate(measured['prepared']):
                p.update(run_id=measured['run_id'],source_sha256=measured['source_sha256'],version_id=str(UUID(int=n+1)),export_id=str(UUID(int=n+10)),
                    actor=dict(domain='local',user_id='reviewer',username='Synthetic reviewer',role_code='admin'))
                version=p['version_id']; refs[version]=str(origin/version); directory=root/version; directory.mkdir()
                records.append({k:p[k] for k in ('version_id','export_id','actor')})
                for i,f in enumerate(p['expected']):
                    if i: f['name']=str(UUID(int=i))+'.owpwave'
                    data=b'{}' if not i else b'x'; path=directory/f['name']; path.write_bytes(data)
                    relative=path.relative_to(root).as_posix(); index[relative]=dict(relative_path=relative,size=len(data),sha256=sha256(data).hexdigest())
                    measured['downloads'].append(dict(f,version_id=version,export_id=p['export_id'],relative_path=relative,started=12.,completed=13.))
            recovery=dict(records=records,references=refs,recovery_downloads=measured)
            result=restore.retained_references(root,recovery,origin,index)
            self.assertEqual(len(result['files']),1107); self.assertEqual(set(result['references']),set(refs))
            for change in ('actor','duplicate','path','file','extra','digest'):
                bad=deepcopy(recovery); extra=None; changed=None
                if change=='actor': bad['records'][0]['actor']=dict(bad['records'][0]['actor'],user_id='wrong')
                elif change=='duplicate': bad['recovery_downloads']['prepared'][1]['version_id']=records[0]['version_id']
                elif change=='path': bad['references'][records[0]['version_id']]=str(origin/'wrong')
                elif change=='file': bad['recovery_downloads']['downloads'][0]['relative_path']='../outside'
                elif change=='extra': extra=root/records[0]['version_id']/'extra'; extra.write_bytes(b'x')
                else:
                    changed=root/measured['downloads'][0]['relative_path']; changed.write_bytes(b'xx')
                with self.subTest(change=change),self.assertRaises(ValueError): restore.retained_references(root,bad,origin,index)
                if extra: extra.unlink()
                if changed: changed.write_bytes(b'{}')

    def test_retained_restore_rechecks_live_before_shared_tail_and_never_dumps(self):
        from unittest.mock import Mock,patch
        from tools import waveform_restore as restore
        self.assertTrue(hasattr(restore,'restore_retained'),'Retained archive caller absent')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source=Mock(assert_live=Mock()); fingerprint={'archive_sha256':'a'*64}
            bundle=dict(fingerprint=fingerprint,archive=root/'prior-r90/source.dump')
            with patch.object(restore,'_restore_archive',return_value=('target',{})) as tail,\
                patch.object(restore,'validate_retained_copy',return_value=bundle),patch.object(restore,'_tool') as tool,\
                patch.object(restore.subprocess,'run',return_value=Mock(stdout=b'pg_restore (PostgreSQL) 18.0')) as version,\
                patch.object(restore,'digest_file',return_value='a'*64):
                source.assert_live.side_effect=ValueError('Live source mismatch')
                with self.assertRaisesRegex(ValueError,'Live source'): restore.restore_retained(source,root,'b'*32)
                tail.assert_not_called(); tool.assert_not_called(); version.assert_not_called()
                source.assert_live.side_effect=None
                self.assertEqual(restore.restore_retained(source,root,'b'*32),('target',{}))
                self.assertEqual(tail.call_args.kwargs['archive'],bundle['archive'])
                self.assertEqual(tail.call_args.kwargs['fingerprint'],fingerprint)
                self.assertIs(tail.call_args.kwargs['guard'],source.guard_alive)
                tool.assert_not_called()

    def test_retained_guard_rejects_live_mismatch_before_target_and_closes_all(self):
        from unittest.mock import Mock,patch
        from tools import waveform_restore as restore
        self.assertTrue(hasattr(restore,'RetainedSource'),'Non-resetting retained guard absent')
        authority=dict(parent={'fixture_id':'fixture'},sidecar={},settings={})
        fingerprint=dict(content={'facts':1},security={'acl':[('owner',)]},properties=('UTF8','C','C','c'))
        conn=Mock(); conn.closed=False; conn.close.side_effect=lambda:setattr(conn,'closed',True)
        with patch.object(restore,'connect_source',return_value=conn),patch.object(restore,'verify_principals'),\
            patch.object(restore.RetainedSource,'guard_alive'),patch.object(restore,'source_state',return_value=dict(fingerprint,security={'acl':[['wrong-owner']]})),\
            patch.object(restore,'create_target') as create:
            context=restore.RetainedSource(authority,{},'a'*32,fingerprint)
            with self.assertRaisesRegex(ValueError,'Live source'):
                with context: create()
            create.assert_not_called(); self.assertTrue(conn.closed)
            self.assertEqual(context.receipt['unlock_attempts'],[499001213,499001211])
            self.assertFalse(context.receipt['complete'])

    def test_retained_guard_cleanup_attempts_both_unlocks_and_every_close_preserving_first_error(self):
        from unittest.mock import Mock,patch
        from tools import waveform_restore as restore
        self.assertTrue(hasattr(restore,'RetainedSource'),'Non-resetting retained guard absent')
        state=dict(content={'facts':1},security={'acl':[['owner']]},properties=['UTF8','C','C','c'])
        guard=Mock(); guard.closed=False; second=Mock(); second.closed=False
        guard.close.side_effect=OSError('first close'); second.close.side_effect=lambda:setattr(second,'closed',True)
        with patch.object(restore,'connect_source',return_value=guard),patch.object(restore,'verify_principals'),\
            patch.object(restore.RetainedSource,'guard_alive'),patch.object(restore,'source_state',return_value=state):
            context=restore.RetainedSource({}, {},'a'*32,state)
            with self.assertRaisesRegex(RuntimeError,'primary'):
                with context:
                    context.connections.append(second)
                    guard.execute.side_effect=OSError('unlock')
                    raise RuntimeError('primary')
            self.assertEqual(context.receipt['unlock_attempts'],[499001213,499001211])
            self.assertEqual(guard.close.call_count,1); self.assertTrue(second.closed)
            self.assertFalse(context.receipt['complete']); self.assertEqual(context.receipt['first_error'],'RuntimeError')

    def test_retained_source_transition_requires_exact_prior_diagnostics_and_external_pin(self):
        from unittest.mock import patch
        from tools import verify_waveforms as tool,waveform_restore as restore
        self.assertTrue(hasattr(tool,'retained_source_transition'),'R90-specific exact transition absent')
        old={'fixed':'a'*64,'diagnostic':'b'*64,'gate':'c'*64}; baseline=old|{'diagnostic':'d'*64}; current=baseline|{'gate':'e'*64}
        with patch.object(restore,'RETAINED_SOURCE_SHA256',tool.inventory_sha256(old)),\
            patch.object(tool,'RETAINED_BASE_SHA256',tool.inventory_sha256(baseline)),\
            patch.object(tool,'RETAINED_DIAGNOSTIC_DELTA',{'diagnostic':dict(before='b'*64,after='d'*64)}),\
            patch.object(tool,'RETAINED_IMPLEMENTATION_BEFORE',{'gate':'c'*64}):
            result=tool.retained_source_transition(old,current,tool.inventory_sha256(current))
            self.assertEqual(result['id'],'R92-retained-source-new-target')
            self.assertEqual(result['implementation_delta'],{'gate':dict(before='c'*64,after='e'*64)})
            for changed in (current|{'fixed':'f'*64},current|{'diagnostic':'f'*64},current|{'extra':'f'*64},{'gate':'e'*64},baseline):
                with self.subTest(changed=changed),self.assertRaises(ValueError): tool.retained_source_transition(old,changed,tool.inventory_sha256(changed))
            with self.assertRaises(ValueError): tool.retained_source_transition(old,current,'0'*64)
            with self.assertRaises(ValueError): tool.retained_source_transition(old|{'fixed':'f'*64},current,tool.inventory_sha256(current))

    def test_restore_catalog_normalization_preserves_all_values_and_rejects_unsupported_objects(self):
        from tools import waveform_restore as restore
        self.assertTrue(hasattr(restore,'catalog_value'),'Lossless persisted/live catalogue comparison absent')
        live={'extension':[('btree_gist','1.8','public','owner')],'acl':[(None,'reader','CONNECT',False)],'nested':(1,True,None,{'x':('a',)})}
        stored=json.loads(json.dumps(live))
        self.assertEqual(restore.catalog_value(live),stored)
        for key,value in (('extension',[['btree_gist','1.8','public','wrong']]),('extra',[]),('nested',[1,False,None,{'x':['a']}])):
            self.assertNotEqual(restore.catalog_value(live),restore.catalog_value(stored|{key:value}))
        with self.assertRaises(TypeError): restore.catalog_value({'bad':object()})

    def test_reviewed_lineage_reverses_only_exact_checkpoints_and_final_two_files(self):
        from unittest.mock import patch
        tool=self.tool()
        self.assertTrue(hasattr(tool,'reviewed_source_transition'),'Fixed reviewed checkpoint validator required')
        a,b,c,d,e,f=('1'*64,'2'*64,'3'*64,'4'*64,'5'*64,'6'*64)
        original={'untouched':a,'tools/verify_waveforms.py':a,'tests/test_waveform_verification_tool.py':a,'doc':a}
        migration=original|{'sql':b}
        docs=migration|{'doc':c}
        current=docs|{'tools/verify_waveforms.py':d,'tests/test_waveform_verification_tool.py':e}
        digest=lambda v:sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        chain=(('migration',digest(original),digest(migration),(('sql',None,b),)),
               ('docs',digest(migration),digest(docs),(('doc',a,c),)))
        before={p:a for p in ('tools/verify_waveforms.py','tests/test_waveform_verification_tool.py')}
        with patch.object(tool,'REVIEWED_SOURCE_CHAIN',chain),patch.object(tool,'FINAL_GATE_BEFORE',before), \
             patch.object(tool,'RUN3_SOURCE_SHA256',digest(original)):
            value=tool.reviewed_source_transition(current,digest(current),measured=original)
            self.assertEqual(value['measured'],original)
            self.assertEqual(value['source_transition']['checkpoints'],[
                {'id':'migration','before':digest(original),'after':digest(migration)},
                {'id':'docs','before':digest(migration),'after':digest(docs)}])
            self.assertEqual(set(value['source_transition']['gate_delta']),set(before))
            for changed in (current|{'extra':f},current|{'untouched':f},current|{'doc':a},
                            {p:v for p,v in current.items() if p!='sql'},current|{'tools/verify_waveforms.py':a}):
                with self.subTest(changed=changed),self.assertRaises(ValueError): tool.reviewed_source_transition(changed,digest(changed),measured=original)
            with self.assertRaises(ValueError): tool.reviewed_source_transition(current,f,measured=original)
            with self.assertRaises(ValueError): tool.reviewed_source_transition(current,digest(current),measured=original|{'untouched':f})
            bad_chains=(chain[::-1],chain[:-1],chain+(chain[-1],),
                (chain[0],('docs',digest(migration),f,(('doc',a,c),))),
                (chain[0],('docs',digest(migration),digest(docs),(('doc',a,c),('doc',a,c)))),
                (chain[0],('docs',digest(migration),digest(docs),(('doc',f,c),))),
                (chain[0],('docs',digest(migration),digest(docs),(('doc',a,f),))),
                (chain[0],('docs',digest(migration),digest(docs),())),
                (chain[0],('docs',digest(migration),digest(docs),(('doc',a,None),))))
            for bad in bad_chains:
                with self.subTest(chain=bad),patch.object(tool,'REVIEWED_SOURCE_CHAIN',bad),self.assertRaises(ValueError):
                    tool.reviewed_source_transition(current,digest(current),measured=original)

    def test_target_source_inventory_isolates_nested_git_stdin_without_changing_digest(self):
        from unittest.mock import patch
        from tools import waveform_restore as restore
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'a.txt').write_bytes(b'abc'); (root/'b.txt').write_bytes(b'')
            def git(command,**kwargs):
                self.assertEqual(command,['git','ls-files','-z','--cached','--others','--exclude-standard'])
                self.assertEqual(kwargs,dict(cwd=root,capture_output=True,check=True,timeout=15,stdin=subprocess.DEVNULL))
                return subprocess.CompletedProcess(command,0,stdout=b'b.txt\0a.txt\0a.txt\0missing\0',stderr=b'')
            expected=sha256(b'{"a.txt":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad","b.txt":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}').hexdigest()
            with patch.object(restore,'ROOT',root),patch.object(restore.subprocess,'run',side_effect=git):
                self.assertEqual(restore._current_source_sha256(),expected)

    def test_target_export_operation_events_refuse_missing_replay_and_overdue_completion(self):
        from tests import waveform_resource_scenarios as resources
        self.assertTrue(hasattr(resources,'TargetExportEvents'),'Target event pairing validator absent')
        from unittest.mock import patch
        now=[0.]
        with patch.object(resources.time,'monotonic',side_effect=lambda:now[0]):
            def collector():
                watch=resources.ResourceWatchdog(until=1800.,clock=lambda:now[0]); watch.start_operation('work',0,0.)
                return resources.TargetExportEvents(watch,'work')
            start=dict(event='target_operation_started',sequence=1,operation='identity',started=0.)
            done=dict(event='target_operation_complete',sequence=1,operation='identity',started=0.,completed=1.)
            for events in ([done],[start],[start,start],[start,done,done],[start,dict(done,sequence=2)],[dict(start,sequence=2)],[]):
                with self.subTest(events=events),self.assertRaises(ValueError):
                    c=collector()
                    for event in events:
                        now[0]=max(now[0],event.get('completed',event['started'])); c.accept(event)
                    c.finish()
            now[0]=0.; c=collector(); c.accept(start); now[0]=1.; c.accept(done)
            now[0]=2.; c.accept(dict(start,sequence=2,operation='finalization',started=2.))
            now[0]=3.; c.accept(dict(done,sequence=2,operation='finalization',started=2.,completed=3.))
            self.assertEqual(c.finish(),['identity','finalization'])
            c=collector(); c.accept(start); now[0]=121.
            with self.assertRaisesRegex(RuntimeError,'safety.operation_deadline'): c.accept(dict(done,completed=121.))

    def test_target_supervision_refuses_bare_results_residue_and_late_closed_result(self):
        from unittest.mock import patch
        from tests import waveform_resource_scenarios as resources
        now=[0.]
        good=dict(status='passed',exit_code=0,owned_cleanup=dict(complete=True),target_operations_validated=True,watchdog_closed=True)
        with tempfile.TemporaryDirectory() as directory,patch.object(resources.time,'monotonic',side_effect=lambda:now[0]),\
            patch('tools.waveform_restore.target_export_request',return_value=dict(kind='target_exports')):
            root=Path(directory)
            for mutate in (lambda v:v.update(status='partial'),lambda v:v.update(exit_code=1),lambda v:v['owned_cleanup'].update(complete=False),
                lambda v:v.update(target_operations_validated=False),lambda v:v.update(watchdog_closed=False)):
                value=deepcopy(good); mutate(value)
                with self.subTest(mutation=mutate),patch.object(resources,'measured_job',return_value=value),self.assertRaises(ValueError):
                    resources.supervised_target_exports(object(),root,'a'*32,'b'*64,[],{})
            deadline=resources.PreparationDeadline(until=600.)
            def late(*args,**kwargs): now[0]=601.; return good
            with patch.object(resources,'measured_job',side_effect=late),self.assertRaisesRegex(RuntimeError,'safety.preparation_deadline'):
                resources.supervised_target_exports(object(),root,'a'*32,'b'*64,[],{},deadline=deadline)

    def test_invalid_target_export_request_never_resolves_secrets_or_connects(self):
        from unittest.mock import patch
        from tools import waveform_restore as restore
        self.assertTrue(hasattr(restore,'_target_export_inputs'),'Pinned target request validator absent')
        for value in ({},dict(kind='target_exports',run_root='C:/unowned',run_id='a'*32,source_sha256='b'*64,records=[],references={})):
            with self.subTest(value=value),patch.object(restore,'load_source_authority',side_effect=AssertionError('No authority before run validation')):
                with self.assertRaises(ValueError): restore._target_export_inputs(value)

    def test_target_export_reference_output_and_operation_sets_are_exact(self):
        from unittest.mock import patch
        from dataclasses import asdict
        from tests.waveform_pairing_support import REVIEWER
        from tools import waveform_restore as restore
        run='a'*32; version='00000000-0000-0000-0000-000000000001'; export='00000000-0000-0000-0000-000000000002'
        parent=dict(database=restore.SOURCE,host='127.0.0.1',port=3822,data='owned',fixture_id='c'*32,roles={role:'owned_'+role for role in restore.ROLES})
        authority=dict(parent=parent,sidecar=dict(role='owned_registry'),settings={})
        def unresolved(path,*,resolve):
            self.assertFalse(resolve,'Pure validation must never resolve credentials'); return authority
        with tempfile.TemporaryDirectory() as directory,patch.object(restore,'ROOT',Path(directory)),\
            patch.object(restore,'_current_source_sha256',return_value='b'*64),patch.object(restore,'load_source_authority',side_effect=unresolved):
            root,_=restore.create_run_root(Path(directory)/'logs/waveforms-wp04'/('20261007T000000Z-'+run))
            prior=root/'prior'; prior.mkdir(); (prior/'manifest.json').write_bytes(b'{}')
            file=dict(name='manifest.json',size=2,sha256=sha256(b'{}').hexdigest()); record=dict(version_id=version,export_id=export,actor=asdict(REVIEWER))
            recovery=dict(records=[record],references={version:str(prior)},recovery_downloads=dict(status='passed',monitor_cleanup=True,run_id=run,source_sha256='b'*64,prepared=[dict(record,expected=[file])]))
            (root/'recovery-source.json').write_text(json.dumps(recovery))
            identity=restore.target_identity(run,parent,'owned_registry'); (root/'restore-identity.json').write_text(json.dumps(identity))
            request=restore.target_export_request(root,run,'b'*64,[record],{version:str(prior)})
            restore._target_export_inputs(request)
            for field in ('export_id','actor','duplicate'):
                altered=deepcopy(recovery); bad=deepcopy(request)
                if field=='duplicate': altered['recovery_downloads']['prepared'].append(altered['recovery_downloads']['prepared'][0])
                else:
                    changed='00000000-0000-0000-0000-000000000003' if field=='export_id' else dict(record['actor'],id='different')
                    altered['records'][0][field]=changed; bad['records'][0][field]=changed
                (root/'recovery-source.json').write_text(json.dumps(altered))
                with self.subTest(field=field),self.assertRaises(ValueError): restore._target_export_inputs(bad)
            (root/'recovery-source.json').write_text(json.dumps(recovery))
            for mutate in (lambda r:r.update(run_id='d'*32),lambda r:r.update(source_sha256='d'*64),
                lambda r:r['references'][version].update(relative_path='../escape'),lambda r:r['records'].append(record),
                lambda r:r['references'][version]['files'].append(file)):
                bad=deepcopy(request); mutate(bad)
                with self.subTest(mutation=mutate),self.assertRaises(ValueError): restore._target_export_inputs(bad)
            (root/'restore-identity.json').write_text(json.dumps(dict(identity,database=restore.SOURCE)))
            with self.assertRaises(ValueError): restore._target_export_inputs(request)
            (root/'restore-identity.json').write_text(json.dumps(identity))
            from types import SimpleNamespace
            import time
            class Context:
                def stream_manifest(self): yield b'{}'
                def close(self): pass
            class Exporter:
                def __init__(self,*args,**kwargs): pass
                def describe(self,*args,**kwargs): return SimpleNamespace(dataset_content_sha256=file['sha256'],manifest_bytes=2)
                def files(self,*args,**kwargs): return []
                def open(self,*args,**kwargs): return Context()
            events=[]; resolutions=[]
            def resolve(path,*,resolve): resolutions.append(resolve); return authority
            from tests import waveform_resource_scenarios as resources
            def process(pid):
                p=SimpleNamespace(pid=pid,returncode=None); p.poll=lambda:p.returncode; return p
            child,broker=process(1),process(2)
            class Stack:
                def __init__(self,*args,_created,**kwargs):
                    self.created=_created; self.root=root/'fake-worker'; self.root.mkdir()
                    self.instances=[dict(instance_id='work',component='resource_measure',pid=1,process=child)]
                    self.broker=SimpleNamespace(process=broker,instances=[dict(instance_id='broker',component='broker',pid=2,process=broker)])
                def __enter__(self): self.created(broker); return self
                def __exit__(self,*args): child.returncode=0; broker.returncode=0
                def launch(self,*args):
                    self.created(child)
                    request=json.loads((self.root/'resource-request.json').read_text())
                    value=restore.run_target_exports(request,lambda kind,**data:events.append(dict(event=kind,**data)))
                    (self.root/'resource-result.json').write_text(json.dumps(value))
                    events.append(dict(event='resource_complete')); return child
                def events(self,component,event=None): return [e for e in events if event is None or e['event']==event]
                def stop(self,*args): child.returncode=0
            with patch('backend.waveforms.export.DatasetExporter',Exporter),patch.object(restore,'load_source_authority',side_effect=resolve),\
                patch('tests.waveform_stack.Stack',Stack),patch.object(resources,'process_memory',side_effect=lambda p:dict(pid=p.pid,working_set_bytes=1)),\
                patch.object(resources,'host_resources',return_value=dict(physical_available_bytes=4*1024**3,disk_available_bytes=11*1024**3)):
                measured=resources.measured_job(object(),root,request,deadline=resources.PreparationDeadline(until=time.monotonic()+60))
                actual=measured['result']
            self.assertEqual(resolutions,[False,True,False,False])
            self.assertTrue(measured['watchdog_closed']); self.assertTrue(measured['owned_cleanup']['complete'])
            self.assertTrue(measured['target_operations_validated'])
            output=root/'restored-exports'/version
            value=dict(run_id=run,source_sha256='b'*64,target=identity['database'],exports=[dict(version_id=version,export_id=export,
                manifest=dict(relative_path=version+'/manifest.json',bytes=2,sha256=file['sha256']),files=[],descriptors=[])])
            names=['identity','export_setup','describe:'+version,'file:'+version+'/manifest.json','page:'+version+':0','finalization']
            self.assertEqual([e['operation'] for e in events if e['event']=='target_operation_complete'],names)
            restore._verify_target_exports(request,actual,operations=names)
            restore._verify_target_exports(request,value,operations=names)
            for mutate in (lambda v:v.update(source_sha256='d'*64),lambda v:v['exports'].append(v['exports'][0]),
                lambda v:v['exports'][0]['manifest'].update(bytes=3)):
                bad=deepcopy(value); mutate(bad)
                with self.subTest(mutation=mutate),self.assertRaises(ValueError): restore._verify_target_exports(request,bad,operations=names)
            with self.assertRaises(ValueError): restore._verify_target_exports(request,value,operations=names[:-1])
            (output/'extra').write_bytes(b'x')
            with self.assertRaises(ValueError): restore._verify_target_exports(request,value,operations=names)
            (output/'extra').unlink(); (output/'manifest.json').write_bytes(b'[]')
            with self.assertRaises(ValueError): restore._verify_target_exports(request,value,operations=names)
            (prior/'extra-directory').mkdir()
            with self.assertRaises(ValueError): restore._target_export_inputs(request)

    def test_restored_export_clock_covers_open_empty_eof_and_cleanup(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from dataclasses import asdict
        from tests.waveform_pairing_support import REVIEWER
        from tests.waveform_support import LIMITS
        from tools import waveform_restore as restore
        record=dict(version_id='00000000-0000-0000-0000-000000000001',export_id='00000000-0000-0000-0000-000000000002',actor=asdict(REVIEWER))
        for slow in ('open','eof','close'):
            with self.subTest(slow=slow),tempfile.TemporaryDirectory() as directory:
                now=[0.]; closed=[]
                class Iterator:
                    def __iter__(self): return self
                    def __next__(self):
                        if slow=='eof': now[0]=121.
                        raise StopIteration
                    def close(self):
                        closed.append('iterator')
                        if slow=='close': now[0]=121.
                class Context:
                    def stream_manifest(self): return Iterator()
                    def close(self): closed.append('context')
                class Exporter:
                    def __init__(self,*args,**kwargs): pass
                    def open(self,*args,**kwargs):
                        if slow=='open': now[0]=121.
                        return Context()
                    def files(self,*args,**kwargs): return []
                    def describe(self,*args,**kwargs): return SimpleNamespace(dataset_content_sha256=sha256(b'').hexdigest(),manifest_bytes=0)
                with patch.object(restore.time,'monotonic',side_effect=lambda:now[0]),patch('backend.waveforms.export.DatasetExporter',Exporter):
                    with self.assertRaisesRegex(RuntimeError,'safety.restore_export_deadline'):
                        restore.export_existing(lambda *a:None,[record],Path(directory)/'output',limits=LIMITS)
                self.assertEqual(closed,['iterator','context'])

    def test_restored_export_body_error_survives_all_stream_close_failures(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from dataclasses import asdict
        from tests.waveform_pairing_support import REVIEWER
        from tests.waveform_support import LIMITS
        from tools.waveform_restore import export_existing
        first=ValueError('stream failed'); closed=[]
        class Iterator:
            def __iter__(self): return self
            def __next__(self): raise first
            def close(self): closed.append('iterator'); raise OSError('iterator close')
        class Context:
            def stream_manifest(self): return Iterator()
            def close(self): closed.append('context'); raise OSError('context close')
        class Exporter:
            def __init__(self,*args,**kwargs): pass
            def open(self,*args,**kwargs): return Context()
            def describe(self,*args,**kwargs): return SimpleNamespace(dataset_content_sha256=sha256(b'').hexdigest(),manifest_bytes=0)
        record=dict(version_id='00000000-0000-0000-0000-000000000001',export_id='00000000-0000-0000-0000-000000000002',actor=asdict(REVIEWER))
        with tempfile.TemporaryDirectory() as directory,patch('backend.waveforms.export.DatasetExporter',Exporter):
            with self.assertRaises(ValueError) as caught: export_existing(lambda *a:None,[record],Path(directory)/'output',limits=LIMITS)
        self.assertIs(caught.exception,first); self.assertEqual(closed,['iterator','context'])
        self.assertEqual(len(first.__notes__),2)

    def test_broker_owner_is_registered_before_readiness_or_config_can_block(self):
        import threading,time
        from types import SimpleNamespace
        from unittest.mock import patch
        from tests import waveform_stack as stacks
        from tests.waveform_resource_scenarios import ResourceWatchdog
        for blocked in ('readiness','config'):
            with self.subTest(blocked=blocked):
                killed=threading.Event(); registered=[]; owned=[]
                class Process:
                    pid=12345; returncode=None
                    def poll(self): return self.returncode
                    def kill(self): self.returncode=1; killed.set()
                    terminate=kill
                    def wait(self,timeout): return self.returncode
                def popen(*args,**kwargs):
                    p=Process(); owned.append(p); return p
                def stall():
                    self.assertTrue(killed.wait(2),'Created broker escaped watchdog while caller blocked')
                    raise RuntimeError('injected startup I/O stopped')
                class Ready:
                    def __enter__(self): return self
                    def __exit__(self,*args): pass
                class Bound:
                    def __enter__(self): return self
                    def __exit__(self,*args): pass
                    def bind(self,address): pass
                    def getsockname(self): return ('127.0.0.1',33333)
                watch=ResourceWatchdog(until=time.monotonic()+.15)
                def created(process): registered.append(process); watch.register(process)
                stack=stacks.Stack(object()); stack._created=created
                with patch.object(stacks.subprocess,'Popen',side_effect=popen),patch.object(stacks.subprocess,'run',return_value=SimpleNamespace(returncode=0)),\
                    patch.object(stacks.Path,'is_file',return_value=True),patch.object(stacks.socket,'socket',Bound),\
                    patch.object(stacks.socket,'create_connection',side_effect=(lambda *a,**k:stall()) if blocked=='readiness' else lambda *a,**k:Ready()),\
                    patch.object(stacks,'sections_for',side_effect=lambda *a,**k:stall()),patch.object(stack,'preserve_evidence',return_value=None):
                    watch.start()
                    try:
                        with self.assertRaisesRegex(RuntimeError,'injected startup'): stack.__enter__()
                    finally: watch.close()
                self.assertEqual(registered,owned); self.assertEqual(len(owned),1)
                self.assertIs(stack.broker.instances[0]['process'],owned[0]); self.assertEqual(owned[0].poll(),1)
                self.assertTrue(stack.broker.log.closed)

    def test_broker_rejected_registration_preserves_owner_and_first_error(self):
        import io
        from types import SimpleNamespace
        from unittest.mock import patch
        from tests import waveform_stack as stacks
        first=RuntimeError('registration rejected'); calls=[]
        class Process:
            pid=23456; returncode=None
            def poll(self): return self.returncode
            def terminate(self): calls.append('terminate'); raise OSError('terminate failed')
            def kill(self): self.returncode=1; calls.append('kill')
            def wait(self,timeout): return self.returncode
        process=Process(); log=io.BytesIO()
        stack=stacks.Stack(object()); stack._created=lambda p:(_ for _ in ()).throw(first)
        def setup(broker,root,gateways,**kwargs):
            broker.root=root; broker.clients=[]; broker.process=None; broker.log=None; broker.instances=[]
            broker.executable=Path('fake'); broker.config=Path('fake.conf'); broker.port=33333
            broker._created=kwargs.get('_created')
            broker.start()
        with patch.object(stacks.Broker,'__init__',setup),patch.object(stacks.subprocess,'Popen',return_value=process),\
            patch.object(stacks.Path,'open',return_value=log),patch.object(stacks.socket,'create_connection',side_effect=AssertionError('Readiness must follow registration')),\
            patch.object(stack,'preserve_evidence',return_value=None):
            with self.assertRaises(RuntimeError) as caught: stack.__enter__()
        self.assertIs(caught.exception,first); self.assertEqual(calls,['terminate','kill'])
        self.assertIs(stack.broker.instances[0]['process'],process); self.assertTrue(log.closed)

    def test_monitor_and_work_register_startup_owner_once_and_keep_failed_envelope(self):
        import threading,time
        from unittest.mock import patch
        from tests import waveform_stack as stacks,waveform_resource_scenarios as resources
        for caller in ('monitor','work'):
            with self.subTest(caller=caller),tempfile.TemporaryDirectory() as directory:
                root=Path(directory); killed=threading.Event(); registrations=[]
                class Process:
                    pid=34567; returncode=None
                    def poll(self): return self.returncode
                    def kill(self): self.returncode=1; killed.set()
                    terminate=kill
                    def wait(self,timeout): return self.returncode
                class Watch(resources.ResourceWatchdog):
                    def __init__(self,**kwargs): kwargs['seconds']=.15; super().__init__(**kwargs)
                    def register(self,process): registrations.append(process); return super().register(process)
                process=Process()
                def setup(broker,path,gateways,**kwargs):
                    broker.root=path; broker.clients=[]; broker.process=None; broker.log=None; broker.instances=[]
                    broker.executable=Path('fake'); broker.config=Path('fake.conf'); broker.port=33333; broker._created=kwargs.get('_created')
                    broker.start()
                def blocked(*args,**kwargs):
                    self.assertTrue(killed.wait(2),'Caller did not supply early owner hook')
                    raise OSError('readiness interrupted')
                with patch.object(stacks.Broker,'__init__',setup),patch.object(stacks.subprocess,'Popen',return_value=process),\
                    patch.object(stacks.socket,'create_connection',side_effect=blocked),patch.object(stacks.Stack,'preserve_evidence',return_value=None),\
                    patch.object(resources,'ResourceWatchdog',Watch):
                    deadline=resources.PreparationDeadline()
                    with self.assertRaisesRegex(RuntimeError,'safety.operation_deadline'):
                        if caller=='monitor':
                            monitor=resources.RecoveryStorage(object(),root,deadline=deadline,outer=deadline.until)
                            monitor.__enter__()
                        else: resources.measured_job(object(),root,dict(kind='dataset'),deadline=deadline)
                self.assertEqual(registrations,[process]); self.assertEqual(process.poll(),1)
                value=json.loads(next(root.glob('measurement-*/result.json')).read_text())
                self.assertEqual(value['status'],'partial'); self.assertTrue(value['owned_cleanup']['complete'])
                self.assertEqual(value['owned_cleanup']['processes'][0]['pid'],34567)

    def test_broker_default_restart_and_expired_registration_keep_exact_owners(self):
        import io
        from unittest.mock import patch
        from tests import waveform_stack as stacks
        from tests.waveform_resource_scenarios import ResourceWatchdog
        class Process:
            def __init__(self,pid): self.pid=pid; self.returncode=None
            def poll(self): return self.returncode
            def kill(self): self.returncode=1
            terminate=kill
            def wait(self,timeout): return self.returncode
        class Ready:
            def __enter__(self): return self
            def __exit__(self,*args): pass
        with tempfile.TemporaryDirectory() as directory:
            broker=stacks.Broker.__new__(stacks.Broker)
            broker.root=Path(directory); broker.clients=[]; broker.process=None; broker.log=None; broker.instances=[]
            broker.executable=Path('fake'); broker.config=Path('fake.conf'); broker.port=33333; broker._created=None
            processes=[Process(1),Process(2),Process(3)]; logs=[io.BytesIO() for _ in processes]
            with patch.object(stacks.subprocess,'Popen',side_effect=processes),patch.object(stacks.Path,'open',side_effect=logs),\
                patch.object(stacks.socket,'create_connection',return_value=Ready()):
                broker.start(); broker.stop(); broker.start(); broker.stop()
                watch=ResourceWatchdog(until=0.,clock=lambda:1.)
                broker._created=lambda owner:watch.register(owner.process)
                with self.assertRaisesRegex(RuntimeError,'safety.preparation_deadline') as caught: broker.start()
                self.assertIs(caught.exception,watch.failure); watch.close()
            self.assertEqual([i['process'] for i in broker.instances],processes)
            self.assertTrue(all(p.poll()==1 for p in processes)); self.assertTrue(all(log.closed for log in logs))
            self.assertEqual(len({i['instance_id'] for i in broker.instances}),3)

    def test_worker_created_hook_retains_metadata_and_closes_rejected_dead_owner(self):
        import io
        from types import SimpleNamespace
        from unittest.mock import patch
        from tests.waveform_stack import Stack
        for reject in (False,True):
            with self.subTest(reject=reject):
                first=RuntimeError('late owner rejected'); registered=[]
                process=SimpleNamespace(pid=45678,returncode=None,stdin=io.BytesIO())
                process.poll=lambda:process.returncode
                process.wait=lambda **kwargs:setattr(process,'returncode',0)
                stack=Stack(object()); stack.broker=SimpleNamespace(password='synthetic',close=lambda:None,instances=[])
                stack.config=stack.root/'unused.toml'
                def created(owner):
                    self.assertIs(stack.instances[-1]['process'],owner); registered.append(owner)
                    if reject:
                        owner.returncode=1  # The deadline hook has already killed/reaped it.
                        raise first
                stack._created=created if reject else None
                try:
                    with patch('tests.waveform_stack.subprocess.Popen',return_value=process):
                        if reject:
                            with self.assertRaises(RuntimeError) as caught: stack.launch('resource_measure')
                            self.assertIs(caught.exception,first)
                            self.assertTrue(process.stdin.closed,'Rejected reaped owner retained an open stdin handle')
                            self.assertTrue(stack.handles[0].closed)
                        else: self.assertIs(stack.launch('resource_measure'),process)
                    self.assertEqual(registered,[process] if reject else [])
                    self.assertEqual(stack.instances[0]['component'],'resource_measure')
                finally: stack.close()

    def test_independent_watchdog_aborts_while_caller_is_blocked(self):
        import threading,time
        from tests import waveform_resource_scenarios as resources
        self.assertTrue(hasattr(resources,'ResourceWatchdog'),'Independent deadline watchdog absent')
        for stage in (False,True):
            with self.subTest(stage=stage):
                killed=threading.Event()
                class Process:
                    def __init__(self): self.returncode=None
                    def poll(self): return self.returncode
                    def kill(self): self.returncode=1; killed.set()
                    def wait(self,timeout): return self.returncode
                began=time.monotonic(); process=Process()
                watch=resources.ResourceWatchdog(until=began+(.15 if stage else 10),seconds=.15)
                watch.register(process); watch.start_operation('work',0,began)
                watch.start()
                try:
                    # Simulates a caller stuck in DB/stat/log I/O, without polling it.
                    self.assertTrue(killed.wait(2),'Caller polling must not be necessary to abort')
                    with self.assertRaisesRegex(RuntimeError,'safety.'): watch.check()
                finally: watch.close()
                self.assertFalse(watch.thread.is_alive()); self.assertEqual(process.poll(),1)

    def test_watchdog_events_and_late_registration_cannot_revive_first_timeout(self):
        from tests import waveform_resource_scenarios as resources
        self.assertTrue(hasattr(resources,'ResourceWatchdog'),'Independent deadline watchdog absent')
        now=[0.]; closed=[]
        class Process:
            returncode=None
            def poll(self): return self.returncode
            def kill(self): self.returncode=1; closed.append(self)
            def wait(self,timeout): return self.returncode
        watch=resources.ResourceWatchdog(until=1000.,seconds=120.,clock=lambda:now[0])
        watch.start_operation('work',0,0.)
        now[0]=100.; watch.start_operation('work',0,0.)
        now[0]=121.
        with self.assertRaises(RuntimeError) as timeout: watch.start_operation('work',1,121.)
        process=Process()
        with self.assertRaises(RuntimeError) as late: watch.register(process)
        self.assertIs(timeout.exception,late.exception); self.assertEqual(closed,[process])
        watch.close(); self.assertEqual(process.poll(),1)

    def test_watchdog_signals_every_owned_handle_before_reaping_and_late_close_registration(self):
        from tests.waveform_resource_scenarios import ResourceWatchdog
        calls=[]; first=RuntimeError('safety.operation_deadline')
        class Process:
            def __init__(self,name): self.name=name; self.returncode=None
            def poll(self): return self.returncode
            def kill(self):
                calls.append('kill-'+self.name); self.returncode=1
                if self.name=='one': raise OSError('first kill reported failure')
            def wait(self,timeout): calls.append('wait-'+self.name); return self.returncode
        watch=ResourceWatchdog(until=1000.,clock=lambda:0.)
        one,two=Process('one'),Process('two'); watch.register(one); watch.register(two)
        with self.assertRaisesRegex(RuntimeError,'live_handle_removal'): watch.retire(one)
        watch.fail(first); watch.close()
        self.assertEqual(calls,['kill-one','kill-two','wait-one','wait-two'])
        self.assertIs(watch.failure,first); self.assertFalse(watch.live_handles())
        late=Process('late')
        with self.assertRaises(RuntimeError) as caught: watch.register(late)
        self.assertIs(caught.exception,first); self.assertEqual(late.poll(),1)

    def test_storage_events_validate_freshness_without_parent_sampling(self):
        from unittest.mock import patch
        from tests import waveform_resource_scenarios as resources
        self.assertTrue(hasattr(resources.RecoveryStorage,'accept'),'Owned storage event consumer absent')
        now=[0.]
        with tempfile.TemporaryDirectory() as directory,patch.object(resources.time,'monotonic',side_effect=lambda:now[0]),\
            patch.object(resources,'storage_sample',side_effect=AssertionError('No DB/stat in parent')):
            root=Path(directory); deadline=resources.PreparationDeadline()
            monitor=resources.RecoveryStorage(object(),root,deadline=deadline,outer=3600.)
            monitor.accept(dict(event='storage_started',sequence=1,started=0.))
            now[0]=1.; monitor.accept(dict(event='storage_sample',sequence=1,started=0.,completed=1.,sample=dict(database_bytes=100,artifact_bytes=1,logical_raw_bytes=0)))
            self.assertEqual(monitor.baseline['database_bytes'],100)
            self.assertFalse(monitor.fresh_after(1.)); self.assertTrue(monitor.fresh_after(0.))
            with self.assertRaises(ValueError): monitor.accept(dict(event='storage_sample',sequence=1,started=0.,completed=1.,sample={}))
            now[0]=5.; monitor.accept(dict(event='storage_started',sequence=2,started=5.))
            now[0]=6.
            with self.assertRaisesRegex(RuntimeError,'safety.database_growth'):
                monitor.accept(dict(event='storage_sample',sequence=2,started=5.,completed=6.,sample=dict(database_bytes=2*1024**3+101,artifact_bytes=1,logical_raw_bytes=0)))
            rows=[json.loads(line) for line in (root/'recovery-storage.jsonl').read_text().splitlines()]
            self.assertEqual(rows[-1]['sample']['database_bytes'],2*1024**3+101)

    def test_storage_close_preserves_timeout_and_attempts_every_owner(self):
        from types import SimpleNamespace
        from tests import waveform_resource_scenarios as resources
        with tempfile.TemporaryDirectory() as directory:
            monitor=resources.RecoveryStorage(object(),Path(directory),deadline=resources.PreparationDeadline(),outer=10**20)
            first=RuntimeError('safety.operation_deadline'); monitor.watchdog.fail(first); calls=[]
            def fail(stage): calls.append(stage); raise OSError(stage)
            monitor.stack=SimpleNamespace(processes={'resource_measure':object()},stop=lambda *a:fail('stop'),__exit__=lambda *a:fail('stack'))
            monitor.evidence=SimpleNamespace(__exit__=lambda *a:fail('evidence'),complete=lambda *a:fail('complete'))
            with self.assertRaises(RuntimeError) as caught: monitor.__exit__(None,None,None)
            self.assertIs(caught.exception,first)
            self.assertEqual(calls,['stop','stack','evidence'])
            self.assertTrue(monitor.closed); self.assertFalse(monitor.watchdog.thread.is_alive())

    def test_recovery_gate_requires_monitor_closed_before_success(self):
        from tests.waveform_resource_scenarios import validate_recovery_measurement
        value=complete_report()['recovery_downloads']
        value.pop('monitor_cleanup',None)
        with self.assertRaises(ValueError): validate_recovery_measurement(value,run_id=value['run_id'],source_sha256=value['source_sha256'])

    def test_recovery_never_hands_off_before_monitor_closure_or_after_cleanup_deadline(self):
        from unittest.mock import patch
        from tests import waveform_experiments as experiments,waveform_resource_scenarios as resources
        for residue in (True,False):
            now=[0.]; calls=[]
            class Monitor:
                def __init__(self,*args,**kwargs): pass
                def __enter__(self): calls.append('entered'); return self
                def check(self,**kwargs): pass
                def __exit__(self,*args):
                    calls.append('closed')
                    if not residue: now[0]=3601.
                def closure(self): return not residue
            def inner(fixture,root,volumes,budget,run,source,measurement):
                budget.start_download(root); measurement['clock']={}; return {}
            with self.subTest(residue=residue),tempfile.TemporaryDirectory() as directory,\
                patch.object(resources.time,'monotonic',side_effect=lambda:now[0]),patch.object(resources,'RecoveryStorage',Monitor),\
                patch.object(experiments,'_prepare_recovery',side_effect=inner),patch.object(resources,'validate_recovery_measurement') as qualify:
                root=Path(directory)
                with self.assertRaisesRegex(RuntimeError,'safety.storage_monitor_residue' if residue else 'safety.preparation_deadline'):
                    experiments.prepare_recovery(object(),root,run_id='a'*32,source_sha256='b'*64)
                self.assertEqual(calls,['entered','closed']); qualify.assert_not_called()
                saved=json.loads((root/'recovery-downloads.json').read_text())
                self.assertEqual(saved['status'],'partial'); self.assertNotIn('monitor_cleanup',saved)

    def test_http_cleanup_combinations_preserve_first_failure_and_all_attempts(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from tests import waveform_resource_scenarios as resources
        for failures in (('socket',),('sampler','child'),('request','socket','sampler','child','events'),()):
            with self.subTest(failures=failures),tempfile.TemporaryDirectory() as directory:
                root=Path(directory); calls=[]; errors={stage:OSError(stage) for stage in failures}
                def act(stage):
                    calls.append(stage)
                    if stage in errors: raise errors[stage]
                class Connection:
                    def request(self,*args,**kwargs): act('request')
                    def getresponse(self): return SimpleNamespace(status=200,read=lambda:b'{"data":{}}')
                    def close(self): act('socket')
                class Sampler:
                    failure=None
                    def __init__(self,*args,**kwargs): self.alive=True; self.thread=SimpleNamespace(is_alive=lambda:self.alive)
                    def __enter__(self): return self
                    def __exit__(self,*args): act('sampler'); self.alive=False
                    def check(self): pass
                class Stack:
                    def __init__(self,*args,**kwargs):
                        self.root=root/'child'; self.root.mkdir(); (self.root/'http-token').write_text('test')
                        self.sections={'waveforms':{}}; self.broker=None
                        self.process=SimpleNamespace(returncode=None,poll=lambda:self.process.returncode)
                        self.instances=[dict(instance_id='pure',component='http_measure',pid=1,process=self.process)]
                    def __enter__(self): return self
                    def __exit__(self,*args): self.process.returncode=0
                    def launch(self,*args,**kwargs): return self.process
                    def await_event(self,*args): return dict(port=1,remaining_threads=[],connections=0,output_owners=0)
                    def stop(self,*args): act('child'); self.process.returncode=0
                    def events(self,*args): act('events'); return []
                with patch('http.client.HTTPConnection',return_value=Connection()),patch('tests.waveform_stack.Stack',Stack),\
                    patch('tests.waveform_measurements.OwnedSampler',Sampler),patch.object(resources,'process_memory',return_value={}),\
                    patch.object(resources,'host_resources',return_value={}):
                    if failures:
                        with self.assertRaises(OSError) as caught: resources.measured_http(object(),root,'/pure')
                        self.assertIs(caught.exception,errors[failures[0]])
                    else:
                        self.assertEqual(resources.measured_http(object(),root,'/pure')['status'],'passed')
                self.assertEqual(calls.count('socket'),1)
                self.assertTrue(all(stage in calls for stage in ('sampler','child','events')))
                value=json.loads(next(root.glob('measurement-*/result.json')).read_text())
                self.assertEqual(value['socket_closed'],'socket' not in failures)
                self.assertEqual(value['sampler_joined'],'sampler' not in failures)
                self.assertEqual(value['child_stopped'],'child' not in failures)
                self.assertEqual(value['status'],'partial' if failures else 'passed')
                if len(failures)>1:
                    self.assertTrue(all(any(stage in note for note in caught.exception.__notes__) for stage in failures[1:]))

    def test_output_owner_socket_failure_still_exits_sampler_and_stops_owned_child(self):
        from types import SimpleNamespace
        from tests import waveform_resource_scenarios as resources
        calls=[]; socket_error=OSError('socket close')
        class Socket:
            def close(self): calls.append('socket'); raise socket_error
        class Monitor:
            samples=[]
            def __exit__(self,*args): calls.append('sampler')
        class Stack:
            def stop(self,name): calls.append('child')
        class Evidence:
            result={}
            def events(self,name): calls.append('events')
        with self.assertRaises(OSError) as caught:
            resources._finish_output_owners([Socket()],Monitor(),Stack(),Evidence(),[],0,error=None)
        self.assertIs(caught.exception,socket_error)
        self.assertEqual(calls,['socket','sampler','child','events'])

    def test_sender_cleanup_keeps_body_error_and_attempts_later_owned_stages(self):
        from types import SimpleNamespace
        from tests import waveform_resource_scenarios as resources
        calls=[]; primary=ValueError('body'); stop_error=OSError('stop')
        class Stack:
            processes={'sender_measure':SimpleNamespace(returncode=0), 'publish':SimpleNamespace(returncode=0)}
            def stop(self,name):
                calls.append('stop '+name)
                if name=='sender_measure': raise stop_error
            def events(self,name,*args,**kwargs): calls.append('events '+name); return []
        result={}; resources._finish_sender_workers(Stack(),SimpleNamespace(returncode=0),result,0,error=primary)
        self.assertEqual(calls,['stop sender_measure','events sender_measure','stop sender_measure','events sender_measure','stop publish','events publish'])
        self.assertIn('stop',primary.__notes__[0])

    def test_sender_sample_close_failure_still_saves_cleanup_envelope(self):
        from types import SimpleNamespace
        from tests import waveform_resource_scenarios as resources
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); error=OSError('samples close'); calls=[]
            class Samples:
                def close(self): calls.append('samples'); raise error
            process=SimpleNamespace(poll=lambda:0)
            stack=SimpleNamespace(instances=[dict(instance_id='one',component='sender',pid=1,process=process)],broker=None)
            result={}
            with self.assertRaises(OSError) as caught:
                resources._finish_sender_evidence(Samples(),stack,result,root,0,error=None)
            self.assertIs(caught.exception,error)
            self.assertEqual(calls,['samples'])
            self.assertTrue((root/'result.json').is_file())
            self.assertTrue(result['cleanup']['complete'])

    def test_sender_abort_failure_does_not_replace_body_error(self):
        from types import SimpleNamespace
        from tests import waveform_resource_scenarios as resources
        body=ValueError('body'); abort=OSError('abort')
        class Stack:
            def kill_owned(self,*args,**kwargs): raise abort
        resources._abort_sender(Stack(),SimpleNamespace(poll=lambda:None),{},body)
        self.assertIn('OSError',body.__notes__[0])

    def test_backup_guard_unlock_failure_preserves_primary(self):
        from types import SimpleNamespace
        from tools import waveform_restore as restore
        calls=[]; primary=ValueError('restore'); unlock_error=OSError('unlock')
        class Guard:
            closed=False
            def execute(self,*args): calls.append('unlock'); raise unlock_error
        restore._unlock_backup_guard(SimpleNamespace(guard=Guard()),primary)
        self.assertEqual(calls,['unlock']); self.assertIn('OSError',primary.__notes__[0])
        with self.assertRaises(OSError) as caught: restore._unlock_backup_guard(SimpleNamespace(guard=Guard()),None)
        self.assertIs(caught.exception,unlock_error)

    def test_lease_gate_requires_fresh_complete_bound_actual_bytes(self):
        tool=self.tool(); report=complete_report()
        report.pop('lease_measurement'); report['artifacts']=[a for a in report['artifacts'] if a['relative_path']!='lease-measurement.json']
        self.assertIn('lease.invalid',tool.qualify(report))
        value=dict(schema='waveform.lease-measurement/1',status='passed',run_id=report['run_id'],
            source_sha256=tool.inventory_sha256(report['source']['files_sha256']),
            assertions={k:True for k in ('one_claim','owned_64','full_unowned_refused','expired_owned_64','reclaimed_owned_only',
                'token_version_changed','stale_renew_refused','stale_run_refused','stale_completion_unchanged','replacement_completed',
                'rollback_refused','rollback_cleared_owned','two_workers_renewed','two_workers_completed','workers_clean','query_plans')},
            timings=[dict(operation='claim',elapsed_seconds=.1)],
            plans=[dict(cardinality=n,state=s,query='SELECT capture_pk FROM waveforms.assembly_jobs WHERE intake_gateway_id=%s',params=['GW'],
                client_seconds=.001,plan={'Execution Time':.01,'Plan':{'Actual Rows':1,'Shared Hit Blocks':1}})
                for n,s in ((1,'available'),(64,'available'),(66,'full-live'),(66,'expired-still-owned')) for _ in range(2)],
            dual_workers=dict(peak_connections=4,completed=2,configured_connections=4),
            cleanup=dict(complete=True,processes=[dict(component='assemble',pid=1,instance_id='owned',exit_code=0)]))
        report['lease_measurement']=value
        value['dual_workers']=complete_report()['lease_measurement']['dual_workers']
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/'lease-measurement.json'; path.write_text(json.dumps(value),encoding='utf-8')
            report['artifacts'].append(dict(relative_path=path.name,size=path.stat().st_size,sha256=tool.digest_file(path)))
            self.assertNotIn('lease.invalid',tool.qualify(report)); self.assertEqual(tool.verify_lease_artifact(root,report),())
            for change in (lambda v:v.update(status='partial'),lambda v:v.update(run_id='f'*32),lambda v:v.update(source_sha256='0'*64),
                lambda v:v['assertions'].update(stale_run_refused=False),lambda v:v['cleanup'].update(complete=False),
                lambda v:v.update(plans=[]),lambda v:v['timings'][0].update(elapsed_seconds=121)):
                changed=deepcopy(report); change(changed['lease_measurement']); self.assertIn('lease.invalid',tool.qualify(changed))
            path.write_text('{}',encoding='utf-8')
            self.assertEqual(tool.verify_lease_artifact(root,report),('lease.artifact_invalid',))
            report['artifacts'][-1].update(size=2,sha256=tool.digest_file(path))
            self.assertEqual(tool.verify_lease_artifact(root,report),('lease.artifact_invalid',))

    def test_lease_gate_rejects_missing_rss_guard_or_sampler_cleanup(self):
        tool=self.tool(); report=complete_report()
        self.assertTrue(tool.valid_lease_measurement(report))
        for mutate in (lambda d:d.pop('continuous_samples'),lambda d:d.update(sampler_joined=False),
            lambda d:d['owned_cleanup'].update(complete=False),lambda d:d['metadata'].update(source_sha256='0'*64),
            lambda d:d['continuous_samples'][0]['processes'][0].update(working_set_bytes=2*1024**3)):
            changed=deepcopy(report); mutate(changed['lease_measurement']['dual_workers'])
            self.assertFalse(tool.valid_lease_measurement(changed))

    def test_cli_refuses_lease_section_file_mismatch_before_full_regression(self):
        self.check_cli_artifact_refusal('lease')

    def test_cli_refuses_recovery_section_file_mismatch_before_full_regression(self):
        self.check_cli_artifact_refusal('recovery')

    def check_cli_artifact_refusal(self,section):
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        from tools import waveform_restore
        tool=self.tool(); value=complete_report()
        with tempfile.TemporaryDirectory() as directory:
            owned=Path(directory); root=owned/'logs/waveforms-wp04'/('20261005T000000Z-'+'a'*32)
            def experiments(path,*args,**kwargs):
                (path/'lease-measurement.json').write_text('{}' if section=='lease' else json.dumps(value['lease_measurement']),encoding='utf-8')
                item=next(a for a in value['artifacts'] if a['relative_path']=='lease-measurement.json')
                item.update(size=(path/'lease-measurement.json').stat().st_size,sha256=tool.digest_file(path/'lease-measurement.json'))
                if section=='recovery':
                    (path/'recovery-downloads.json').write_text('{}',encoding='utf-8')
                    value['artifacts'].append(dict(relative_path='recovery-downloads.json',size=2,sha256=sha256(b'{}').hexdigest()))
                return value
            with patch.object(waveform_restore,'ROOT',owned),patch.object(waveform_restore,'load_source_authority'),\
                patch.object(tool,'source_inventory',return_value=value['source']['files_sha256']),\
                patch('tools.verify_ingest_reliability.protected_snapshot',return_value={}),\
                patch('tests.waveform_experiments.run_experiments',side_effect=experiments),\
                patch.object(tool.subprocess,'run',side_effect=AssertionError('No fullgate spawn')) as spawn,redirect_stdout(StringIO()):
                self.assertEqual(tool.main(['--config','unused','--evidence-dir',str(root),'--include-existing-regression']),1)
                spawn.assert_not_called()
            result=json.loads((root/'report.json').read_text(encoding='utf-8'))
            self.assertEqual(result['error_type'],'RuntimeError')
            self.assertIn(section+'.artifact_invalid',result['qualification_reasons'])

    def test_preparation_deadline_is_shared_and_refuses_next_work(self):
        from unittest.mock import patch
        from tests import waveform_resource_scenarios as resources
        from tests.waveform_experiments import prepare_recovery
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(resources.time,'monotonic',return_value=10.):
                deadline=resources.PreparationDeadline(until=20.)
            with patch.object(resources.time,'monotonic',return_value=18.):
                self.assertEqual(deadline.check(root,'population')['remaining_seconds'],2.)
            with patch.object(resources.time,'monotonic',return_value=20.01):
                for call in (lambda:resources.prepare_members(object(),root,count=1,deadline=deadline),
                             lambda:resources.measured_job(object(),root,dict(kind='intake'),deadline=deadline),
                             lambda:prepare_recovery(object(),root,volumes=(1,),deadline=deadline)):
                    with self.assertRaisesRegex(RuntimeError,'safety.preparation_deadline'): call()
            rows=[json.loads(line) for line in (root/'preparation-clock.jsonl').read_text().splitlines()]
            self.assertEqual(rows[0]['began'],10.); self.assertEqual(rows[-1]['deadline'],20.)
            self.assertLess(rows[-1]['remaining_seconds'],0)
            self.assertTrue(all(row['deadline']==20. for row in rows))
            with patch.object(resources.time,'monotonic',return_value=100.):
                self.assertEqual(resources.PreparationDeadline(until=5000.).until,1900.)

    def test_recovery_stage_budgets_never_resurrect_caller_or_late_preparation(self):
        from unittest.mock import patch
        from tests import waveform_resource_scenarios as resources
        self.assertTrue(hasattr(resources,'RecoveryBudget'),'Two-stage recovery budget absent')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(resources.time,'monotonic',return_value=100.):
                budget=resources.RecoveryBudget()
                short=resources.RecoveryBudget(until=700.)
            self.assertEqual(budget.preparation.until,1900.); self.assertEqual(budget.outer,3700.)
            with patch.object(resources.time,'monotonic',return_value=500.):
                download=budget.start_download(root)
                self.assertEqual(download.until,2300.)
                self.assertEqual(short.start_download(root).until,700.)
                with self.assertRaises(ValueError): budget.start_download(root)
            with patch.object(resources.time,'monotonic',return_value=701.):
                with self.assertRaisesRegex(RuntimeError,'safety.preparation_deadline'): short.finish(root)
            with patch.object(resources.time,'monotonic',return_value=100.): late=resources.RecoveryBudget()
            with patch.object(resources.time,'monotonic',return_value=1901.):
                with self.assertRaisesRegex(RuntimeError,'safety.preparation_deadline'): late.start_download(root)

    def test_recovery_download_gate_rejects_incomplete_mixed_identity_and_late_files(self):
        from tests import waveform_resource_scenarios as resources
        self.assertTrue(hasattr(resources,'validate_recovery_measurement'),'Complete download gate absent')
        value=dict(schema='waveform.recovery-downloads/2',policy='R88-two-stage',status='passed',run_id='a'*32,source_sha256='b'*64,
            volumes=[1,2],clock=dict(began=0.,outer=3600.,preparation_deadline=1800.,prepared=10.,download_started=11.,download_deadline=1811.,completed=20.),
            prepared=[],downloads=[],owned_cleanup=True,monitor_cleanup=True)
        for n,count in enumerate((2,1,2)):
            version=dict(version_id=str(n),export_id=str(n+10),actor=dict(account='synthetic'),members=count,
                run_id='a'*32,source_sha256='b'*64,
                expected=[dict(name='manifest.json',sha256='c'*64,size=3)]+[dict(name=str(i)+'.owpwave',sha256='d'*64,size=2,key=dict(capture_id=str(i))) for i in range(count)])
            value['prepared'].append(version)
            for file in version['expected']:
                value['downloads'].append(dict(version_id=version['version_id'],export_id=version['export_id'],**file,
                    relative_path=version['version_id']+'/'+file['name'],started=12.,completed=13.))
        resources.validate_recovery_measurement(value,run_id='a'*32,source_sha256='b'*64,volumes=(1,2))
        for mutate in (lambda v:v['downloads'].pop(),lambda v:v['downloads'].append(v['downloads'][0]),
            lambda v:v.update(run_id='e'*32),lambda v:v.update(source_sha256='e'*64),lambda v:v.update(policy='old-1800'),
            lambda v:v.update(owned_cleanup=False),lambda v:v['downloads'][0].update(completed=1812.),
            lambda v:v['downloads'][0].update(started=9.),lambda v:v['downloads'][0].update(completed=133.),
            lambda v:v['prepared'][0]['expected'][0].update(sha256='e'*64),lambda v:v['clock'].update(completed=3601.)):
            broken=deepcopy(value); mutate(broken)
            with self.subTest(mutation=mutate),self.assertRaises(ValueError):
                resources.validate_recovery_measurement(broken,run_id='a'*32,source_sha256='b'*64,volumes=(1,2))

    def test_actual_recovery_artifact_refuses_missing_tampered_and_unbound_bytes(self):
        from tools import verify_waveforms as tool
        self.assertTrue(hasattr(tool,'verify_recovery_artifact'),'Actual recovery file authentication absent')
        report=complete_report(); value=report['recovery_downloads']
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for f in value['downloads']:
                path=root/f['relative_path']; path.parent.mkdir(exist_ok=True); path.write_bytes(b'{}' if f['name']=='manifest.json' else b'x')
            path=root/'recovery-downloads.json'; path.write_text(json.dumps(value),encoding='utf-8')
            report['artifacts']=[dict(relative_path=p.relative_to(root).as_posix(),size=p.stat().st_size,sha256=tool.digest_file(p)) for p in root.rglob('*') if p.is_file()]
            self.assertEqual(tool.verify_recovery_artifact(root,report),())
            first=root/value['downloads'][0]['relative_path']; first.write_bytes(b'[]')
            self.assertEqual(tool.verify_recovery_artifact(root,report),('recovery.artifact_invalid',))
            first.write_bytes(b'{}'); value['downloads'].pop()
            self.assertIn('recovery.invalid',tool.qualify(report))
            self.assertEqual(tool.verify_recovery_artifact(root,report),('recovery.artifact_invalid',))

    def test_recovery_progress_is_append_only_not_a_completed_baseline(self):
        from tests import waveform_experiments as experiments
        self.assertTrue(hasattr(experiments,'record_recovery'),'Incremental recovery journal absent')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); value=dict(status='partial',downloads=[])
            experiments.record_recovery(root,value)
            value['downloads'].append(dict(name='manifest.json')); experiments.record_recovery(root,value)
            rows=[json.loads(line) for line in (root/'recovery-progress.jsonl').read_text().splitlines()]
            self.assertEqual([len(r['downloads']) for r in rows],[0,1])
            self.assertFalse((root/'recovery-downloads.json').exists())
            experiments.save_result(root,'recovery-downloads',value)
            with self.assertRaises(FileExistsError): experiments.save_result(root,'recovery-downloads',value)

    def test_final_measurement_save_failure_does_not_mask_first_error_or_owned_cleanup(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from tests.waveform_resource_scenarios import MeasurementEvidence
        closed=[]; first=RuntimeError('first operation failed'); original=Path.open
        class Owned:
            instances=[]; broker=None
            def __enter__(self): return self
            def __exit__(self,*args): closed.append(True)
        def fail_final(path,*args,**kwargs):
            if path.name=='result.json': raise OSError('final evidence write failed')
            return original(path,*args,**kwargs)
        with tempfile.TemporaryDirectory() as directory,patch.object(Path,'open',fail_final):
            with self.assertRaises(RuntimeError) as caught:
                with MeasurementEvidence(directory,'resource',{}) as evidence,Owned() as owned:
                    evidence.stack=owned
                    raise first
            self.assertIs(caught.exception,first); self.assertEqual(closed,[True])
            self.assertTrue(evidence.samples.closed and evidence.phases.closed)
            self.assertTrue(any('OSError' in n for n in first.__notes__))

    def test_recovery_resource_guard_keeps_one_baseline_and_records_violations(self):
        from unittest.mock import patch
        from tests import waveform_resource_scenarios as resources
        self.assertTrue(hasattr(resources,'RecoveryStorage'),'Shared physical storage guard absent')
        now=[0.]
        with tempfile.TemporaryDirectory() as directory,patch.object(resources.time,'monotonic',side_effect=lambda:now[0]),\
            patch.object(resources,'storage_sample',side_effect=AssertionError('No physical sampling in parent')):
            root=Path(directory); budget=resources.RecoveryBudget()
            guard=resources.RecoveryStorage(object(),root,deadline=budget.preparation,outer=budget.outer)
            budget.preparation.storage_guard=guard
            def sample(sequence,bytes):
                guard.accept(dict(event='storage_started',sequence=sequence,started=now[0]))
                guard.accept(dict(event='storage_sample',sequence=sequence,started=now[0],completed=now[0],sample=dict(database_bytes=bytes,artifact_bytes=1,logical_raw_bytes=0)))
            now[0]=1.; sample(1,100)
            now[0]=5.; sample(2,1024**3)
            with patch.object(guard,'check'):
                download=budget.start_download(root)
            self.assertIs(download.storage_guard,guard)
            now[0]=10.
            with self.assertRaisesRegex(RuntimeError,'safety.database_growth'): sample(3,2*1024**3+101)
            rows=[json.loads(line) for line in (root/'recovery-storage.jsonl').read_text().splitlines()]
            self.assertEqual(rows[-1]['baseline']['database_bytes'],100)
            self.assertEqual(rows[-1]['sample']['database_bytes'],2*1024**3+101)

    def test_resource_sample_checks_broker_aggregate_and_completion(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from tests import waveform_resource_scenarios as resources
        self.assertTrue(hasattr(resources,'sample_resource_job'),'Aggregate child and broker sample absent')
        child=object(); broker=object(); monitor=object(); monitor_broker=object(); captured=[]
        stack=SimpleNamespace(broker=SimpleNamespace(process=broker)); evidence=SimpleNamespace(sample=captured.append)
        controller=dict(pid=99,monotonic=1.,working_set_bytes=3*1024**3,peak_working_set_bytes=4*1024**3,private_commit_bytes=5*1024**3)
        with patch.object(resources,'process_memory',side_effect=lambda p:dict(working_set_bytes=1200*1024**2,pid=1 if p is child else 2)),\
            patch.object(resources,'current_process_memory',return_value=controller,create=True),\
            patch.object(resources,'host_resources',return_value=dict(physical_available_bytes=4*1024**3,disk_available_bytes=11*1024**3)):
            with self.assertRaisesRegex(RuntimeError,'safety.owned_rss'):
                resources.sample_resource_job(stack,child,evidence,Path('.'),watchdog=SimpleNamespace(live_handles=lambda:(child,broker,monitor,monitor_broker)))
        self.assertEqual(len(captured[0]['processes']),4)
        self.assertEqual(captured[0].get('controller'),controller,'Controller attribution missing from violating child-aggregate row')
        with patch.object(resources,'process_memory',side_effect=lambda p:dict(working_set_bytes=17,pid=1 if p is child else 2)),\
            patch.object(resources,'current_process_memory',return_value=controller,create=True),\
            patch.object(resources,'host_resources',return_value=dict(physical_available_bytes=4*1024**3,disk_available_bytes=11*1024**3)):
            self.assertEqual(resources.sample_resource_job(stack,child,evidence,Path('.')),dict(working_set_bytes=17,pid=1))
        self.assertEqual(len(captured[-1]['processes']),2)  # Diagnostic controller is not aggregate-owned RSS.
        with patch.object(resources,'process_memory',return_value=dict(working_set_bytes=17,pid=1)),\
            patch.object(resources,'current_process_memory',return_value=controller,create=True),\
            patch.object(resources,'host_resources',return_value=dict(physical_available_bytes=1,disk_available_bytes=11*1024**3)):
            with self.assertRaisesRegex(RuntimeError,'safety.host_reserve'):
                resources.sample_resource_job(stack,child,evidence,Path('.'))
        self.assertEqual(captured[-1]['controller'],controller); self.assertEqual(captured[-1]['host']['physical_available_bytes'],1)

    def test_resource_completion_violation_still_closes_all_owned_processes(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from tests import waveform_resource_scenarios as resources
        for failure in ('rss','controller'):
          with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def process(pid):
                value=SimpleNamespace(pid=pid,returncode=None); value.poll=lambda:value.returncode; return value
            child,broker=process(1),process(2)
            class Stack:
                def __init__(self,*args,**kwargs):
                    self.root=root/'child'; self.root.mkdir(); (self.root/'resource-result.json').write_text('{}')
                    self.instances=[dict(instance_id='child',component='resource_measure',pid=1,process=child)]
                    self.broker=SimpleNamespace(process=broker,instances=[dict(instance_id='broker',component='broker',pid=2,process=broker)])
                def __enter__(self): return self
                def __exit__(self,*args): child.returncode=1; broker.returncode=1
                def launch(self,*args): return child
                def await_event(self,*args): pass
                def events(self,component,event=None): return [dict(event=event)] if event=='resource_complete' else []
                def stop(self,*args): child.returncode=1
                def kill_owned(self,*args,**kwargs): child.returncode=1
            with patch('tests.waveform_stack.Stack',Stack),\
                patch.object(resources,'current_process_memory',side_effect=OSError('controller query failed') if failure=='controller' else None,return_value=dict(pid=99,working_set_bytes=31),create=True),\
                patch.object(resources,'process_memory',side_effect=lambda p:dict(working_set_bytes=1200*1024**2,pid=p.pid)),\
                patch.object(resources,'host_resources',return_value=dict(physical_available_bytes=4*1024**3,disk_available_bytes=11*1024**3)):
                with self.assertRaisesRegex(OSError if failure=='controller' else RuntimeError,'controller query failed' if failure=='controller' else 'safety.owned_rss'):
                    resources.measured_job(object(),root,dict(kind='dataset'))
            envelope=json.loads(next(root.glob('measurement-*/result.json')).read_text())
            self.assertTrue(envelope['owned_cleanup']['complete']); self.assertEqual(envelope['reason'],'error.OSError' if failure=='controller' else 'safety.owned_rss')
            samples=[json.loads(line) for line in Path(envelope['samples_path']).read_text().splitlines()]
            self.assertEqual(len(samples),0 if failure=='controller' else 1)
            if samples: self.assertEqual(len(samples[0]['processes']),2)

    def test_storage_query_cannot_hide_a_late_stage_transition(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from tests import waveform_resource_scenarios as resources
        now=[0.]
        with tempfile.TemporaryDirectory() as directory,patch.object(resources.time,'monotonic',side_effect=lambda:now[0]):
            budget=resources.RecoveryBudget()
            def slow_storage(): now[0]=1801.
            budget.preparation.storage_guard=SimpleNamespace(check=slow_storage)
            with self.assertRaisesRegex(RuntimeError,'safety.preparation_deadline'): budget.start_download(Path(directory))
            self.assertIsNone(budget.download)
            now[0]=0.; budget=resources.RecoveryBudget()
            budget.preparation.storage_guard=SimpleNamespace(check=lambda:now.__setitem__(0,25.))
            budget.start_download(Path(directory))
            self.assertEqual(budget.prepared['monotonic'],25.)
            self.assertEqual(budget.download.began,25.)

    def test_download_budget_includes_open_and_final_advance_and_preserves_close_error(self):
        from unittest.mock import patch
        from tests import waveform_resource_scenarios as resources
        for phase in ('open','last_advance','close_failure'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as directory:
                now=[0.]; closed=[]
                class Iterator:
                    def __init__(self): self.sent=False
                    def __iter__(self): return self
                    def __next__(self):
                        if self.sent:
                            now[0]=121.
                            raise StopIteration
                        self.sent=True; return b'{}'
                    def close(self):
                        closed.append('iterator')
                        if phase=='close_failure': raise OSError('close failed')
                class Context:
                    def stream_manifest(self): return Iterator()
                    def close(self): closed.append('context')
                class Exporter:
                    def __init__(self,*args,**kwargs): pass
                    def open(self,*args,**kwargs):
                        if phase=='open': now[0]=121.
                        return Context()
                value=dict(export_root=str(Path(directory)/'files'),version_id='v',export_id='00000000-0000-0000-0000-000000000001',
                    actor=dict(source='local',id='test',display='Test',role='admin'),expected=[dict(name='manifest.json',sha256=sha256(b'{}').hexdigest(),size=2)])
                from dataclasses import asdict
                from tests.waveform_pairing_support import REVIEWER
                value['actor']=asdict(REVIEWER)
                with patch.object(resources.time,'monotonic',side_effect=lambda:now[0]),patch('backend.waveforms.export.DatasetExporter',Exporter):
                    with self.assertRaisesRegex(RuntimeError,'safety.dataset_download_deadline'):
                        resources.download_prepared(lambda:None,value,lambda *a,**k:None,deadline=1800.)
                self.assertIn('context',closed)
                if phase!='open': self.assertEqual((Path(value['export_root'])/'manifest.json').read_bytes(),b'{}')
                self.assertFalse((Path(value['export_root'])/'download-progress.jsonl').exists())
    def test_preparation_child_progress_and_late_result_cannot_reset_clock(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from tests import waveform_resource_scenarios as resources
        for late in (False,True):
            now=[0.]; calls=[]
            with self.subTest(late=late),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                class Stack:
                    def __init__(self,*args,**kwargs):
                        self.root=root/'child'; self.root.mkdir(); self.broker=None
                        self.process=SimpleNamespace(returncode=None,poll=lambda:self.process.returncode)
                        self.instances=[dict(instance_id='fake-owned',component='resource_measure',pid=1,process=self.process)]
                    def __enter__(self): return self
                    def __exit__(self,*args): self.process.returncode=1
                    def launch(self,*args): return self.process
                    def await_event(self,*args):
                        if late: now[0]=6.
                    def events(self,component,event=None):
                        if event=='resource_complete':
                            if late: now[0]=6.
                            return [dict(event=event)] if late else []
                        if event=='dataset_phase_started': return [dict(event=event)]*int(now[0]+1)
                        return []
                    def kill_owned(self,*args,**kwargs): calls.append('kill'); self.process.returncode=1
                    def stop(self,*args): calls.append('stop'); self.process.returncode=1
                def advance(seconds): now[0]+=1.
                with patch.object(resources.time,'monotonic',side_effect=lambda:now[0]),patch.object(resources.time,'sleep',side_effect=advance),\
                     patch('tests.waveform_stack.Stack',Stack),patch.object(resources,'process_memory',return_value={}),\
                     patch.object(resources,'host_resources',return_value={}),patch.object(resources,'safety_check'):
                    deadline=resources.PreparationDeadline(until=5.)
                    with self.assertRaisesRegex(RuntimeError,'safety.preparation_deadline'):
                        resources.measured_job(object(),root,dict(kind='dataset'),deadline=deadline)
                envelope=json.loads(next(root.glob('measurement-*/result.json')).read_text())
                self.assertEqual(envelope['reason'],'safety.preparation_deadline')
                self.assertEqual(envelope['preparation_clock']['deadline'],5.)
                self.assertLessEqual(envelope['preparation_clock']['remaining_seconds'],0)
                self.assertTrue(envelope['owned_cleanup']['complete']); self.assertEqual(calls,['kill','stop'])
                if not late: self.assertGreater(len(envelope['samples']),1)

    def test_owned_sampler_rejects_late_completion_without_waiting_for_next_sample(self):
        from unittest.mock import patch
        from tests import waveform_measurements as measurements
        with patch.object(measurements.time,'monotonic',return_value=0):
            sampler=measurements.OwnedSampler('.',lambda:[],lambda:None,seconds=1)
        with patch.object(measurements.time,'monotonic',return_value=1.01):
            with self.assertRaisesRegex(RuntimeError,'safety.operation_deadline'): sampler.check()

    def test_stack_cleanup_attempts_all_stages_and_preserves_original_failure(self):
        from types import SimpleNamespace
        from tests.waveform_stack import Stack
        for failing in (('worker',),('handle',),('broker',),('copy',),('worker','broker','copy')):
            with self.subTest(failing=failing):
                calls=[]; first=OSError(failing)
                def action(name):
                    def run(*args,**kwargs):
                        calls.append(name)
                        if name==failing[0]: raise first
                        if name in failing: raise RuntimeError(name)
                    return run
                stack=Stack.__new__(Stack); stack.failed=False; stack.evidence_root='supplied'
                stack.processes={'one':object(),'two':object()}; stack.stop=lambda name:action('worker' if name=='one' else 'worker2')()
                stack.handles=[SimpleNamespace(close=action('handle')),SimpleNamespace(close=action('handle2'))]
                stack.broker=SimpleNamespace(close=action('broker')); stack.temp=SimpleNamespace(cleanup=action('temp'))
                stack.preserve_evidence=action('copy')
                with self.assertRaises(OSError) as caught: stack.close()
                self.assertIs(caught.exception,first)
                self.assertEqual(calls,['worker','worker2','handle','handle2','broker','copy','temp'])
        # Context-body failure must not be replaced by cleanup/copy failure.
        original=ValueError('body'); secondary=OSError('cleanup')
        stack=Stack.__new__(Stack); stack.close=lambda:(_ for _ in ()).throw(secondary)
        stack.__exit__(ValueError,original,None)
        self.assertTrue(any('cleanup' in note for note in original.__notes__))
        from unittest.mock import patch
        stack.root=Path('.'); stack.limits=SimpleNamespace(gateways=('GW',))
        with patch('tests.waveform_stack.Broker',side_effect=original),self.assertRaises(ValueError) as caught:
            stack.__enter__()
        self.assertIs(caught.exception,original)

    def test_broker_peer_failure_still_closes_all_peers_and_stops(self):
        from types import SimpleNamespace
        from tests.waveform_stack import Broker
        calls=[]; first=OSError('first peer')
        def failed(): calls.append('first'); raise first
        def stopped(): calls.append('stop'); raise RuntimeError('stop failure')
        broker=Broker.__new__(Broker)
        broker.clients=[SimpleNamespace(close=failed),SimpleNamespace(close=lambda:calls.append('second'))]
        broker.stop=stopped
        with self.assertRaises(OSError) as caught: broker.close()
        self.assertIs(caught.exception,first)
        self.assertEqual(calls,['first','second','stop']); self.assertEqual(broker.clients,[])
        self.assertTrue(any('RuntimeError' in note for note in first.__notes__))

    def tool(self):
        self.assertIsNotNone(importlib.util.find_spec('tools.verify_waveforms'), 'Task17 verifier is required')
        from tools import verify_waveforms
        return verify_waveforms

    def test_gate_rejects_skip_partial_run_or_short_soak(self):
        gate=self.tool().qualify
        self.assertEqual(gate(complete_report()), ())
        for field,value,reason in [('skipped',1,'tests.skipped'),('testsRun',3,'tests.partial')]:
            report=complete_report(); report['tests'][field]=value
            self.assertIn(reason,gate(report))
        report=complete_report(); report['duration']['monotonic_elapsed_seconds']=1799
        self.assertIn('duration.too_short',gate(report))

    def test_gate_requires_both_ingress_paths_and_frozen_protection(self):
        gate=self.tool().qualify
        for section,key in [('ingress','online'),('ingress','offline'),('protection','unchanged')]:
            report=complete_report(); report[section][key]=False
            self.assertTrue(gate(report))

    def test_gate_rejects_malformed_missing_scenarios_and_test_identity_substitution(self):
        gate=self.tool().qualify
        for report in (None, {}, [], {'duration':None}): self.assertTrue(gate(report))
        for edit in (lambda r:r['scenarios'].pop(), lambda r:r['fault_cycles'].clear(),
                     lambda r:r['tests']['started_ids'].pop(),
                     lambda r:r['tests']['terminal_results'][0].update(outcome='skipped'),
                     lambda r:r['resources']['aborts'].append('ram_reserve'),
                     lambda r:r['cleanup']['residue'].append(123),
                     lambda r:r['duration'].update(monotonic_elapsed_seconds=float('nan')),
                     lambda r:r['tests'].update(testsRun=True),
                     lambda r:r['duration'].update(progress_seconds=[0,1800]),
                     lambda r:r['source'].update(unchanged=False)):
            report=complete_report(); edit(report); self.assertTrue(gate(report))

    def test_artifact_verifier_detects_tampering_and_path_escape(self):
        tool=self.tool()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'evidence').write_bytes(b'actual')
            item=dict(relative_path='evidence',size=6,sha256=sha256(b'actual').hexdigest())
            self.assertEqual(tool.verify_artifacts(root,[item]), ())
            (root/'evidence').write_bytes(b'edited')
            self.assertTrue(tool.verify_artifacts(root,[item]))
            self.assertTrue(tool.verify_artifacts(root,[item|{'relative_path':'../evidence'}]))

    def test_import_and_spawn_entry_have_no_database_or_discovery_side_effect(self):
        self.tool()
        source="""import unittest, psycopg, subprocess
def forbidden(*a,**k): raise AssertionError('import side effect')
unittest.TestLoader.discover=forbidden
psycopg.connect=forbidden
subprocess.Popen=forbidden
import tools.verify_waveforms, tools.waveform_restore, tests.waveform_measurements, tests.waveform_worker
import tests.waveform_experiments, tests.waveform_resource_scenarios
print('import-safe')
"""
        result=subprocess.run([sys.executable,'-c',source],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr); self.assertEqual(result.stdout.strip(),'import-safe')

    def test_restore_identity_refuses_source_wrong_cluster_or_role_mapping(self):
        self.assertIsNotNone(importlib.util.find_spec('tools.waveform_restore'))
        from tools.waveform_restore import target_identity, validate_target_identity
        roles={name:'owned_'+name for name in ('migration_owner','receiver','assembler','receipt_publisher','reader','operator')}
        parent=dict(fixture_id='b'*32,database='oilwell_waveforms_test',host='127.0.0.1',port=3822,data='C:/owned/data',roles=roles)
        identity=target_identity('a'*32,parent,'owned_registry')
        validate_target_identity(identity,'a'*32,parent,'owned_registry')
        for changes in ({'database':'oilwell_waveforms_test'},{'port':5432},{'owner':'other'},
                        {'registry_owner':roles['migration_owner']},{'run_id':'c'*32}):
            with self.assertRaises(ValueError): validate_target_identity(identity|changes,'a'*32,parent,'owned_registry')

    def test_real_result_collector_keeps_skip_and_discovery_multiplicity(self):
        tool=self.tool()
        class Examples(unittest.TestCase):
            def test_ok(self): pass
            @unittest.skip('intentional collector fixture')
            def test_skip(self): pass
        suite=unittest.defaultTestLoader.loadTestsFromTestCase(Examples)
        import io
        report=tool.run_suite(suite,io.StringIO(),modules=['example'],configured=True)
        self.assertEqual(report['testsRun'],2); self.assertEqual(report['skipped'],1)
        self.assertEqual(len(report['discovered_ids']),2); self.assertEqual(len(report['terminal_results']),2)
        self.assertNotEqual(report['exit_code'],0)

    def test_gate_does_not_crash_on_malformed_nested_scenarios(self):
        gate=self.tool().qualify
        for edit in (lambda r:r.update(scenarios=[None]),lambda r:r['tests'].update(terminal_results=[None]),
                     lambda r:r['scenarios'][0].update(id=[])):
            report=complete_report(); edit(report); self.assertTrue(gate(report))

    def test_bounded_vector_has_exact_frames_and_independent_sample_values(self):
        from tests import waveform_measurements as measurements
        self.assertTrue(hasattr(measurements,'build_capture'))
        from backend.waveforms.files import scan_file
        from backend.waveforms.wire import parse_frame
        from io import BytesIO
        import struct
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sample.owpwave'
            details=measurements.build_capture(path,frames=64,channels=32,chunk_frames=8)
            with path.open('rb') as source: scan=scan_file(source)
            self.assertEqual(details['decoded_bytes'],10240); self.assertEqual(len(scan.lines),9)
            chunk=parse_frame(path.read_bytes().splitlines()[1])
            self.assertEqual(len(chunk.data),1280)
            self.assertEqual(struct.unpack('<iB',chunk.data[:5]),(0,0))
            self.assertEqual(struct.unpack('<iB',chunk.data[5:10]),(13,0))
            self.assertEqual(scan.manifest.streams[0].frame_count,64)
            metadata=json.loads(path.with_name(path.name+'.input.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata,details)

    def test_owned_sampler_retains_violating_sample_and_query_failure_is_not_zero(self):
        from unittest.mock import patch
        from tests import waveform_measurements as measurements
        observed=[]; aborts=[]; sample={'pid':12,'working_set_bytes':17}; host={'physical_available_bytes':1,'disk_available_bytes':11*1024**3}
        controller=dict(pid=99,monotonic=1.,working_set_bytes=31,peak_working_set_bytes=41,private_commit_bytes=51)
        monitor=measurements.OwnedSampler('.',lambda:[object()],lambda:aborts.append(True),sink=observed.append)
        with patch.object(measurements,'process_memory',return_value=sample),patch.object(measurements,'host_resources',return_value=host),\
             patch.object(measurements,'current_process_memory',return_value=controller,create=True):
            monitor._run()
        self.assertEqual(observed,[dict(monotonic=observed[0]['monotonic'],processes=[sample],host=host,controller=controller)])
        self.assertEqual(monitor.errors,['safety.host_reserve']); self.assertEqual(aborts,[True])
        missing=measurements.OwnedSampler('.',lambda:[object()],lambda:None,sink=observed.append)
        with patch.object(measurements,'process_memory',side_effect=OSError('query failed')): missing._run()
        self.assertEqual(missing.samples,[]); self.assertEqual(missing.errors,['OSError']); self.assertEqual(len(observed),1)

    def test_controller_query_failure_aborts_sampler_without_zero_row_or_cleanup_override(self):
        from unittest.mock import patch
        from tests import waveform_measurements as measurements
        first=OSError('controller query failed'); observed=[]; closed=[]
        def abort():
            closed.append(True)
            raise RuntimeError('cleanup also failed')
        sampler=measurements.OwnedSampler('.',lambda:[],abort,sink=observed.append)
        with patch.object(measurements,'current_process_memory',side_effect=first,create=True),\
            patch.object(measurements,'host_resources',return_value=dict(physical_available_bytes=1,disk_available_bytes=1)):
            with self.assertRaises(OSError) as caught:
                with sampler: sampler.thread.join(1)
        self.assertIs(caught.exception,first); self.assertEqual(observed,[]); self.assertEqual(sampler.samples,[])
        self.assertEqual(closed,[True]); self.assertFalse(sampler.thread.is_alive())
        self.assertEqual(first.__notes__,['Owned sampler abort: RuntimeError'])

    def test_memory_queries_use_retained_and_current_handles_and_refuse_native_failure(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from tests import waveform_measurements as measurements
        self.assertTrue(hasattr(measurements,'current_process_memory'),'Current-process HANDLE query missing')
        handles=[]; fail=[False]
        def query(handle,pointer,size):
            handles.append(handle.value)
            if fail[0]: return 0
            value=pointer._obj
            value.WorkingSetSize=17000; value.PeakWorkingSetSize=19000; value.PrivateUsage=23000
            return 1
        def current(): return -1
        def dll(name,**kwargs):
            if name=='psapi': return SimpleNamespace(GetProcessMemoryInfo=query)
            self.assertEqual(name,'kernel32'); return SimpleNamespace(GetCurrentProcess=current)
        child=SimpleNamespace(pid=12,_handle=123,poll=lambda:None)
        with patch('ctypes.WinDLL',side_effect=dll):
            parent=measurements.current_process_memory(); owned=measurements.process_memory(child)
            self.assertEqual(parent['pid'],os.getpid()); self.assertEqual(owned['pid'],12)
            for result in (parent,owned):
                self.assertEqual((result['working_set_bytes'],result['peak_working_set_bytes'],result['private_commit_bytes']),(17000,19000,23000))
            import ctypes
            self.assertEqual(handles,[ctypes.c_void_p(-1).value,123])
            fail[0]=True
            for action in (measurements.current_process_memory,lambda:measurements.process_memory(child)):
                with self.assertRaises(OSError): action()
        child.poll=lambda:0
        with self.assertRaises(ValueError): measurements.process_memory(child)

    def test_current_process_memory_native_self_query_has_sane_byte_counters(self):
        from tests import waveform_measurements as measurements
        sample=measurements.current_process_memory()
        self.assertEqual(sample['pid'],os.getpid()); self.assertGreater(sample['monotonic'],0)
        self.assertGreater(sample['working_set_bytes'],0); self.assertGreater(sample['private_commit_bytes'],0)
        self.assertGreaterEqual(sample['peak_working_set_bytes'],sample['working_set_bytes'])

    def test_native_query_structure_and_pointer_types_are_stable_with_exact_layout(self):
        import ctypes
        from ctypes import wintypes
        from types import SimpleNamespace
        from unittest.mock import patch
        from tests import waveform_measurements as measurements
        structures={'Counters':[],'Memory':[]}; pointers={'Counters':[],'Memory':[]}
        def current(): return -1
        def process_query(handle,pointer,size):
            value=pointer._obj; structures['Counters'].append(type(value)); pointers['Counters'].append(process_query.argtypes[1])
            self.assertEqual(size,ctypes.sizeof(value)); self.assertEqual(value.cb,size)
            value.WorkingSetSize=17000; value.PeakWorkingSetSize=19000; value.PrivateUsage=23000
            return 1
        def host_query(pointer):
            value=pointer._obj; structures['Memory'].append(type(value)); pointers['Memory'].append(host_query.argtypes[0])
            self.assertEqual(value.length,ctypes.sizeof(value))
            value.total_physical=16*1024**3; value.available_physical=4*1024**3
            return 1
        def dll(name,**kwargs):
            if name=='psapi': return SimpleNamespace(GetProcessMemoryInfo=process_query)
            self.assertEqual(name,'kernel32')
            return SimpleNamespace(GetCurrentProcess=current,GlobalMemoryStatusEx=host_query)
        child=SimpleNamespace(pid=12,_handle=123,poll=lambda:None)
        with patch('ctypes.WinDLL',side_effect=dll),patch('shutil.disk_usage',return_value=SimpleNamespace(free=11*1024**3)):
            for _ in range(3):
                parent=measurements.current_process_memory(); owned=measurements.process_memory(child); host=measurements.host_resources(Path('.'))
                self.assertEqual(parent['pid'],os.getpid()); self.assertEqual(owned['pid'],12)
                self.assertEqual((parent['working_set_bytes'],owned['peak_working_set_bytes'],owned['private_commit_bytes']),(17000,19000,23000))
                self.assertEqual((host['physical_total_bytes'],host['physical_available_bytes'],host['disk_available_bytes']),(16*1024**3,4*1024**3,11*1024**3))
        expected={
            'Counters':[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in
                ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage','PrivateUsage')],
            'Memory':[('length',wintypes.DWORD),('load',wintypes.DWORD)]+[(name,ctypes.c_ulonglong) for name in
                ('total_physical','available_physical','total_pagefile','available_pagefile','total_virtual','available_virtual','extended_virtual')]}
        for name,count in (('Counters',6),('Memory',3)):
            with self.subTest(name=name):
                self.assertEqual(len(structures[name]),count)  # Still one real query per invocation.
                for structure in structures[name]: self.assertEqual(structure._fields_,expected[name])
                self.assertTrue(all(value is structures[name][0] for value in structures[name]),'Fresh Structure type per query: '+name)
                self.assertTrue(all(value is pointers[name][0] for value in pointers[name]),'Fresh POINTER type per query: '+name)
                self.assertIs(pointers[name][0]._type_,structures[name][0])

    def test_measurement_import_does_not_load_or_query_windows_dlls(self):
        from unittest.mock import patch
        from tests import waveform_measurements as measurements
        spec=importlib.util.spec_from_file_location('isolated_measurements_import',measurements.__file__)
        module=importlib.util.module_from_spec(spec)
        with patch('ctypes.WinDLL',side_effect=AssertionError('No native calls on import')):
            spec.loader.exec_module(module)
        self.assertTrue(callable(module.current_process_memory)); self.assertTrue(callable(module.host_resources))

    def test_os_sampler_reads_owned_child_bytes_and_refuses_finished_process(self):
        from tests import waveform_measurements as measurements
        self.assertTrue(hasattr(measurements,'process_memory'))
        process=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-c','import sys; data=bytearray(4*1024*1024); print("ready",flush=True); sys.stdin.readline()'],
            env=dict(os.environ,__PYVENV_LAUNCHER__=sys.executable),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            self.assertEqual(process.stdout.readline().strip(),b'ready')
            sample=measurements.process_memory(process)
            self.assertGreater(sample['working_set_bytes'],4*1024*1024)
            self.assertGreaterEqual(sample['peak_working_set_bytes'],sample['working_set_bytes'])
        finally:
            process.communicate(b'stop\n',timeout=5)
        with self.assertRaises(ValueError): measurements.process_memory(process)

    def test_restore_commands_never_create_clean_or_drop_owner_acl(self):
        from tools import waveform_restore as restore
        self.assertTrue(hasattr(restore,'restore_arguments'))
        args=restore.restore_arguments(Path('C:/Program Files/PostgreSQL/18/bin/pg_restore.exe'),
            'oilwell_waveforms_restore_'+'a'*32,'owned_admin',Path('C:/owned/source.dump'))
        self.assertIn('--single-transaction',args); self.assertIn('--exit-on-error',args)
        self.assertNotIn('--create',args); self.assertNotIn('--clean',args)
        self.assertNotIn('--no-owner',args); self.assertNotIn('--no-acl',args)
        with self.assertRaises(ValueError): restore.restore_arguments(Path('pg_restore'), 'oilwell_waveforms_test','u',Path('dump'))

    def test_target_creation_refuses_existing_database_before_any_ddl(self):
        from tools import waveform_restore as restore
        self.assertTrue(hasattr(restore,'create_target'))
        class Existing:
            def execute(self,statement,params=()):
                if str(statement).startswith('SELECT datname FROM pg_database'): return self
                raise AssertionError('DDL on existing target')
            def fetchone(self): return ('occupied',)
        with self.assertRaises(ValueError): restore.create_target(Existing(),{'database':'oilwell_waveforms_restore_'+'a'*32},{})

    def test_fault_schedule_and_progress_refuse_idle_padding_or_failed_cycles(self):
        self.assertIsNotNone(importlib.util.find_spec('tests.waveform_experiments'))
        from tests.waveform_experiments import FAULTS, Progress
        self.assertEqual(len(FAULTS),12)
        self.assertEqual(len(set(FAULTS)),12)
        ticks=iter((10.,10.5,71.,1811.))
        progress=Progress(clock=lambda:next(ticks))
        progress.record(messages=1,bytes_=20)
        progress.record(messages=1,bytes_=20)
        with self.assertRaisesRegex(RuntimeError,'cadence'): progress.record(messages=1,bytes_=20)

    def test_gate_rejects_unhashable_terminal_identity_and_bad_artifact_refs(self):
        gate=self.tool().qualify
        for edit in (lambda r:r['tests']['terminal_results'][0].update(id=[]),
                     lambda r:r['scenarios'][0].update(artifact_refs=['absent.json']),
                     lambda r:r['artifacts'][0].update(sha256='bad')):
            report=complete_report(); edit(report); self.assertTrue(gate(report))

    def test_target_mutation_and_wrong_source_path_refuse_before_connect_or_secret_lookup(self):
        from tools import waveform_restore as restore
        from unittest.mock import patch
        roles={name:'owned_'+name for name in restore.ROLES}
        parent=dict(fixture_id='b'*32,database='oilwell_waveforms_test',host='127.0.0.1',port=3822,data='C:/owned/data',roles=roles)
        authority=dict(parent=parent,sidecar=dict(role='owned_registry'),settings={})
        identity=restore.target_identity('a'*32,parent,'owned_registry')
        target=restore.RecoveryTarget(authority,identity,Path('C:/owned'))
        identity['database']='oilwell_waveforms_test'
        with patch('psycopg.connect',side_effect=AssertionError('Forbidden connection')):
            with self.assertRaises(ValueError): target.connect()
        with patch('backend.secret_store.read_windows_credential',side_effect=AssertionError('Forbidden secret lookup')):
            with self.assertRaises(ValueError): restore.load_source_authority('C:/unowned.toml',resolve=True)

    def test_safety_refuses_host_rss_and_storage_ceiling_breaches(self):
        from tests.waveform_measurements import safety_check
        from tests.waveform_resource_scenarios import storage_check
        gib=1024**3; host=dict(physical_available_bytes=4*gib,disk_available_bytes=11*gib)
        safety_check(host,preflight=True)
        for changed in (host|dict(physical_available_bytes=2*gib),host|dict(disk_available_bytes=9*gib)):
            with self.assertRaises(RuntimeError): safety_check(changed,preflight=True)
        with self.assertRaises(RuntimeError): safety_check(host,[dict(working_set_bytes=2*gib)])
        with self.assertRaises(RuntimeError): storage_check(dict(database_bytes=0),dict(database_bytes=3*gib,artifact_bytes=0))
        with self.assertRaises(RuntimeError): storage_check(dict(database_bytes=0),dict(database_bytes=0,artifact_bytes=9*gib))

    def test_sender_storage_sampling_allows_only_absent_optional_journal(self):
        from tests import waveform_resource_scenarios as resources
        from unittest.mock import patch
        from types import SimpleNamespace
        self.assertTrue(hasattr(resources,'sender_storage_sample'))
        spool=Path('owned-sender.db')
        for error in (FileNotFoundError(),PermissionError('denied'),OSError('I/O failure')):
            with patch.object(Path,'stat',side_effect=[SimpleNamespace(st_size=123),error]):
                if isinstance(error,FileNotFoundError):
                    self.assertEqual(resources.sender_storage_sample(spool),dict(database_bytes=123,journal_bytes=0))
                else:
                    with self.assertRaises(type(error)): resources.sender_storage_sample(spool)
        with patch.object(Path,'stat',side_effect=FileNotFoundError()):
            with self.assertRaises(FileNotFoundError): resources.sender_storage_sample(spool)

    def test_sender_operation_deadline_is_distinct_from_three_capture_aggregate(self):
        from tests import waveform_resource_scenarios as resources
        self.assertTrue(hasattr(resources,'SenderDeadline'))
        events=[]
        for index,started in enumerate((0.,100.,200.)):
            events.extend([dict(event='sender_phase_started',phase='run_once',operation_id=index,monotonic=started),
                dict(event='sender_phase_complete',phase='run_once',operation_id=index,monotonic=started+90.,elapsed_seconds=90.)])
        deadline=resources.SenderDeadline(3,began=0.)
        deadline.check(291.,events)  # >120 total, every actual operation <120.
        with self.assertRaisesRegex(RuntimeError,'sender_aggregate_deadline'): deadline.check(360.001,events)
        stalled=resources.SenderDeadline(3,began=0.)
        with self.assertRaisesRegex(RuntimeError,'sender_operation_deadline'):
            stalled.check(120.001,[dict(event='sender_phase_started',phase='run_once',operation_id=0,monotonic=0.)])
        with self.assertRaisesRegex(RuntimeError,'sender_operation_deadline'):
            resources.SenderDeadline(3,began=0.).check(121.,[
                dict(event='sender_phase_started',phase='prepare',operation_id=0,monotonic=0.),
                dict(event='sender_phase_complete',phase='prepare',operation_id=0,monotonic=121.,elapsed_seconds=121.)])
        with self.assertRaisesRegex(RuntimeError,'sender_aggregate_deadline'):
            resources.SenderDeadline(1,began=0.).check(120.001,[])

    def test_continuation_rejects_tamper_incomplete_prefix_and_unapproved_source(self):
        tool=self.tool()
        self.assertTrue(hasattr(tool,'validate_continuation'))
        from unittest.mock import patch
        from tools import waveform_restore
        with tempfile.TemporaryDirectory() as directory:
            owned=Path(directory); root=owned/'logs/waveforms-wp04'/('20261001T000000Z-'+'a'*32); root.mkdir(parents=True)
            path,report=prefix_fixture(root); original=path.read_bytes()
            current=dict(report['source']['files_sha256']); current['tests/waveform_measurements.py']='c'*64
            approved=tool.inventory_sha256(current); expected=sha256(original).hexdigest()
            with patch.object(waveform_restore,'ROOT',owned),patch('psycopg.connect',side_effect=AssertionError('No DB')):
                value=tool.validate_continuation(path,expected,approved,current)
                self.assertEqual(len(value['prefix']['scenarios']),6)
                self.assertEqual(value['prefix']['duration']['monotonic_elapsed_seconds'],1800.)
                for old_hash,new_hash,inventory in [('0'*64,approved,current),(expected,'0'*64,current),
                    (expected,tool.inventory_sha256(current|{'backend/unchanged.py':'d'*64}),current|{'backend/unchanged.py':'d'*64})]:
                    with self.assertRaises(ValueError): tool.validate_continuation(path,old_hash,new_hash,inventory)
                for modify in (lambda r:r['artifacts'].pop(),lambda r:r['artifacts'][0].update(relative_path='../escape'),
                    lambda r:r['artifacts'].append(r['artifacts'][0]),lambda r:r['fault_cycles'][0].update(recovered=False),
                    lambda r:r['duration'].update(messages=999),lambda r:r['source'].update(unchanged=False)):
                    changed=deepcopy(report); modify(changed); path.write_text(json.dumps(changed),encoding='utf-8')
                    with self.assertRaises(ValueError): tool.validate_continuation(path,tool.digest_file(path),approved,current)
                path.write_bytes(original)
                for name,modify in [('ingress-GW.json',lambda v:v.update(receipts=0)),
                    ('ingress-A.json',lambda v:v.update(file_source_ref=v['mqtt_source_ref'])),
                    ('transport.json',lambda v:v.update(orphan_recovered=False)),
                    ('fault-0.json',lambda v:v.update(recovered=False))]:
                    saved=(root/name).read_bytes(); value=json.loads(saved); modify(value)
                    data=json.dumps(value).encode(); (root/name).write_bytes(data)
                    changed=deepcopy(report)
                    next(a for a in changed['artifacts'] if a['relative_path']==name).update(size=len(data),sha256=sha256(data).hexdigest())
                    path.write_text(json.dumps(changed),encoding='utf-8')
                    with self.assertRaises(ValueError): tool.validate_continuation(path,tool.digest_file(path),approved,current)
                    (root/name).write_bytes(saved); path.write_bytes(original)
                raw=(root/'ingress-GW.json').read_bytes(); (root/'ingress-GW.json').write_bytes(b'{}')
                with self.assertRaises(ValueError): tool.validate_continuation(path,expected,approved,current)
                (root/'ingress-GW.json').write_bytes(raw); (root/'fault-11.json').unlink()
                with self.assertRaises(ValueError): tool.validate_continuation(path,expected,approved,current)

    def test_continuation_inventory_digest_has_canonical_utf8_order(self):
        tool=self.tool()
        self.assertTrue(hasattr(tool,'inventory_sha256'))
        expected=sha256(('{"a.py":"'+'b'*64+'","录波.py":"'+'a'*64+'"}').encode('utf-8')).hexdigest()
        self.assertEqual(tool.inventory_sha256({'录波.py':'a'*64,'a.py':'b'*64}),expected)
        self.assertEqual(tool.inventory_sha256({'a.py':'b'*64,'录波.py':'a'*64}),expected)

    def r78_exception(self):
        return dict(id='R78-exact-calibration-bounds',scope='fresh_suffix_and_regression',files={
            'backend/waveforms/windows.py':dict(before='7f74877a8c9ec21daf9ef9c2df6816b582af388629704a7b4b3a5a63dd2ad3c8',after='590ac7ead178ffa6c3dd2467125ca6a963a9050a5dbd028050aa3438ff82af97'),
            'tests/test_waveform_windows.py':dict(before='b4d037b33c011e4317ee90b72234799293e64ff24c109c61a639d9bbd0d1343a',after='a88d29cce12e861b8dbd3755c6729648cb47a205a24ec818e1603c6a0a3d2112')})

    def test_continuation_r78_exact_pairs_refuse_drift_and_are_annotated_by_cli(self):
        tool=self.tool(); exception=self.r78_exception(); pairs=exception['files']
        from unittest.mock import patch
        from tools import waveform_restore
        with tempfile.TemporaryDirectory() as directory:
            owned=Path(directory); root=owned/'logs/waveforms-wp04'/('20261001T000000Z-'+'a'*32); root.mkdir(parents=True)
            path,report=prefix_fixture(root)
            old=report['source']['files_sha256']; old.update({p:v['before'] for p,v in pairs.items()})
            current=old|{p:v['after'] for p,v in pairs.items()}; path.write_text(json.dumps(report),encoding='utf-8')
            original=path.read_bytes(); expected=tool.digest_file(path); approved=tool.inventory_sha256(current)
            with patch.object(waveform_restore,'ROOT',owned),patch('psycopg.connect',side_effect=AssertionError('No DB')),\
                 patch('subprocess.Popen',side_effect=AssertionError('No spawn')):
                binding=tool.validate_continuation(path,expected,approved,current)
                self.assertEqual(binding['product_exception'],exception)
                self.assertFalse(set(pairs)&tool.CONTINUATION_PATHS)
                for changed in (current|{'backend/waveforms/windows.py':'f'*64},
                    current|{'tests/test_waveform_windows.py':old['tests/test_waveform_windows.py']},
                    current|{'backend/unchanged.py':'f'*64}):
                    with self.assertRaises(ValueError): tool.validate_continuation(path,expected,tool.inventory_sha256(changed),changed)
                with self.assertRaises(ValueError): tool.validate_continuation(path,expected,'0'*64,current)
                changed=deepcopy(report); changed['source']['files_sha256']['backend/waveforms/windows.py']='e'*64
                path.write_text(json.dumps(changed),encoding='utf-8')
                with self.assertRaises(ValueError): tool.validate_continuation(path,tool.digest_file(path),approved,current)
                path.write_bytes(original)
                target=owned/'logs/waveforms-wp04'/('20261001T000001Z-'+'b'*32)
                with patch.object(tool,'source_inventory',return_value=current),patch.object(waveform_restore,'load_source_authority'),\
                     patch('tools.verify_ingest_reliability.protected_snapshot',return_value={}),\
                     patch('tests.waveform_experiments.run_experiments',side_effect=RuntimeError('fresh suffix boundary')):
                    self.assertEqual(tool.main(['--config','unused','--evidence-dir',str(target),'--continue-from',str(path),
                        '--continue-sha256',expected,'--continue-source-sha256',approved]),1)
                failed=json.loads((target/'report.json').read_text(encoding='utf-8'))
                self.assertEqual(failed['status'],'partial')
                self.assertEqual(failed['segments'][0]['product_exception'],exception)
                self.assertEqual(path.read_bytes(),original)

    def test_two_segment_r78_gate_requires_exact_pairs_and_explicit_annotation(self):
        tool=self.tool(); report=complete_report(); exception=self.r78_exception(); pairs=exception['files']
        old={'example.py':'b'*64}|{p:v['before'] for p,v in pairs.items()}
        current=old|{p:v['after'] for p,v in pairs.items()}; approved=tool.inventory_sha256(current)
        report.update(evidence_mode='two_segment',source=dict(unchanged=True,files_sha256=current,scope='suffix_and_regression'),segments=[
            dict(kind='reused_protocol_soak',run_id='d'*32,validated=True,report_sha256='e'*64,report_ref='prior-run1/report.json',
                files_sha256=old,source_sha256=tool.inventory_sha256(old),source_delta=deepcopy(pairs),product_exception=exception),
            dict(kind='fresh_suffix_and_regression',run_id=report['run_id'],source_sha256=approved,approved_source_sha256=approved)])
        report['artifacts'].append(dict(relative_path='prior-run1/report.json',size=1,sha256='e'*64))
        report['tests'].update(run_id=report['run_id'],source_sha256=approved)
        bind_fake_lease(report)
        self.assertEqual(tool.qualify(report),())
        for modify in (lambda r:r['segments'][0].pop('product_exception'),
            lambda r:r['segments'][0]['product_exception'].update(scope='whole_run'),
            lambda r:r['segments'][0]['product_exception'].update(id='arbitrary'),
            lambda r:r['segments'][0]['product_exception']['files']['backend/waveforms/windows.py'].update(after='f'*64)):
            changed=deepcopy(report); modify(changed); self.assertIn('segments.invalid',tool.qualify(changed))
        for change_old,path,value in [(False,'backend/waveforms/windows.py','f'*64),
            (True,'backend/waveforms/windows.py','f'*64),(False,'tests/test_waveform_windows.py',pairs['tests/test_waveform_windows.py']['before']),
            (False,'example.py','f'*64)]:
            changed=deepcopy(report); prior=changed['segments'][0]; inventory=changed['source']['files_sha256']
            (prior['files_sha256'] if change_old else inventory)[path]=value
            prior['source_sha256']=tool.inventory_sha256(prior['files_sha256'])
            prior['source_delta']={p:dict(before=prior['files_sha256'][p],after=inventory[p]) for p in inventory if prior['files_sha256'][p]!=inventory[p]}
            digest=tool.inventory_sha256(inventory); changed['segments'][1].update(source_sha256=digest,approved_source_sha256=digest)
            changed['tests']['source_sha256']=digest
            self.assertIn('segments.invalid',tool.qualify(changed))

    def test_two_segment_gate_binds_delta_prior_report_and_new_source_regression(self):
        tool=self.tool(); report=complete_report(); prior_files={'example.py':'b'*64}
        report['source']['scope']='suffix_and_regression'
        report['evidence_mode']='two_segment'; report['segments']=[
            dict(kind='reused_protocol_soak',run_id='d'*32,validated=True,report_sha256='e'*64,
                report_ref='prior-run1/report.json',files_sha256=prior_files,source_sha256=tool.inventory_sha256(prior_files),source_delta={}),
            dict(kind='fresh_suffix_and_regression',run_id=report['run_id'],source_sha256=tool.inventory_sha256(prior_files),approved_source_sha256=tool.inventory_sha256(prior_files))]
        report['artifacts'].append(dict(relative_path='prior-run1/report.json',size=1,sha256='e'*64))
        report['tests'].update(run_id=report['run_id'],source_sha256=tool.inventory_sha256(prior_files))
        self.assertEqual(tool.qualify(report),())
        for modify,reason in [(lambda r:r['segments'][0].update(source_delta={'tests/waveform_measurements.py':dict(before='a'*64,after='b'*64)}),'segments.invalid'),
            (lambda r:r['segments'][0].update(report_sha256='f'*64),'segments.invalid'),
            (lambda r:r['tests'].update(run_id='d'*32),'tests.segment_identity'),
            (lambda r:r['tests'].update(source_sha256='f'*64),'tests.segment_identity'),
            (lambda r:r.update(source={}), 'segments.invalid'),
            (lambda r:r.update(scenarios=r['scenarios'][:6]),'scenarios.incomplete')]:
            changed=deepcopy(report); modify(changed); self.assertIn(reason,tool.qualify(changed))
        for segments in ([{'files_sha256':['a']},{}],[[],{}],[{},None]):
            malformed=dict(evidence_mode='two_segment',source=dict(unchanged=True,files_sha256={'a':'0'*64}),segments=segments)
            self.assertIn('segments.invalid',tool.qualify(malformed))

    def test_continuation_cli_refuses_before_authority_connection_copy_or_spawn(self):
        tool=self.tool()
        from unittest.mock import patch
        from tools import waveform_restore
        with tempfile.TemporaryDirectory() as directory:
            owned=Path(directory); root=owned/'logs/waveforms-wp04'/('20261001T000000Z-'+'a'*32); root.mkdir(parents=True)
            path,report=prefix_fixture(root); current=report['source']['files_sha256']
            with patch.object(waveform_restore,'ROOT',owned),patch.object(tool,'source_inventory',return_value=current),\
                patch.object(waveform_restore,'load_source_authority',side_effect=AssertionError('Authority must be later')),\
                patch.object(waveform_restore,'create_run_root',side_effect=AssertionError('No output side effect')),\
                patch('subprocess.Popen',side_effect=AssertionError('No spawn')):
                with self.assertRaises(ValueError): tool.main(['--config','unused','--evidence-dir',str(owned/'new'),
                    '--continue-from',str(path),'--continue-sha256','0'*64,'--continue-source-sha256',tool.inventory_sha256(current)])

    def test_continuation_copies_bound_files_without_relabeling_original(self):
        tool=self.tool()
        from unittest.mock import patch
        from tools import waveform_restore
        with tempfile.TemporaryDirectory() as directory:
            owned=Path(directory); root=owned/'logs/waveforms-wp04'/('20261001T000000Z-'+'a'*32); root.mkdir(parents=True)
            path,report=prefix_fixture(root); original=path.read_bytes(); current=report['source']['files_sha256']
            with patch.object(waveform_restore,'ROOT',owned):
                binding=tool.validate_continuation(path,sha256(original).hexdigest(),tool.inventory_sha256(current),current)
            target=owned/'new'; target.mkdir()
            value=tool.copy_continuation(binding,target)
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual((target/'prior-run1/report.json').read_bytes(),original)
            self.assertEqual(json.loads(original)['status'],'partial')
            self.assertEqual(tool.verify_artifacts(target/'prior-run1',report['artifacts']),())
            self.assertEqual(len(value['scenarios']),6)
            with self.assertRaises(FileExistsError): tool.copy_continuation(binding,target)

    def test_continuation_suffix_does_not_rerun_soak_and_retains_failure(self):
        from unittest.mock import patch
        from tests.waveform_experiments import run_experiments
        from contextlib import nullcontext
        class Fixture:
            manifest={'database':'oilwell_waveforms_test'}
            connections=[]
            def setUp(self): pass
            def doCleanups(self): pass
            def capacity_fixture(self,*args): return nullcontext()
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ):
            root=Path(directory)
            with patch('tools.waveform_restore.load_source_authority'),patch('tests.waveform_dataset_support.DatasetCase',Fixture),\
                patch('tests.waveform_measurements.host_resources',return_value=dict(physical_available_bytes=4*1024**3,disk_available_bytes=11*1024**3)),\
                patch('tests.waveform_measurements.protocol_roundtrip',side_effect=AssertionError('No prefix repetition')),\
                patch('tests.waveform_experiments.fault_cycle',side_effect=AssertionError('No soak repetition')),\
                patch('tests.waveform_resource_scenarios.maximum_scenarios',side_effect=RuntimeError('fresh suffix boundary')):
                with self.assertRaisesRegex(RuntimeError,'fresh suffix boundary'):
                    run_experiments(root,'b'*32,Path('pinned'),1800,prefix=dict(scenarios=[],duration={'reused':True}),source_sha256='1'*64)
            partial=json.loads((root/'experiment-partial.json').read_text(encoding='utf-8'))
            self.assertEqual(partial['identity']['run_id'],'b'*32)
            self.assertEqual(partial['duration'],{'reused':True})
            self.assertNotIn('tests',partial); self.assertNotIn('restore',partial)

    def three_segment_fixture(self):
        tool=self.tool(); report=complete_report()
        old={'backend/unchanged.py':'b'*64,'tests/waveform_experiments.py':'1'*64}
        middle=old|{'tests/waveform_experiments.py':'2'*64}
        current=middle|{'tests/waveform_experiments.py':'3'*64}
        report.update(evidence_mode='three_segment',source=dict(unchanged=True,scope='recovery_and_regression',files_sha256=current),segments=[
            dict(kind='reused_protocol_soak',run_id='d'*32,validated=True,report_sha256='e'*64,
                report_ref='prior-run1/report.json',files_sha256=old,source_sha256=tool.inventory_sha256(old),
                source_delta={'tests/waveform_experiments.py':dict(before='1'*64,after='2'*64)}),
            dict(kind='reused_maximum_xy_audit',run_id='f'*32,validated=True,report_sha256='4'*64,
                report_ref='prior-run3/report.json',files_sha256=middle,source_sha256=tool.inventory_sha256(middle),
                ancestor_run_id='d'*32,ancestor_report_sha256='e'*64,ancestor_report_ref='prior-run3/prior-run1/report.json',
                source_delta={'tests/waveform_experiments.py':dict(before='2'*64,after='3'*64)}),
            dict(kind='fresh_recovery_and_regression',run_id='a'*32,source_sha256=tool.inventory_sha256(current),approved_source_sha256=tool.inventory_sha256(current))])
        original={'online_then_offline','offline_then_online','transport_judgments','receipt_chain','gateway_isolation','lease_recovery'}
        refs={'online_then_offline':['ingress-GW.json'],'offline_then_online':['ingress-A.json'],
            'receipt_chain':['ingress-GW.json','ingress-A.json'],'transport_judgments':['transport.json'],
            'gateway_isolation':['fault-8.json'],'lease_recovery':['fault-2.json','fault-9.json','fault-10.json'],
            'xy':['xy.json'],'audit_volume':['audit.json'],'dataset_1000':['dataset-1.json','dataset-100.json','dataset-1000.json']}
        for case in report['scenarios']:
            segment=0 if case['id'] in original else 2 if case['id']=='dataset_1000' else 1
            case['segment_run_id']=report['segments'][segment]['run_id']
            case['artifact_refs']=[('prior-run1/' if segment==0 else 'prior-run3/' if segment==1 else '')+ref for ref in refs.get(case['id'],['maximum.json'])]
        report['artifacts'] += [dict(relative_path=p,size=1,sha256='c'*64) for p in sorted({p for c in report['scenarios'] for p in c['artifact_refs']})]
        report['artifacts'] += [dict(relative_path=p,size=1,sha256=h) for p,h in (
            ('prior-run1/events.jsonl','c'*64),('prior-run3/events.jsonl','c'*64),('prior-run1/report.json','e'*64),
            ('prior-run3/report.json','4'*64),('prior-run3/prior-run1/report.json','e'*64))]
        report['tests'].update(run_id='a'*32,source_sha256=tool.inventory_sha256(current))
        bind_fake_lease(report)
        return report

    def test_three_segment_gate_requires_ancestry_exact_scope_and_new_regression(self):
        tool=self.tool(); report=self.three_segment_fixture()
        self.assertEqual(tool.qualify(report),())
        for edit,reason in (
            (lambda r:r['segments'][1].update(ancestor_report_sha256='0'*64),'segments.invalid'),
            (lambda r:r['segments'][0].update(source_delta={}),'segments.invalid'),
            (lambda r:r['segments'][1].update(source_delta={}),'segments.invalid'),
            (lambda r:r['segments'][1].update(files_sha256=[]),'segments.invalid'),
            (lambda r:r['segments'].__setitem__(1,[]),'segments.invalid'),
            (lambda r:r['source'].update(scope='whole_run'),'segments.invalid'),
            (lambda r:r['source']['files_sha256'].update({'backend/unchanged.py':'0'*64}),'segments.invalid'),
            (lambda r:r['scenarios'][0].update(segment_run_id='a'*32),'segments.invalid'),
            (lambda r:r['scenarios'][0].update(artifact_refs=['events.jsonl']),'segments.invalid'),
            (lambda r:r['scenarios'][0].update(artifact_refs=['prior-run1/events.jsonl']),'segments.invalid'),
            (lambda r:r['tests'].update(run_id='f'*32),'tests.segment_identity'),
            (lambda r:r['tests'].update(source_sha256='0'*64),'tests.segment_identity'),
            (lambda r:r['restore'].update(exports=False),'restore.incomplete')):
            changed=deepcopy(report); edit(changed)
            self.assertIn(reason,tool.qualify(changed))

    def test_resources_cli_incomplete_bindings_refuse_before_side_effects(self):
        from unittest.mock import patch
        tool=self.tool()
        with patch.object(tool,'source_inventory',side_effect=AssertionError('No source process')),\
             patch('tools.waveform_restore.load_source_authority',side_effect=AssertionError('No authority')):
            base=['--config','unused','--evidence-dir','unused']
            for args in (['--continue-resources-from','report.json'],
                ['--continue-resources-from','report.json','--continue-resources-sha256','0'*64],
                ['--continue-from','old.json','--continue-sha256','0'*64,'--continue-source-sha256','1'*64,
                 '--continue-resources-from','report.json','--continue-resources-sha256','2'*64]):
                with self.assertRaises(ValueError): tool.main(base+args)

    def test_resources_resume_skips_earlier_phases_but_requires_restore_and_retains_dataset(self):
        self.check_resources_resume(incomplete=False)

    def test_incomplete_downloads_cannot_create_backup_or_recovery_baseline(self):
        self.check_resources_resume(incomplete=True)

    def check_resources_resume(self,*,incomplete):
        from unittest.mock import patch
        from contextlib import nullcontext
        from tests.waveform_experiments import run_experiments
        class Fixture:
            manifest={'database':'oilwell_waveforms_test'}; connections=[]
            def setUp(self): pass
            def doCleanups(self): pass
            def capacity_fixture(self,*args): return nullcontext()
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ):
            root=Path(directory); old=[dict(id='sender_storage',status='passed',artifact_refs=['prior-run3/maximum.json'])]
            def prepare(fixture,path,**identity):
                for n in (1,100,1000): (path/f'dataset-{n}.json').write_text('{}')
                value=complete_report()['recovery_downloads']; value.update(identity)
                for p in value['prepared']: p.update(identity)
                if incomplete: value['downloads'].pop()
                return {'volumes':(1,100,1000),'recovery_downloads':value}
            with patch('tools.waveform_restore.load_source_authority'),patch('tests.waveform_dataset_support.DatasetCase',Fixture),\
                patch('tests.waveform_measurements.host_resources',return_value=dict(physical_available_bytes=4*1024**3,disk_available_bytes=11*1024**3)),\
                patch('tests.waveform_measurements.protocol_roundtrip',side_effect=AssertionError('No protocol repeat')),\
                patch('tests.waveform_experiments.fault_cycle',side_effect=AssertionError('No soak repeat')),\
                patch('tests.waveform_resource_scenarios.maximum_scenarios',side_effect=AssertionError('No maximum repeat')),\
                patch('tests.waveform_resource_scenarios.create_pair',side_effect=AssertionError('No XY repeat')),\
                patch('tests.waveform_resource_scenarios.audit_scenarios',side_effect=AssertionError('No audit repeat')),\
                patch('tests.waveform_resource_scenarios.storage_sample',return_value=dict(database_bytes=1,artifact_bytes=1)),\
                patch('tests.waveform_experiments.prepare_recovery',side_effect=prepare),\
                patch('tests.waveform_resource_scenarios.measure_leases',return_value={'fresh':True}) as lease,\
                patch('tools.waveform_restore.backup_restore',side_effect=RuntimeError('restore required')) as backup:
                with self.assertRaisesRegex(ValueError if incomplete else RuntimeError,'Incomplete' if incomplete else 'restore required'):
                    run_experiments(root,'b'*32,Path('pinned'),1800,prefix=dict(scenarios=[],duration={'reused':True}),resources_prefix=dict(scenarios=old),source_sha256='1'*64)
                lease.assert_called_once_with(unittest.mock.ANY,root,run_id='b'*32,source_sha256='1'*64)
                if incomplete: backup.assert_not_called()
            partial=json.loads((root/'experiment-partial.json').read_text())
            if incomplete:
                self.assertTrue(partial['cleanup']['complete']); self.assertNotIn('restore',partial)
                self.assertEqual([s['id'] for s in partial['scenarios']],['sender_storage'])
                self.assertFalse((root/'recovery-source.json').exists())
                return
            self.assertEqual([s['id'] for s in partial['scenarios']],['sender_storage','dataset_1000'])
            self.assertEqual(partial['scenarios'][-1]['segment_run_id'],'b'*32)
            self.assertTrue(partial['cleanup']['complete']); self.assertNotIn('restore',partial)
            self.assertEqual(partial['lease_measurement'],{'fresh':True})
            self.assertNotIn('tests',partial); self.assertTrue((root/'recovery-source.json').is_file())

    def test_resources_semantics_bind_envelopes_samples_counts_and_owned_cleanup(self):
        from tests import waveform_experiments as experiments
        self.assertTrue(hasattr(experiments,'validate_completed_resources'),'Middle semantic validator required')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/('run-'+'f'*32); root.mkdir()
            report,maximum,xy,audit,save=resource_fixture(root)
            value=experiments.validate_completed_resources(root,report)
            self.assertEqual({s['id'] for s in value['scenarios']},{'sender_storage','sender_maximum','raw_maximum','summary_maximum','output_owners','xy','audit_volume'})
            def envelopes(value):
                if isinstance(value,dict):
                    if 'envelope_path' in value: save(Path(value['envelope_path']).relative_to(root).as_posix(),value)
                    for child in value.values(): envelopes(child)
                elif isinstance(value,list):
                    for child in value: envelopes(child)
            for section,edit in (
                ('maximum',lambda v:v['sender_three'].update(status='partial')),
                ('maximum',lambda v:v['sender_three']['bounds'].update(aggregate_seconds=120)),
                ('maximum',lambda v:v['sender_one'].update(durability={'journal_mode':'off','synchronous':0})),
                ('maximum',lambda v:v['raw'].update(points=639999)),
                ('maximum',lambda v:v['summary'].update(samples=[])),
                ('maximum',lambda v:v['output_owners']['cleanup'].update(output_owners=1)),
                ('maximum',lambda v:v['inputs'][0].update(channels=31)),
                ('xy',lambda v:v['summary'].update(points=2001)),
                ('audit',lambda v:v['levels'][1].update(actual_rows=999)),
                ('audit',lambda v:v['levels'][2].update(stale_page_rejected=False))):
                original={'maximum':maximum,'xy':xy,'audit':audit}[section]; changed=deepcopy(original); edit(changed)
                save(section+'.json',changed); envelopes(changed)
                with self.assertRaises(ValueError): experiments.validate_completed_resources(root,report)
                save(section+'.json',original); envelopes(original)
            source=Path(maximum['inputs'][0]['path']); data=json.loads(source.read_text()); data['streams'][0]['frame_bytes']=159
            original_source=source.read_bytes(); source.write_text(json.dumps(data)+'\n')
            with self.assertRaises(ValueError): experiments.validate_completed_resources(root,report)
            source.write_bytes(original_source)
            sample=Path(maximum['sender_one']['samples_path']); original=sample.read_bytes(); sample.write_text('[]\n')
            with self.assertRaises(ValueError): experiments.validate_completed_resources(root,report)
            sample.write_bytes(original)
            report['artifacts'][:]=[a for a in report['artifacts'] if a['relative_path']!='audit.json']
            with self.assertRaises(ValueError): experiments.validate_completed_resources(root,report)

    def r82_exception(self):
        return dict(id='R82-exact-portable-recovery',scope='recovery_and_regression',
            historical_schema_versions=[4,4],current_schema_version=5,files={
            'backend/waveforms/migrate.py':dict(before='52aac20f908b004a20d1f31c73d05f47f5019be90e35696d728c6e038a7ca5e5',after='91a101f025f82cf90ab762105083340a5f5176609eb18ef4795171798047f538'),
            'backend/waveforms/sql/005_portable_structure.sql':dict(before=None,after='4e8cdb6f00099ecbb76eb2fa37765654f3317d3b073ff61f5e01d1ed3d524bda'),
            'tools/waveform_restore.py':dict(before='1fe9b99f68034e7f2251920bb4e280958d82d6bb523118cab38c4d6e081a4ec8',after='9bac5a861ef5687e10c34376d4fc0497d184219f47252a5bed16b2a3e0a5b1f3'),
            'tests/test_waveform_schema.py':dict(before='6688ae605b2c934f31a91323c6baa897a9f59438d667f48ee825bbff923fab0d',after='d5dcff6967e6abda918374f297fe59535073ccd1ee225447a852d2d10110043e'),
            'tests/test_waveform_dataset_upgrade.py':dict(before='2e814e4198c7fe01d433b076f5821bf9f0c3ac65c507152c428fc662d496f048',after='f08163dce5cfc6d400af5bb12794248a4563f7516b3d1a03603be50f3ca34825')})

    def test_r82_three_segment_bridge_requires_exact_hashes_annotation_and_schema_scope(self):
        tool=self.tool(); report=self.three_segment_fixture(); exception=self.r82_exception()
        prior,middle,suffix=report['segments']; current=report['source']['files_sha256']
        for p,v in exception['files'].items():
            if v['before'] is not None:
                prior['files_sha256'][p]=v['before']; middle['files_sha256'][p]=v['before']
            current[p]=v['after']
        for segment in (prior,middle):
            segment.update(schema_version=4,source_sha256=tool.inventory_sha256(segment['files_sha256']))
        middle['source_delta'].update(exception['files']); middle['recovery_exception']=exception
        digest=tool.inventory_sha256(current); suffix.update(schema_version=5,source_sha256=digest,approved_source_sha256=digest)
        report['tests']['source_sha256']=digest
        bind_fake_lease(report)
        self.assertEqual(tool.qualify(report),())
        for mutate in (lambda r:r['segments'][1].pop('recovery_exception'),
            lambda r:r['segments'][1]['recovery_exception'].update(scope='whole_run'),
            lambda r:r['segments'][1]['recovery_exception'].update(historical_schema_versions=[5,5]),
            lambda r:r['segments'][1]['recovery_exception']['files']['backend/waveforms/migrate.py'].update(after='f'*64),
            lambda r:r['segments'][0].update(schema_version=5),lambda r:r['segments'][2].pop('schema_version'),
            lambda r:r['segments'][1].update(ancestor_report_sha256='0'*64),
            lambda r:r['segments'][2].update(approved_source_sha256='0'*64)):
            bad=deepcopy(report); mutate(bad); self.assertIn('segments.invalid',tool.qualify(bad))
        for path in exception['files']:
            for value in (None,'f'*64):
                bad=deepcopy(report); inventory=bad['source']['files_sha256']
                if value is None: inventory.pop(path)
                else: inventory[path]=value
                digest=tool.inventory_sha256(inventory)
                bad['segments'][2].update(source_sha256=digest,approved_source_sha256=digest)
                bad['segments'][1]['source_delta']={p:dict(before=middle['files_sha256'].get(p),after=inventory.get(p))
                    for p in set(middle['files_sha256'])|set(inventory) if middle['files_sha256'].get(p)!=inventory.get(p)}
                bad['tests']['source_sha256']=digest
                self.assertIn('segments.invalid',tool.qualify(bad))
        bad=deepcopy(report); bad.update(evidence_mode='two_segment',segments=[prior,suffix])
        self.assertIn('segments.invalid',tool.qualify(bad))

    def test_r82_original_preflight_keeps_r78_exact_and_refuses_drift_before_any_effect(self):
        from unittest.mock import patch
        from tools import waveform_restore
        tool=self.tool(); r78=self.r78_exception(); r82=self.r82_exception()
        with tempfile.TemporaryDirectory() as directory:
            owned=Path(directory); root=owned/'logs/waveforms-wp04'/('20261001T000000Z-'+'a'*32); root.mkdir(parents=True)
            path,report=prefix_fixture(root); pairs=r78['files']|r82['files']
            old=report['source']['files_sha256']; old.update({p:v['before'] for p,v in pairs.items() if v['before'] is not None})
            current=old|{p:v['after'] for p,v in pairs.items()}
            path.write_text(json.dumps(report),encoding='utf-8'); original=path.read_bytes(); expected=tool.digest_file(path)
            approved=tool.inventory_sha256(current)
            with patch.object(waveform_restore,'ROOT',owned),patch('psycopg.connect',side_effect=AssertionError('No DB')),\
                 patch('subprocess.Popen',side_effect=AssertionError('No spawn')):
                result=tool.validate_continuation(path,expected,approved,current,recovery=True)
                self.assertEqual(result['product_exception'],r78); self.assertEqual(result['recovery_exception'],r82)
                with self.assertRaises(ValueError): tool.validate_continuation(path,expected,'0'*64,current,recovery=True)
                for p in pairs:
                    for value in (None,'f'*64):
                        bad=dict(current)
                        if value is None: bad.pop(p)
                        else: bad[p]=value
                        with self.subTest(path=p,value=value),self.assertRaises(ValueError):
                            tool.validate_continuation(path,expected,tool.inventory_sha256(bad),bad,recovery=True)
                    if pairs[p]['before'] is not None:
                        bad=deepcopy(report); bad['source']['files_sha256'][p]='f'*64
                        path.write_text(json.dumps(bad),encoding='utf-8')
                        with self.assertRaises(ValueError): tool.validate_continuation(path,tool.digest_file(path),approved,current,recovery=True)
                        path.write_bytes(original)
                for bad in (current|{'backend/unapproved.py':'f'*64},current|{'backend/unchanged.py':'f'*64}):
                    with self.assertRaises(ValueError): tool.validate_continuation(path,expected,tool.inventory_sha256(bad),bad,recovery=True)
                base=['--config','unused','--evidence-dir',str(owned/'new'),'--continue-from',str(path),
                    '--continue-sha256',expected,'--continue-source-sha256',approved]
                with patch.object(tool,'source_inventory',return_value=current),\
                     patch.object(waveform_restore,'load_source_authority',side_effect=AssertionError('No authority')),\
                     patch.object(waveform_restore,'create_run_root',side_effect=AssertionError('No output root')):
                    for extra in ([],['--include-existing-regression'],['--continue-resources-from','unused'],
                        ['--continue-resources-from','unused','--continue-resources-sha256','0'*64],
                        ['--continue-resources-from','unused','--continue-resources-sha256','0'*64,'--include-existing-regression','--phase','regression']):
                        with self.assertRaises(ValueError): tool.main(base+extra)
                self.assertFalse((owned/'new').exists()); self.assertEqual(path.read_bytes(),original)

    def test_resources_preflight_authenticates_ancestry_artifacts_and_current_source_before_copy(self):
        self.check_resources_preflight(recovery=False)

    def test_r82_bridge_preflight_and_cli_emit_historical_v4_current_v5_without_two_segment_permission(self):
        self.check_resources_preflight(recovery=True)

    def test_reviewed_chain_preflights_and_report_share_exact_annotation_before_effects(self):
        self.check_resources_preflight(recovery=True,reviewed=True)

    def check_resources_preflight(self, *, recovery,reviewed=False):
        from contextlib import ExitStack
        from unittest.mock import patch
        from tools import waveform_restore
        from tests.waveform_experiments import validate_completed_prefix
        tool=self.tool(); self.assertTrue(hasattr(tool,'validate_resources_continuation'),'Read-only middle preflight required')
        with tempfile.TemporaryDirectory() as directory,ExitStack() as approvals:
            owned=Path(directory); base=owned/'logs/waveforms-wp04'
            oldroot=base/('20261001T000000Z-'+'d'*32); oldroot.mkdir(parents=True)
            oldpath,old=prefix_fixture(oldroot); old['source']['files_sha256']['tests/waveform_experiments.py']='1'*64
            if recovery:
                old['source']['files_sha256'].update({p:v['before'] for p,v in self.r82_exception()['files'].items() if v['before'] is not None})
            if reviewed:
                old['source']['files_sha256'].update({p:'8'*64 for p in ('tools/verify_waveforms.py','tests/test_waveform_verification_tool.py','docs/年度工作任务逐项响应.md')})
            oldpath.write_text(json.dumps(old),encoding='utf-8'); oldsha=tool.digest_file(oldpath)
            root=base/('20261001T000001Z-'+'f'*32); root.mkdir()
            report,maximum,xy,audit,save=resource_fixture(root)
            save('run-identity.json',dict(version=1,run_id='f'*32,root=str(root)))
            for item in old['artifacts']:
                save('prior-run1/'+item['relative_path'],(oldroot/item['relative_path']).read_bytes(),binary=True)
            save('prior-run1/report.json',oldpath.read_bytes(),binary=True)
            measured=old['source']['files_sha256']|{'tests/waveform_experiments.py':'2'*64}
            current=measured|{'tests/waveform_experiments.py':'3'*64}
            if recovery: current.update({p:v['after'] for p,v in self.r82_exception()['files'].items()})
            if reviewed:
                migrated=dict(current); docs=current|{'docs/年度工作任务逐项响应.md':'9'*64}
                current=docs|{'tools/verify_waveforms.py':'a'*64,'tests/test_waveform_verification_tool.py':'b'*64}
                delta=lambda a,b:tuple((p,a.get(p),b.get(p)) for p in sorted(set(a)|set(b)) if a.get(p)!=b.get(p))
                chain=(('recovery',tool.inventory_sha256(measured),tool.inventory_sha256(migrated),delta(measured,migrated)),
                       ('docs',tool.inventory_sha256(migrated),tool.inventory_sha256(docs),delta(migrated,docs)))
                for name,value in dict(REVIEWED_SOURCE_CHAIN=chain,RUN3_SOURCE_SHA256=tool.inventory_sha256(measured),
                    FINAL_GATE_BEFORE={p:'8'*64 for p in ('tools/verify_waveforms.py','tests/test_waveform_verification_tool.py')},
                    RUN1_ID='d'*32,RUN3_ID='f'*32,RUN1_REPORT_SHA256=oldsha).items(): approvals.enter_context(patch.object(tool,name,value))
            approved=tool.inventory_sha256(current)
            report.update(schema='waveform.verification/1',evidence_kind='actual',status='partial',evidence_mode='two_segment',
                source=dict(unchanged=True,scope='suffix_and_regression',files_sha256=measured),
                identity=dict(verified=True,run_id='f'*32,source_database='oilwell_waveforms_test'),
                cleanup=dict(complete=True,residue=[]),protection=dict(unchanged=True,before={},after={}),
                segments=[dict(kind='reused_protocol_soak',run_id='d'*32,validated=True,report_sha256=oldsha,report_ref='prior-run1/report.json',
                    files_sha256=old['source']['files_sha256'],source_sha256=tool.inventory_sha256(old['source']['files_sha256']),
                    source_delta={'tests/waveform_experiments.py':dict(before='1'*64,after='2'*64)}),
                    dict(kind='fresh_suffix_and_regression',run_id='f'*32,source_sha256=tool.inventory_sha256(measured),approved_source_sha256=tool.inventory_sha256(measured))],
                error_type='WaveformError',error_frames=[dict(file=str(tool.ROOT/file),line=line,function=func) for file,line,func in (
                    ('tools/verify_waveforms.py',339,'main'),('tests/waveform_experiments.py',368,'run_experiments'),
                    ('tests/waveform_experiments.py',245,'prepare_recovery'),('backend/waveforms/export.py',111,'open'),('backend/waveforms/dataset_contracts.py',23,'fail'))])
            report.update(validate_completed_prefix(oldroot,old))
            save('phases.jsonl',[dict(phase=p,monotonic=i,utc='fixture') for i,p in enumerate(
                ('validated-prior-protocol-soak','maximum-resources','xy-audit','populated-recovery'))],True)
            path=root/'report.json'; path.write_text(json.dumps(report),encoding='utf-8'); original=path.read_bytes()
            if reviewed: approvals.enter_context(patch.object(tool,'RUN3_REPORT_SHA256',tool.digest_file(path)))
            with patch.object(waveform_restore,'ROOT',owned),patch('psycopg.connect',side_effect=AssertionError('No DB')),\
                 patch('subprocess.Popen',side_effect=AssertionError('No spawn')):
                if recovery:
                    with self.assertRaises(ValueError): tool.validate_continuation(oldpath,oldsha,approved,current)
                first=tool.validate_continuation(oldpath,oldsha,approved,current,**({'recovery':True} if recovery else {}))
                binding=tool.validate_resources_continuation(path,tool.digest_file(path),first,current)
                self.assertEqual(len(binding['prefix']['scenarios']),7)
                if reviewed:
                    transition=tool.reviewed_source_transition(current,approved,measured=measured)['source_transition']
                    self.assertEqual(first['source_transition'],transition)
                    self.assertEqual(binding['segment']['source_transition'],transition)
                    final=self.three_segment_fixture(); final['source']['files_sha256']=current
                    final['segments']=[binding['prior_segment'],binding['segment'],dict(kind='fresh_recovery_and_regression',
                        run_id=final['run_id'],source_sha256=approved,approved_source_sha256=approved,schema_version=5,source_transition=transition)]
                    for item in final['artifacts']:
                        if item['relative_path'] in ('prior-run1/report.json','prior-run3/prior-run1/report.json'): item['sha256']=oldsha
                        elif item['relative_path']=='prior-run3/report.json': item['sha256']=tool.digest_file(path)
                    for case in final['scenarios']:
                        if case['id'] in tool.RESOURCE_SCENARIOS: case['segment_run_id']='f'*32
                    final['tests']['source_sha256']=approved; bind_fake_lease(final)
                    self.assertEqual(tool.qualify(final),())
                    for edit in (lambda r:r['segments'][1].pop('source_transition'),lambda r:r['segments'][2].pop('source_transition'),
                                 lambda r:r['segments'][1]['source_transition']['checkpoints'].reverse(),
                                 lambda r:r['segments'][0].update(run_id='e'*32),lambda r:r['segments'][1].update(report_sha256='0'*64)):
                        bad=deepcopy(final); edit(bad); self.assertIn('segments.invalid',tool.qualify(bad))
                for mutate in (lambda r:r['segments'][0].update(report_sha256='0'*64),lambda r:r.update(error_type='RuntimeError'),
                    lambda r:r['error_frames'][-1].update(function='backup_restore'),lambda r:r.update(restore={'schema':True}),
                    lambda r:r['cleanup'].update(complete=False),lambda r:r['protection'].update(after={'changed':True}),
                    lambda r:r.update(segments=[None,{}]),lambda r:r['artifacts'].append(dict(relative_path='../escape',size=1,sha256='0'*64))):
                    bad=deepcopy(report); mutate(bad); path.write_text(json.dumps(bad),encoding='utf-8')
                    with self.assertRaises(ValueError): tool.validate_resources_continuation(path,tool.digest_file(path),first,current)
                path.write_bytes(original)
                for drift in ({'backend/unchanged.py':'0'*64},{'tests/waveform_measurements.py':'0'*64}):
                    changed=current|drift
                    with self.assertRaises(ValueError): tool.validate_resources_continuation(path,tool.digest_file(path),first,changed)
                with self.assertRaises(ValueError): tool.validate_resources_continuation(path,'0'*64,first,current)
                artifact=root/'audit.json'; content=artifact.read_bytes(); artifact.write_bytes(b'corrupt')
                with self.assertRaises(ValueError): tool.validate_resources_continuation(path,tool.digest_file(path),first,current)
                artifact.write_bytes(content)
                command=['--config','unused','--evidence-dir',str(base/('20261001T000002Z-'+'a'*32)),
                    '--continue-from',str(oldpath),'--continue-sha256',oldsha,'--continue-source-sha256',approved,
                    '--continue-resources-from',str(path),'--continue-resources-sha256',tool.digest_file(path),'--include-existing-regression']
                with patch.object(tool,'source_inventory',return_value=current),\
                     patch.object(waveform_restore,'load_source_authority',side_effect=AssertionError('No authority for invalid middle')):
                    with self.assertRaises(ValueError): tool.main(command[:-2]+['0'*64,'--include-existing-regression'])
                with patch.object(tool,'source_inventory',return_value=current),patch.object(waveform_restore,'load_source_authority'),\
                     patch('tools.verify_ingest_reliability.protected_snapshot',return_value={}),\
                     patch('tests.waveform_experiments.run_experiments',side_effect=RuntimeError('fresh recovery boundary')),\
                     patch('sys.stdout',new_callable=__import__('io').StringIO):
                    self.assertEqual(tool.main(command),1)
                new=json.loads((Path(command[3])/'report.json').read_text(encoding='utf-8'))
                self.assertEqual(new['evidence_mode'],'three_segment')
                self.assertEqual(new['segments'][1]['ancestor_report_sha256'],oldsha)
                self.assertEqual(new['source']['scope'],'recovery_and_regression')
                self.assertEqual(new['status'],'partial'); self.assertNotIn('tests',new)
                if recovery:
                    self.assertEqual([s['schema_version'] for s in new['segments']],[4,4,5])
                    self.assertEqual(new['segments'][1]['recovery_exception'],self.r82_exception())
                if reviewed: self.assertEqual(new['segments'][2]['source_transition'],transition)
                target=owned/'copies'; target.mkdir()
                tool.copy_resources_continuation(binding,target)
                self.assertEqual((target/'prior-run3/report.json').read_bytes(),original)
                self.assertEqual((target/'prior-run3/prior-run1/report.json').read_bytes(),oldpath.read_bytes())
                self.assertEqual(path.read_bytes(),original)
                with self.assertRaises(FileExistsError): tool.copy_resources_continuation(binding,target)
