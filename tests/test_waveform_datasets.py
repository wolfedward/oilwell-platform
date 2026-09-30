"""Dataset contracts, immutable publication and independent split checks."""
import importlib
import unittest
from hashlib import sha256
from uuid import UUID
from tests.waveform_dataset_support import DatasetCase
from tests.waveform_registry_support import WaveformRegistryCase


def feature(case, name):
    try: return importlib.import_module('backend.waveforms.' + name)
    except ModuleNotFoundError: case.fail('Task12 ' + name + ' implementation absent')


def physical_member(id, split, low, high, *, device='D', boot='b', digest=None, well='w'):
    return dict(kind='pair',reference_id=str(UUID(int=id)),quality_run_id=str(UUID(int=id+100)),split=split,
        snapshot=dict(quality=[],payload_fingerprints=[] if digest is None else [dict(content_sha256=digest)],
            physical_cycles=[dict(physical_cycle_key=str(id),events=[dict(index=str(n)) for n in (low,(low+high)//2,high)],
                source_intervals=[dict(source=dict(gateway_id='GW',device_id=device,boot_id=boot),first_index=str(low),last_index=str(high))])],
            registry=dict(freeze=dict(status='resolved',well=dict(id=well),utc_start='1970-01-01T00:00:00.000000Z',lower_ns=str(low),upper_ns=str(high),uncertainty_ns='0'))))


class DatasetContractTests(unittest.TestCase):
    def test_nonzero_utc_anchor_added_once_to_corrected_support(self):
        from fractions import Fraction
        from backend.waveforms.dataset_splits import absolute_support
        snapshot=dict(utc_start='1970-01-01T00:00:01.000000Z',lower_ns='-1/2',upper_ns='1999/2',uncertainty_ns='1/2')
        self.assertEqual(absolute_support(snapshot),(Fraction(1999999999,2),Fraction(2000001999,2)))
        self.assertIsNone(absolute_support(snapshot|dict(uncertainty_ns=None)))

    def test_restriction_paging_overflow_and_exact_guarded_pins(self):
        from backend.waveforms.dataset_context import DatasetContext
        from backend.waveforms.pair_contracts import capture_key
        from backend.waveforms.errors import WaveformError
        from unittest.mock import patch
        from backend.waveforms import dataset_context
        key=dict(gateway_id='GW',device_id='D',capture_id=str(UUID(int=1)))
        certificate=dict(member=dict(reference_id=str(UUID(int=2))),refs=[],selection=dict(captures=[dict(key=key)]))
        captures={capture_key(key):dict(capture_pk=UUID(int=3))}
        class Pages:
            def __init__(self,count): self.count=count; self.calls=0
            def execute(self,query,params):
                self.offset=params[-1]; self.calls+=1
                self.assertion='LIMIT 100 OFFSET' in query
            def fetchall(self):
                return [(UUID(int=n+10),UUID(int=3),'capture','capture_conflict',UUID(int=n+10)) for n in range(self.offset,min(self.count,self.offset+100))]
        context=object.__new__(DatasetContext); page=Pages(2000)
        with self.assertRaises(WaveformError) as caught: context.restriction_snapshot(page,certificate,captures,('capture_conflict',))
        self.assertEqual(caught.exception.code,'waveform.dataset_snapshot_limit'); self.assertGreater(page.calls,1); self.assertLess(page.calls,20); self.assertTrue(page.assertion)
        pins=context.restriction_snapshot(Pages(2),certificate,captures,('capture_conflict',))
        with patch.object(dataset_context.json,'dumps',side_effect=AssertionError('Serialization under guard')):
            self.assertEqual(context.restriction_snapshot(Pages(2),certificate,captures,('capture_conflict',),expected=pins),pins)
            with self.assertRaises(WaveformError) as caught: context.restriction_snapshot(Pages(3),certificate,captures,('capture_conflict',),expected=pins)
        self.assertEqual(caught.exception.code,'waveform.dataset_stale_preparation')

    def test_fixed_member_grammar_bounds_and_stream_payload_groups(self):
        from backend.waveforms.dataset_contracts import validate_body,reason_codes
        from backend.waveforms.dataset_splits import check_splits
        from backend.waveforms.errors import ContractError,WaveformError
        good=dict(kind='pair',reference_id=str(UUID(int=1)),quality_run_id=str(UUID(int=2)),split='unassigned')
        for members in ([],[good]*101,[good,good],[good|dict(extra=True)],[good|dict(reference_id='not-a-uuid')]):
            with self.assertRaises(ContractError): validate_body('update',dict(target_id=str(UUID(int=3)),changes=dict(upsert_members=members)))
        with self.assertRaises(ContractError): validate_body('create',dict(title='a'*65536,purpose='raw_archive',split_plan=dict(kind='none')))
        self.assertEqual(len(reason_codes(['same']*1000)),1)
        with self.assertRaises(WaveformError): reason_codes([str(n) for n in range(65)])
        a=physical_member(1,'train',0,10,device='A',well='one'); b=physical_member(2,'test',100,110,device='B',well='two')
        a['snapshot']['payload_fingerprints']=[dict(content_sha256='a',stream_data_sha256='whole-stream')]
        b['snapshot']['payload_fingerprints']=[dict(content_sha256='b',stream_data_sha256='whole-stream')]
        with self.assertRaises(WaveformError): check_splits([a,b],dict(kind='well_holdout'))

    def test_nonempty_frozen_literal_oracle(self):
        import json
        from backend.waveforms.dataset_contracts import stage_manifest
        literal=b'{"creator":{"domain":"local","user_id":"u"},"dataset_id":"00000000-0000-0000-0000-000000000001","draft_revision":"3","files":[{"capture_pk":"00000000-0000-0000-0000-000000000004","content_length":2,"file_plan_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","file_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","filename":"capture-0001.owpwave","key":{"capture_id":"00000000-0000-0000-0000-000000000005","device_id":"D","gateway_id":"GW"},"manifest_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","source_files":[]}],"members":[{"kind":"capture","quality_run_id":"00000000-0000-0000-0000-000000000003","reference_id":"00000000-0000-0000-0000-000000000004","snapshot":{"evidence":[],"pair":null,"payload_fingerprints":[],"physical_cycles":[],"quality":[{"evidence_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","id":"00000000-0000-0000-0000-000000000003","manifest_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","parameters_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","profile_id":null,"profile_sha256":null,"purpose":"raw_archive","registry_sha256":null,"result_sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","selection_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_key":{"capture_id":"00000000-0000-0000-0000-000000000005","device_id":"D","gateway_id":"GW"},"status":"pending","validator_version":"waveform.quality/1"}],"registry":{"freeze":null,"historical":null},"restrictions":[]},"split":"unassigned"}],"protocol":"waveform.dataset-manifest/1","purpose":"raw_archive","reviewer":{"domain":"local","user_id":"r"},"snapshot_versions":{"pair":"waveform.pair-content/1","quality":"waveform.quality/1","split":"waveform.dataset-split/1"},"source_kinds":["synthetic"],"split_plan":{"kind":"none"},"title":"A","version_id":"00000000-0000-0000-0000-000000000002"}'
        with stage_manifest(json.loads(literal)) as staged:
            self.assertEqual(b''.join(staged.chunks()),literal)
            self.assertEqual(staged.length,1993)
            self.assertEqual(staged.content_sha256,'fe2fe4d98c9b6e510c1a8521064360b9373baa9e7b395c60e9527b5d7390f1e4')

    def test_staging_chunk_bound_and_cleanup_on_failure(self):
        from unittest.mock import patch
        from tempfile import TemporaryFile
        from backend.waveforms import dataset_contracts as contracts
        from backend.waveforms.errors import WaveformError
        handles=[]
        def tracked(*args,**kwargs):
            handle=TemporaryFile(*args,**kwargs); handles.append(handle); return handle
        with patch.object(contracts,'TemporaryFile',tracked):
            with contracts.stage_manifest(dict(text='a'*131072)) as staged:
                chunks=list(staged.chunks()); self.assertEqual([len(c) for c in chunks],[65536,65536,11])
                self.assertEqual(staged.content_sha256,sha256(b''.join(chunks)).hexdigest())
            with patch.object(contracts,'MAX_MANIFEST',100):
                with self.assertRaises(WaveformError): contracts.stage_manifest(dict(text='a'*101))
            with self.assertRaises(ValueError): contracts.stage_manifest(dict(number=float('nan')))
        self.assertTrue(all(h.closed for h in handles))

    def test_payload_duplicates_multi_source_and_well_groups(self):
        splits=feature(self,'dataset_splits')
        from backend.waveforms.errors import WaveformError
        a=physical_member(1,'train',0,10,device='A',digest='same',well='one')
        b=physical_member(2,'test',100,110,device='B',digest='same',well='two')
        with self.assertRaises(WaveformError): splits.check_splits([a,b],dict(kind='well_holdout'))
        b['snapshot']['payload_fingerprints']=[]
        self.assertTrue(splits.check_splits([a,b],dict(kind='well_holdout')))
        b['snapshot']['registry']['freeze']['well']['id']='one'
        with self.assertRaises(WaveformError): splits.check_splits([a,b],dict(kind='well_holdout'))
        b['snapshot']['registry']['freeze']['well']['id']='two'
        a['snapshot']['physical_cycles'][0]['source_intervals'].extend(b['snapshot']['physical_cycles'][0]['source_intervals'])
        with self.assertRaises(WaveformError): splits.check_splits([a,b],dict(kind='well_holdout'))

    def test_freeze_immutable_manifest(self):
        contracts=feature(self,'dataset_contracts')
        literal=b'{"creator":{"domain":"local","user_id":"u"},"dataset_id":"00000000-0000-0000-0000-000000000001","draft_revision":"3","files":[],"members":[],"protocol":"waveform.dataset-manifest/1","purpose":"raw_archive","reviewer":{"domain":"local","user_id":"r"},"snapshot_versions":{"pair":"waveform.pair-content/1","quality":"waveform.quality/1","split":"waveform.dataset-split/1"},"source_kinds":[],"split_plan":{"kind":"none"},"title":"A","version_id":"00000000-0000-0000-0000-000000000002"}'
        import json
        with contracts.stage_manifest(json.loads(literal)) as staged:
            self.assertEqual(b''.join(staged.chunks()),literal)
            self.assertEqual(staged.content_sha256,'58ff484a499b580bee231d045baa4350ee912b0750f2cadd6d26403c93c1efec')
            self.assertEqual(staged.length,482)
        self.assertTrue(staged.closed)

    def test_create_command_literal_and_strict_rationals(self):
        contracts=feature(self,'dataset_contracts')
        from backend.waveforms.commands import Actor,Command,dataset_request_hash
        from backend.waveforms.errors import ContractError
        command=Command(Actor('local','u','U','operator'),UUID(int=1),0,'seed','0'*64)
        body=dict(title='A',purpose='raw_archive',split_plan=dict(kind='none'))
        self.assertEqual(dataset_request_hash('create',None,command,body=body),'7d2a1383567d56236397650b67cda78bf26055aacc6d3fb40713c8a8d20f146a')
        for value in ('2/2','1/1','-0','+1',' 1','1.0','01','1/0','1/-2','1/'+'9'*128,True):
            with self.subTest(value=value),self.assertRaises(ContractError): contracts.exact_fraction(value)
        self.assertEqual(str(contracts.exact_fraction('1999/2')),'1999/2')

    def test_same_cycle_variants_cannot_cross_splits(self):
        splits=feature(self,'dataset_splits')
        from backend.waveforms.errors import WaveformError
        members=[physical_member(1,'train',0,10),physical_member(2,'train',8,18),physical_member(3,'test',16,26)]
        with self.assertRaises(WaveformError): splits.check_splits(members,dict(kind='well_holdout'))
        members=[physical_member(1,'train',0,10,well='one'),physical_member(2,'test',10,20,well='two')]
        with self.assertRaises(WaveformError): splits.check_splits(members,dict(kind='well_holdout'))

    def test_exact_time_boundaries_gap_and_unknown_anchor(self):
        splits=feature(self,'dataset_splits')
        from backend.waveforms.errors import WaveformError
        plan=dict(kind='time_holdout',gap_ns='1/2',intervals=dict(train=dict(start_ns='0',end_ns='1999/2'),validation=None,test=dict(start_ns='1000',end_ns='2000')))
        members=[physical_member(1,'train',0,999),physical_member(2,'test',1000,2000,boot='b2')]
        members[0]['snapshot']['registry']['freeze']['upper_ns']='1999/2'
        self.assertTrue(splits.check_splits(members,plan))
        members[0]['snapshot']['registry']['freeze']['upper_ns']='1000'
        with self.assertRaises(WaveformError): splits.check_splits(members,plan)
        members[0]['snapshot']['registry']['freeze']['upper_ns']='999'
        members[1]['snapshot']['registry']['freeze']['utc_start']=None
        with self.assertRaises(WaveformError): splits.check_splits(members,plan)


class DatasetDatabaseTests(DatasetCase):
    def test_raw_scan_hash_and_manifest_staging_precede_guards(self):
        from unittest.mock import patch
        from backend.waveforms import dataset_context,datasets
        from backend.waveforms.commands import Actor
        pair,_=self.pair(two=True); service,draft=self.submitted(pair); state=dict(guarded=False,scans=0,raws=0,stages=0)
        class TrackedConnection:
            def __init__(self,conn): self.connection=conn
            def __getattr__(self,name): return getattr(self.connection,name)
            def __enter__(self): self.connection.__enter__(); return self
            def __exit__(self,*args):
                try: return self.connection.__exit__(*args)
                finally: state['guarded']=False
        service._connect=lambda:TrackedConnection(self.connect('operator'))
        guard=service._guard; raw=service._raw; scan=dataset_context.iter_samples; stage=datasets.stage_manifest
        def guarded(cur):
            result=guard(cur); state['guarded']=True; return result
        def raw_before(*args):
            self.assertFalse(state['guarded']); state['raws']+=1; return raw(*args)
        def scan_before(*args,**kwargs):
            self.assertFalse(state['guarded']); state['scans']+=1; return scan(*args,**kwargs)
        def stage_before(*args):
            self.assertFalse(state['guarded']); state['stages']+=1; return stage(*args)
        service._guard=guarded; service._raw=raw_before; body=dict(target_id=str(draft.id))
        with patch.object(dataset_context,'iter_samples',scan_before),patch.object(datasets,'stage_manifest',stage_before):
            service.freeze(body,self.command('freeze',draft.id,draft.revision,Actor('local','r','R','admin'),body=body))
        self.assertEqual((state['raws'],state['scans'],state['stages']),(4,2,1))
        self.assertTrue(all(c.closed or c is self.guard for c in self.connections))

    def test_exact_create_quota_rolls_back_all_five_rows(self):
        from dataclasses import replace
        from backend.waveforms.config import CapacityLimits
        from backend.waveforms.datasets import DatasetService
        from backend.waveforms.errors import WaveformError
        from tests.waveform_support import LIMITS
        limits=CapacityLimits(LIMITS.global_quota,{'GW':replace(LIMITS.gateways['GW'],metadata_rows=4)})
        with self.capacity_fixture(limits):
            service=DatasetService(lambda:self.connect('operator'),limits=limits,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
            body=dict(title='Quota',purpose='raw_archive',split_plan=dict(kind='none'))
            with self.assertRaises(WaveformError): service.create(body,self.command('create',body=body))
            for table in ('dataset_drafts','dataset_revisions','dataset_events','business_audit','business_receipts'):
                self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.'+table,role='operator'),0)
            self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner'),0)
            self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='*'",role='migration_owner'),0)

    def test_freeze_commit_loss_replay_immutable_rows_and_closed_publication(self):
        import psycopg
        from psycopg import sql
        from backend.waveforms.commands import Actor
        from backend.waveforms.errors import WaveformError
        pair,_=self.pair(); actor=Actor('local','freeze','Freeze','admin')
        class CommitLoss:
            def __init__(self,conn,after): self.connection,self.after=conn,after
            def __getattr__(self,name): return getattr(self.connection,name)
            def __enter__(self): self.connection.__enter__(); return self
            def __exit__(self,*args): return self.connection.__exit__(*args)
            def commit(self):
                if self.after: self.connection.commit()
                raise psycopg.OperationalError('synthetic freeze commit response loss')
        for after in (False,True):
            service,draft=self.submitted(pair); body=dict(target_id=str(draft.id)); command=self.command('freeze',draft.id,draft.revision,actor,body=body)
            saved=service._connect; service._connect=lambda:CommitLoss(self.connect('operator'),after)
            with self.assertRaises(WaveformError): service.freeze(body,command)
            service._connect=saved
            self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_versions WHERE dataset_id=%s',(draft.id,)),int(after))
            self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.business_receipts WHERE idempotency_key=%s',(command.key,),role='operator'),int(after))
            before=self.scalar('SELECT count(*) FROM waveforms.audit_metadata')
            version=service.freeze(body,command)
            if after: self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.audit_metadata'),before)
            counts=(self.scalar('SELECT count(*) FROM waveforms.business_audit'),self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner'))
            self.assertEqual(service.freeze(body,command),version)
            self.assertEqual((self.scalar('SELECT count(*) FROM waveforms.business_audit'),self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner')),counts)
        for table in ('dataset_drafts','dataset_revisions','dataset_events','dataset_versions','dataset_members','dataset_files','dataset_manifest_chunks'):
            for operation in ('UPDATE','DELETE'):
                with self.subTest(table=table,operation=operation),self.assertRaises(psycopg.Error):
                    with self.connect('migration_owner') as conn:
                        statement=sql.SQL('UPDATE waveforms.{} SET accounting_bucket=accounting_bucket' if operation=='UPDATE' else 'DELETE FROM waveforms.{}').format(sql.Identifier(table))
                        conn.execute(statement)
        with self.assertRaises(psycopg.Error):
            with self.connect('operator') as conn:
                conn.execute("INSERT INTO waveforms.dataset_manifest_chunks(version_id,ordinal,accounting_bucket,payload) VALUES(%s,0,'GW','x'::bytea)",(version.id,))
        self.assertEqual(service.get(version.id).content_sha256,version.content_sha256)
        self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='*'",role='migration_owner'),0)

    def test_draft_idempotency_content_and_purpose_barriers(self):
        from copy import deepcopy
        from dataclasses import replace
        from backend.waveforms.commands import Actor
        from backend.waveforms.errors import WaveformError,ContractError
        from backend.waveforms.dataset_splits import check_splits
        service=self.dataset_service(); body=dict(title='Bound content',purpose='raw_archive',split_plan=dict(kind='none'))
        cmd=self.command('create',body=body); draft=service.create(body,cmd)
        with self.assertRaises(WaveformError): service.create(body|dict(title='Other'),cmd)
        alternate=self.command('create',body=body|dict(title='Other'))
        with self.assertRaises(WaveformError): service.create(body|dict(title='Other'),replace(alternate,key=cmd.key))
        invalid=dict(target_id=str(draft.id),changes=dict(upsert_members=[]))
        with self.assertRaises(ContractError): self.command('update',draft.id,draft.revision,body=invalid)
        member=physical_member(1,'train',0,10); other=physical_member(2,'test',20,30,device='other',well='other')
        member['snapshot']['physical_cycles']=[]
        with self.assertRaises(WaveformError): check_splits([member,other],dict(kind='well_holdout'))
        pair,_=self.pair(); service,draft=self.submitted(pair,purpose='field_pair_input'); target=dict(target_id=str(draft.id))
        with self.assertRaises(WaveformError) as caught: service.freeze(target,self.command('freeze',draft.id,draft.revision,Actor('local','r','R','admin'),body=target))
        self.assertEqual(caught.exception.code,'waveform.dataset_purpose_mismatch')
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_versions'),0)

    def test_late_conflict_before_guard_and_after_guard(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading,time,json
        from backend.waveforms.commands import Actor
        from backend.waveforms.repository import WaveformRepository
        from backend.waveforms.errors import WaveformError
        from tests.test_waveform_repository import observation
        from tests.waveform_vectors import encoded
        from tests.waveform_support import LIMITS
        for guarded in (False,True):
            pair,vector=self.pair(); service,draft=self.submitted(pair); held=threading.Event(); release=threading.Event()
            completion=self.scalar('SELECT result FROM waveforms.completion WHERE capture_pk=(SELECT capture_pk FROM waveforms.quality_runs WHERE quality_run_id=%s)',(pair.left_quality_run_id,))
            body=dict(target_id=str(draft.id)); command=self.command('freeze',draft.id,draft.revision,Actor('local','freeze','Freeze','admin'),body=body)
            conflict=json.loads(vector['raws'][0]); conflict['producer']['recorder_version']='late conflict'
            def ingest():
                return WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW').ingest(observation(encoded(conflict),topic='oil/v2/GW/D0/waveform'))
            if not guarded:
                original=service.prepare_dataset
                def changed(*args):
                    prepared=original(*args); ingest(); return prepared
                service.prepare_dataset=changed
                with self.assertRaises(WaveformError): service.freeze(body,command)
                self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_versions'),0)
            else:
                original=service.recheck_dataset
                def paused(cur,prepared,*,strict):
                    result=original(cur,prepared,strict=strict)
                    if strict: held.set(); self.assertTrue(release.wait(2))
                    return result
                service.recheck_dataset=paused
                with ThreadPoolExecutor(max_workers=2) as pool:
                    freezing=pool.submit(service.freeze,body,command); self.assertTrue(held.wait(30)); arriving=pool.submit(ingest)
                    try:
                        deadline=time.monotonic()+1; waiting=False
                        while time.monotonic()<deadline:
                            with self.connect('migration_owner') as conn: waiting=conn.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE NOT granted)").fetchone()[0]
                            if waiting: break
                            time.sleep(.01)
                        self.assertTrue(waiting)
                    finally: release.set()
                    version=freezing.result(timeout=10); arriving.result(timeout=10)
                self.assertEqual(service.get(version.id).restriction_state,'restricted')
            self.assertEqual(self.scalar('SELECT result FROM waveforms.completion WHERE capture_pk=(SELECT capture_pk FROM waveforms.quality_runs WHERE quality_run_id=%s)',(pair.left_quality_run_id,)),completion)

    def test_database_collation_order_and_bounded_capture_batches(self):
        from uuid import uuid4
        from tests.waveform_support import FIXED_TIME
        captures={}
        # Existing rows are all that immutable003's lock helper requires. No raw read.
        with self.connect('receiver') as conn:
            for n in range(65):
                id=uuid4(); gateway='GW'; device=('a','A','z','Z','a.','A-','a_')[n%7]+str(n%3)
                conn.execute("INSERT INTO waveforms.captures(capture_pk,intake_gateway_id,gateway_id,device_id,capture_id,first_seen,last_valid_at) VALUES(%s,'GW',%s,%s,%s,%s,%s)",(id,gateway,device,uuid4(),FIXED_TIME,FIXED_TIME))
                captures[n]=dict(capture_pk=id)
        expected=self.scalar("SELECT array_agg(capture_pk ORDER BY gateway_id,device_id,capture_id) FROM waveforms.capture_metadata")
        batches=[]
        class ObservedCursor:
            def __init__(self,cur): self.cur=cur
            def execute(self,statement,params):
                if statement.startswith('SELECT waveforms.lock_business_captures'): batches.append(params[0])
                return self.cur.execute(statement,params)
            def fetchall(self): return self.cur.fetchall()
        with self.connect('operator') as conn:
            self.dataset_service().lock_captures(ObservedCursor(conn.cursor()),captures)
            self.assertEqual([len(batch) for batch in batches],[32,32,1])
            self.assertEqual([id for batch in batches for id in batch],expected)
            conn.rollback()

    def test_incremental_1000_members_exact_replay_and_terminal_submission(self):
        from uuid import uuid4
        from backend.waveforms.commands import Actor
        from backend.waveforms.errors import WaveformError,ContractError
        service=self.dataset_service(); body=dict(title='Batch test',purpose='raw_archive',split_plan=dict(kind='none'))
        create=self.command('create',body=body); draft=service.create(body,create)
        before=self.scalar("SELECT used_metadata_rows FROM waveforms.capacity_metadata WHERE bucket='GW'")
        self.assertEqual(before,5)
        self.assertEqual(service.create(body,create),draft)
        old=draft
        for batch in range(10):
            members=[dict(kind='capture',reference_id=str(UUID(int=1+batch*100+n)),quality_run_id=str(uuid4()),split='unassigned') for n in range(100)]
            body=dict(target_id=str(draft.id),changes=dict(upsert_members=members))
            command=self.command('update',draft.id,draft.revision,body=body); draft=service.update(body,command)
            self.assertEqual(service.update(body,command),draft)
        self.assertEqual(draft.member_count,1000)
        self.assertEqual(service.get_draft(old.id,old.revision),old)
        self.assertEqual(len(service.draft_members(draft.id,offset=900)),100)
        self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity_metadata WHERE bucket='GW'"),45)
        body=dict(target_id=str(draft.id),changes=dict(upsert_members=[dict(kind='capture',reference_id=str(uuid4()),quality_run_id=str(uuid4()),split='unassigned')]))
        with self.assertRaises(WaveformError): service.update(body,self.command('update',draft.id,draft.revision,body=body))
        self.assertEqual(service.get_draft(draft.id).revision,11)
        body=dict(target_id=str(draft.id)); other=Actor('local','submitter','Submitter','operator')
        draft=service.submit(body,self.command('submit',draft.id,draft.revision,other,body=body))
        for actor in (other,old.creator):
            with self.assertRaises(WaveformError): service.freeze(body,self.command('freeze',draft.id,draft.revision,actor,body=body))
        edit=dict(target_id=str(draft.id),changes=dict(remove_members=[dict(kind='capture',reference_id=str(UUID(int=1)))]))
        with self.assertRaises(WaveformError): service.update(edit,self.command('update',draft.id,draft.revision,body=edit))
        self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='*'",role='migration_owner'),0)
        self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner'),49)
        self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity_metadata WHERE bucket='*'"),49)

    def test_freeze_then_revoke_has_no_check_commit_gap(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading,time
        from backend.waveforms.commands import Actor
        pair,vector=self.pair(two=True); service,draft=self.submitted(pair)
        held=threading.Event(); release=threading.Event(); recheck=service.recheck_dataset
        def pause(cur,prepared,*,strict):
            result=recheck(cur,prepared,strict=strict)
            if strict:
                held.set()
                self.assertTrue(release.wait(2))
            return result
        service.recheck_dataset=pause
        id=UUID(vector['selection']['channels'][-1]['calibration']['id']); body=dict(target_id=str(draft.id))
        with ThreadPoolExecutor(max_workers=2) as pool:
            freezing=pool.submit(service.freeze,body,self.command('freeze',draft.id,draft.revision,Actor('local','freeze','Freeze','admin'),body=body))
            self.assertTrue(held.wait(30))
            revoke=pool.submit(vector['evidence_service'].revoke,id,vector['evidence_command']('revoke',id,2))
            try:
                deadline=time.monotonic()+1; waiting=False
                while time.monotonic()<deadline:
                    with self.connect('migration_owner') as conn: waiting=conn.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted)").fetchone()[0]
                    if waiting: break
                    time.sleep(.01)
                self.assertTrue(waiting)
            finally: release.set()
            version=freezing.result(timeout=10); revoke.result(timeout=10)
        self.assertEqual(service.get(version.id).restriction_state,'restricted')
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_versions'),1)

    def test_revoke_then_freeze_waits_and_refuses(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading,time
        from backend.waveforms.commands import Actor
        from backend.waveforms.errors import WaveformError
        pair,vector=self.pair(); service,draft=self.submitted(pair); evidence=vector['evidence_service']
        held=threading.Event(); release=threading.Event(); event=evidence._event
        def pause(*args,**kwargs):
            result=event(*args,**kwargs); held.set(); self.assertTrue(release.wait(2)); return result
        evidence._event=pause
        id=vector['profile'].id; body=dict(target_id=str(draft.id))
        with ThreadPoolExecutor(max_workers=2) as pool:
            revoke=pool.submit(evidence.revoke,id,vector['evidence_command']('revoke',id,2)); self.assertTrue(held.wait(5))
            freezing=pool.submit(service.freeze,body,self.command('freeze',draft.id,draft.revision,Actor('local','freeze','Freeze','admin'),body=body))
            try:
                deadline=time.monotonic()+1; waiting=False
                while time.monotonic()<deadline:
                    with self.connect('migration_owner') as conn: waiting=conn.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted)").fetchone()[0]
                    if waiting: break
                    time.sleep(.01)
                self.assertTrue(waiting)
            finally: release.set()
            revoke.result(timeout=5)
            with self.assertRaises(WaveformError): freezing.result(timeout=10)
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_versions'),0)

    def test_freeze_immutable_manifest(self):
        feature(self,'datasets')
        service,version,vector=self.frozen(two=True)
        from backend.waveforms.commands import Actor
        self.assertEqual(version.restriction_state,'clear')
        self.assertIsInstance(version.manifest_bytes,int)
        first=self.scalar('SELECT string_agg(payload,\'\'::bytea ORDER BY ordinal) FROM waveforms.dataset_manifest_chunks WHERE version_id=%s',(version.id,))
        self.assertEqual(sha256(first).hexdigest(),version.content_sha256)
        self.assertEqual(len(first),version.manifest_bytes)
        import json
        manifest=json.loads(first)
        self.assertTrue(all('stream_data_sha256' in p for m in manifest['members'] for p in m['snapshot']['payload_fingerprints']))
        self.assertEqual(len(service.members(version.id)),1)
        self.assertEqual(service.get(version.id).content_sha256,version.content_sha256)

    def test_revoke_racing_freeze(self):
        feature(self,'datasets')
        from backend.waveforms.commands import Actor
        from backend.waveforms.errors import WaveformError
        pair,vector=self.pair(two=True); service,draft=self.submitted(pair)
        evidence=vector['evidence_service']; cmd=vector['evidence_command']; id=UUID(vector['selection']['channels'][-1]['calibration']['id'])
        evidence.revoke(id,cmd('revoke',id,2))
        body=dict(target_id=str(draft.id))
        with self.assertRaises(WaveformError): service.freeze(body,self.command('freeze',draft.id,draft.revision,Actor('local','r','R','admin'),body=body))
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_versions'),0)


class DatasetRegistryTests(DatasetCase,WaveformRegistryCase):
    def test_unrelated_revision_before_preparation_allowed_after_preparation_stale(self):
        from backend.waveforms.commands import Actor
        from backend.waveforms.errors import WaveformError
        pair,_=self.pair(); service,draft=self.submitted(pair)
        self.registry_command('well.create',area_code='A',well_code='BEFORE',name='Synthetic unrelated before preparation')
        body=dict(target_id=str(draft.id)); actor=Actor('local','freeze','Freeze','admin')
        version=service.freeze(body,self.command('freeze',draft.id,draft.revision,actor,body=body))
        self.assertEqual(version.restriction_state,'clear')
        snapshot=self.scalar('SELECT snapshot FROM waveforms.dataset_members WHERE version_id=%s',(version.id,))
        self.assertNotEqual(snapshot['registry']['historical']['revision'],snapshot['registry']['freeze']['revision'])
        service,draft=self.submitted(pair); original=service.prepare_dataset
        def changed(*args):
            prepared=original(*args)
            self.registry_command('well.create',area_code='A',well_code='AFTER',name='Synthetic unrelated after preparation')
            return prepared
        service.prepare_dataset=changed; body=dict(target_id=str(draft.id))
        with self.assertRaises(WaveformError) as caught: service.freeze(body,self.command('freeze',draft.id,draft.revision,actor,body=body))
        self.assertEqual(caught.exception.code,'waveform.dataset_stale_preparation')
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_versions'),1)
        self.assertEqual(service.get(version.id).restriction_state,'clear')

    def test_registry_writer_waits_for_frozen_publication(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading,time
        from backend.waveforms.commands import Actor
        pair,_=self.pair(); service,draft=self.submitted(pair); held=threading.Event(); release=threading.Event(); original=service.recheck_dataset
        def paused(cur,prepared,*,strict):
            result=original(cur,prepared,strict=strict)
            if strict: held.set(); self.assertTrue(release.wait(2))
            return result
        service.recheck_dataset=paused; body=dict(target_id=str(draft.id))
        with ThreadPoolExecutor(max_workers=2) as pool:
            freezing=pool.submit(service.freeze,body,self.command('freeze',draft.id,draft.revision,Actor('local','freeze','Freeze','admin'),body=body))
            self.assertTrue(held.wait(30))
            writer=pool.submit(self.registry_command,'well.create',area_code='A',well_code='WAIT',name='Synthetic concurrent dataset history')
            try:
                deadline=time.monotonic()+1; waiting=False
                while time.monotonic()<deadline:
                    with self.registry_connect() as conn: waiting=conn.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted)").fetchone()[0]
                    if waiting: break
                    time.sleep(.01)
                self.assertTrue(waiting); self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_versions'),0)
            finally: release.set()
            version=freezing.result(timeout=10); writer.result(timeout=10)
        self.assertEqual(service.get(version.id).restriction_state,'clear')
