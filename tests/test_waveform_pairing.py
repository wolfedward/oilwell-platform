"""Fixed commands and real durable joint candidates."""
import importlib
import unittest
from uuid import UUID
from backend.waveforms.commands import Actor,Command
from tests.waveform_support import LIMITS
from tests.waveform_registry_support import WaveformRegistryCase


class PairContractTests(unittest.TestCase):
    def test_independent_content_hash_binds_method_and_selection(self):
        from backend.waveforms.pair_contracts import pair_content,METHOD_PARAMETERS
        selection=dict(purpose='raw_archive',profile=None,captures=[dict(key=dict(gateway_id='GW',device_id='D',capture_id=str(UUID(int=1))),manifest_sha256=None,origins=[])],channels=[],cycle=None,sync=None)
        body=dict(selection=selection,selection_sha256='d262a6bc6cf580f42049417c05eb501cd0d34cd1b4b8b989068d527bad14d11f',
            quality_run_ids=[str(UUID(int=9))]*2,physical_cycles=[],method_parameters=METHOD_PARAMETERS,
            joint_result=dict(cycle_summary=None,evidence_ids=[],purpose='raw_archive',reasons=['incomplete'],status='pending',validator_version='waveform.quality/1'))
        self.assertEqual(pair_content(body,'linear_adjacent/1'),'4e07286182052b52d574b49851c9d098cdcec4faf27114f5d60d616766f2afdf')
        self.assertNotEqual(pair_content(body,'linear_adjacent/1'),pair_content(body,'changed/2'))

    def test_selection_literal_hash_and_strict_fields(self):
        try: contracts=importlib.import_module('backend.waveforms.pair_contracts')
        except ModuleNotFoundError: self.fail('Task11 selection implementation is absent')
        from backend.waveforms.errors import ContractError
        selection=dict(purpose='raw_archive',profile=None,captures=[dict(key=dict(gateway_id='GW',device_id='D',capture_id=str(UUID(int=1))),
            manifest_sha256=None,origins=[])],channels=[],cycle=None,sync=None)
        self.assertEqual(contracts.selection_hash(selection),'d262a6bc6cf580f42049417c05eb501cd0d34cd1b4b8b989068d527bad14d11f')
        with self.assertRaises(ContractError): contracts.validate_selection(selection|{'eligible':True})

    def test_review_literal_hash(self):
        from backend.waveforms import commands
        self.assertTrue(hasattr(commands,'pair_request_hash'),'Task11 pair command binding is absent')
        command=Command(Actor('local','1','User','admin'),UUID(int=8),1,'synthetic test','0'*64)
        self.assertEqual(commands.pair_request_hash('review',UUID(int=9),command,decision='approve'),
                         '39ea27ba7e0136a85b6faac05b4556ef46563eb5cd4c50c84d8c60801c6ec22b')


class PairDatabaseTests(WaveformRegistryCase):
    def test_sealed_offline_origin_qualifies_but_shared_complete_unsealed_sources_do_not(self):
        from copy import deepcopy
        from dataclasses import replace
        from hashlib import sha256
        from uuid import uuid4
        from backend.waveforms.imports import ImportService
        from backend.waveforms.commands import import_request_hash
        from backend.waveforms.evidence_contracts import thaw
        from backend.waveforms.pair_contracts import capture_key
        from tests.waveform_pairing_support import seed_pair,CREATOR,REVIEWER
        vector=seed_pair(self); quality,pairs=self.services()
        payloads=vector['wire_captures'][0]; raw=b'\n'.join(payloads); chosen=vector['selection']['captures'][0]
        metadata=dict(chosen['key'],file_sha256=sha256(raw).hexdigest(),bytes=len(raw),line_count=len(payloads),final_lf=False)
        imports=ImportService(self.connect,limits=LIMITS,prefix='oil')
        facts=self.scalar('SELECT count(*) FROM waveforms.facts',role='receiver')
        def import_command(operation,target=None,version=0,**values):
            command=Command(CREATOR,uuid4(),version,'Synthetic offline pairing closure','0'*64)
            return replace(command,request_hash=import_request_hash(operation,target,command,**values))
        def create_source(*,all_lines):
            session=imports.create(metadata,import_command('create',metadata=metadata))
            for line,payload in enumerate(payloads if all_lines else payloads[:1]):
                session=imports.append(session.import_id,line,payload,
                    import_command('append',session.import_id,session.version,line=line,payload=payload))
            service=vector['evidence_service']; command=vector['evidence_command']
            original=service.get(UUID(chosen['origins'][0]['id'])); body=thaw(original.body)
            with self.connect('reader') as conn:
                observation,fact,outcome=conn.execute('SELECT observation_id,fact_id,outcome_id FROM waveforms.provenance_metadata WHERE source_ref=%s AND kind=%s',
                    (session.import_id,'manifest')).fetchone()
            body['broker_config_ref']=None
            body['provenance']=dict(transport='file',source_ref=str(session.import_id),import_id=str(session.import_id),
                observation_id=str(observation),fact_id=str(fact),outcome_id=str(outcome),manifest_sha256=chosen['manifest_sha256'])
            created=service.create('origin',body,command('create',kind='origin',body=body))
            origin=service.review(created.id,decision='approve',command=command('review',created.id,1,REVIEWER,decision='approve'))
            selection=deepcopy(vector['selection'])
            selection['captures'][0]['origins']=[dict(id=str(origin.id),content_sha256=origin.content_sha256,version=str(origin.version))]
            return session,selection
        recorded,unsealed=create_source(all_lines=True)
        cancelled,cancelled_selection=create_source(all_lines=False)
        imports.cancel(cancelled.import_id,import_command('cancel',cancelled.import_id,cancelled.version))
        for selection in (unsealed,cancelled_selection):
            self.assertEqual(self.scalar('SELECT state FROM waveforms.capture_metadata'),'complete')
            body=dict(selection=selection,method_version='linear_adjacent/1')
            pair=pairs.propose(body,self.command('propose',body=body))
            self.assertEqual(pair.body['joint_result']['status'],'pending')
            self.assertIn('import_not_sealed',pair.body['joint_result']['reasons'])
        self.assertEqual(imports.seal(recorded.import_id,import_command('seal',recorded.import_id,recorded.version)).state,'sealed')
        body=dict(selection=unsealed,method_version='linear_adjacent/1')
        pair=pairs.propose(body,self.command('propose',body=body))
        self.assertEqual(pair.body['joint_result']['status'],'eligible')
        self.assertEqual(pairs.review(pair.id,decision='approve',command=self.command('review',pair.id,version=1,actor=REVIEWER,decision='approve')).version,2)
        key=capture_key(chosen['key']); runs=quality.record(key,unsealed,self.command('record',key,body=unsealed))
        self.assertEqual(runs.runs[0].status,'eligible')
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.facts',role='receiver'),facts)

    def test_each_capture_origin_required_even_for_same_source_boot(self):
        from tests.waveform_pairing_support import seed_pair
        from copy import deepcopy
        vector=seed_pair(self,two=True,same_source=True); _,pairs=self.services()
        body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
        good=pairs.propose(body,self.command('propose',body=body))
        self.assertEqual(good.body['joint_result']['status'],'eligible')
        changed=deepcopy(body); changed['selection']['captures'][0]['origins']=[]
        bad=pairs.propose(changed,self.command('propose',body=changed))
        self.assertNotEqual(bad.body['joint_result']['status'],'eligible')
        self.assertIn('unknown_origin',bad.body['joint_result']['reasons'])

    def test_independent_review_concurrency_new_profile_and_immutability(self):
        from tests.waveform_pairing_support import seed_pair,REVIEWER,profile_body
        from backend.waveforms.errors import WaveformError,ContractError
        from concurrent.futures import ThreadPoolExecutor
        from copy import deepcopy
        import psycopg
        vector=seed_pair(self); _,pairs=self.services(); body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
        pair=pairs.propose(body,self.command('propose',body=body))
        def review(decision):
            try: return pairs.review(pair.id,decision=decision,command=self.command('review',pair.id,version=1,decision=decision,actor=REVIEWER))
            except WaveformError as error: return error
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(review,('approve','reject')))
        self.assertEqual(sum(isinstance(r,WaveformError) for r in results),1)
        self.assertEqual(pairs.get(pair.id).version,2)
        service=vector['evidence_service']; command=vector['evidence_command']; profile=profile_body()
        profile['trial_basis']='New explicit synthetic trial version'
        created=service.create('profile',profile,command('create',kind='profile',body=profile))
        approved=service.review(created.id,decision='approve',command=command('review',created.id,1,REVIEWER,decision='approve'))
        changed=deepcopy(body); changed['selection']['profile']=dict(id=str(approved.id),content_sha256=approved.content_sha256,version='2')
        next_pair=pairs.propose(changed,self.command('propose',body=changed))
        self.assertEqual(next_pair.version,1); self.assertEqual(next_pair.review_state,'proposed')
        self.assertNotEqual(next_pair.content_sha256,pair.content_sha256)
        self.assertEqual(next_pair.body['physical_cycles'][0]['physical_cycle_key'],pair.body['physical_cycles'][0]['physical_cycle_key'])
        with self.assertRaises(ContractError): pairs.propose(body|{'method_version':'unregistered/2'},self.command('propose',body=body))
        with self.assertRaises(WaveformError): pairs.propose(changed,self.command('propose',body=body))
        with self.assertRaises(psycopg.Error):
            with self.connect('migration_owner') as conn: conn.execute("UPDATE waveforms.quality_runs SET status='eligible'")
        pairs.review(next_pair.id,decision='revoke',command=self.command('review',next_pair.id,version=1,decision='revoke',actor=REVIEWER))
        inspection=pairs.inspect(next_pair.id,actor=REVIEWER)
        self.assertEqual(inspection.current_status,'ineligible')
        self.assertIn('pair_revoked',inspection.current_reasons)

    def test_raw_audit_commit_failure_never_records_evaluation(self):
        from tests.waveform_pairing_support import seed_pair
        from backend.waveforms.pairing import PairService
        from backend.waveforms.errors import WaveformError
        import psycopg
        vector=seed_pair(self); case=self
        class FailedReadCommit:
            def __init__(self,conn): self.connection=conn
            def __getattr__(self,name): return getattr(self.connection,name)
            def __enter__(self): self.connection.__enter__(); return self
            def __exit__(self,*args): return self.connection.__exit__(*args)
            def commit(self): raise psycopg.OperationalError('synthetic raw audit commit failure')
        pairs=PairService(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:FailedReadCommit(case.connect('reader')))
        body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
        before=self.scalar('SELECT count(*) FROM waveforms.audit_metadata')
        with self.assertRaises(WaveformError): pairs.propose(body,self.command('propose',body=body))
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.audit_metadata'),before)
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.quality_runs'),0)

    def test_registry_change_before_guard_rejects_stale_preparation(self):
        from tests.waveform_pairing_support import seed_pair
        from backend.waveforms.pairing import PairService
        from backend.waveforms.errors import WaveformError
        vector=seed_pair(self); case=self
        class RegistryChanged(PairService):
            def prepare(self,*args,**kwargs):
                ready=super().prepare(*args,**kwargs)
                case.registry_command('well.create',area_code='A',well_code='STALE',name='Synthetic changed history')
                return ready
        pairs=RegistryChanged(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
        with self.assertRaises(WaveformError) as caught: pairs.propose(body,self.command('propose',body=body))
        self.assertEqual(caught.exception.code,'waveform.stale_dependency')
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.pair_versions'),0)
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.quality_runs'),0)

    def test_real_pair_commit_holds_history_guard_until_durable_finish(self):
        from tests.waveform_pairing_support import seed_pair
        from backend.waveforms.pairing import PairService
        from concurrent.futures import ThreadPoolExecutor
        import threading,time
        vector=seed_pair(self); case=self; entered=threading.Event(); release=threading.Event()
        class HeldPair(PairService):
            def _write_runs(self,*args,**kwargs):
                result=super()._write_runs(*args,**kwargs); entered.set()
                if not release.wait(3): raise AssertionError('test release absent')
                return result
        pairs=HeldPair(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
        with ThreadPoolExecutor(max_workers=2) as pool:
            pairing=pool.submit(pairs.propose,body,self.command('propose',body=body)); self.assertTrue(entered.wait(5))
            writing=pool.submit(self.registry_command,'well.create',area_code='A',well_code='WAIT',name='Synthetic concurrent registry')
            try:
                deadline=time.monotonic()+.7; waiting=False
                while time.monotonic()<deadline:
                    with self.registry_connect() as conn:
                        waiting=conn.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted)").fetchone()[0]
                    if waiting: break
                    time.sleep(.01)
                self.assertTrue(waiting)
                self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.pair_versions'),0)
            finally: release.set()
            self.assertEqual(pairing.result(timeout=5).body['joint_result']['status'],'eligible')
            self.assertEqual(writing.result(timeout=5)['registry_revision'],'2')

    def test_revoked_dependency_invalidates_current_use_but_not_history(self):
        from tests.waveform_pairing_support import seed_pair,REVIEWER
        from backend.waveforms.errors import WaveformError
        vector=seed_pair(self); _,pairs=self.services(); body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
        cmd=self.command('propose',body=body); pair=pairs.propose(body,cmd)
        original=pair.body['joint_result']['status']; self.assertEqual(original,'eligible')
        vector['evidence_service'].revoke(vector['sync'].id,vector['evidence_command']('revoke',vector['sync'].id,2))
        now=pairs.inspect(pair.id,actor=REVIEWER)
        self.assertFalse(now.dependencies_current); self.assertNotEqual(now.current_status,'eligible')
        self.assertEqual(pairs.get(pair.id).body['joint_result']['status'],'eligible')
        self.assertEqual(pairs.propose(body,cmd),pair)
        with self.assertRaises(WaveformError): pairs.review(pair.id,decision='approve',command=self.command('review',pair.id,version=1,decision='approve',actor=REVIEWER))
        with self.assertRaises(WaveformError): pairs.propose(body,self.command('propose',body=body))

    def test_late_conflict_after_preparation_is_visible_without_rewriting_completion(self):
        from tests.waveform_pairing_support import seed_pair,REVIEWER
        from backend.waveforms.pairing import PairService
        from backend.waveforms.repository import WaveformRepository
        from tests.test_waveform_repository import observation
        from tests.waveform_vectors import encoded
        import json
        vector=seed_pair(self); case=self
        class ArrivingConflict(PairService):
            def prepare(self,*args,**kwargs):
                ready=super().prepare(*args,**kwargs)
                body=json.loads(vector['raws'][0]); body['producer']['recorder_version']='late conflict'
                WaveformRepository(case.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW').ingest(
                    observation(encoded(body),topic='oil/v2/GW/D0/waveform'))
                return ready
        pairs=ArrivingConflict(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
        pair=pairs.propose(body,self.command('propose',body=body))
        self.assertEqual(pair.body['joint_result']['status'],'ineligible')
        self.assertIn('capture_conflict',pair.body['joint_result']['reasons'])
        self.assertEqual(self.scalar('SELECT state FROM waveforms.capture_metadata'),'complete')

    def test_full_sample_scan_precedes_business_locks_and_compact_identity(self):
        from tests.waveform_pairing_support import seed_pair
        from backend.waveforms.pairing import PairService
        from backend.waveforms.timing import ChannelData
        from unittest.mock import patch
        vector=seed_pair(self,count=20003,bad_index=20001,quality=4); stage={'locked':False,'before':0,'after':0}
        class Observed(PairService):
            def _begin(self,*args):
                stage['locked']=True
                return super()._begin(*args)
        original=ChannelData.sample
        def observed(data,index):
            stage['after' if stage['locked'] else 'before']+=1
            return original(data,index)
        pairs=Observed(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
        with patch.object(ChannelData,'sample',observed): pair=pairs.propose(body,self.command('propose',body=body))
        self.assertEqual(pair.body['joint_result']['status'],'ineligible')
        self.assertIn('bad_or_clipped_samples',pair.body['joint_result']['reasons'])
        self.assertGreater(stage['before'],60000); self.assertLess(stage['after'],100)
        self.assertLess(self.scalar('SELECT octet_length(body::text) FROM waveforms.pair_versions WHERE pair_version_id=%s',(pair.id,)),16000)

    def test_commit_loss_replay_and_quota_rollback(self):
        import psycopg
        from backend.waveforms.pairing import PairService
        from backend.waveforms.errors import WaveformError
        body=dict(selection=self.selection(),method_version='linear_adjacent/1'); case=self
        class CommitLost:
            def __init__(self,connection,after): self.connection,self.after=connection,after
            def __getattr__(self,key): return getattr(self.connection,key)
            def __enter__(self): self.connection.__enter__(); return self
            def __exit__(self,*args): return self.connection.__exit__(*args)
            def commit(self):
                if self.after: self.connection.commit()
                raise psycopg.OperationalError('synthetic commit response loss')
        for after in (False,True):
            cmd=self.command('propose',body=body)
            pairs=PairService(lambda:CommitLost(case.connect('operator'),after),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
            with self.assertRaises(WaveformError): pairs.propose(body,cmd)
            self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.business_receipts WHERE idempotency_key=%s',(cmd.key,),role='operator'),int(after))
            _,normal=self.services(); self.assertEqual(normal.propose(body,cmd).version,1)
        from dataclasses import replace
        from backend.waveforms.config import CapacityLimits
        limits=CapacityLimits(LIMITS.global_quota,{'GW':replace(LIMITS.gateways['GW'],metadata_rows=3)})
        with self.capacity_fixture(limits):
            pairs=PairService(lambda:self.connect('operator'),limits=limits,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
            with self.assertRaises(WaveformError): pairs.propose(body,self.command('propose',body=body))
            self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.pair_versions'),0)
            self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity_metadata WHERE bucket='GW'"),0)

    def test_same_and_two_capture_joint_eligibility_and_review(self):
        from tests.waveform_pairing_support import seed_pair,REVIEWER
        for two in (False,True):
            vector=seed_pair(self,two=two); _,pairs=self.services()
            body=dict(selection=vector['selection'],method_version='linear_adjacent/1')
            pair=pairs.propose(body,self.command('propose',body=body))
            self.assertEqual(pair.body['joint_result']['status'],'eligible',pair.body['joint_result']['reasons'])
            self.assertEqual(pair.left_quality_run_id==pair.right_quality_run_id,not two)
            with self.connect('reader') as conn:
                runs=conn.execute('SELECT parameters,result,evidence FROM waveforms.quality_runs WHERE quality_run_id=ANY(%s)',([pair.left_quality_run_id,pair.right_quality_run_id],)).fetchall()
            self.assertTrue(all(r==runs[0] for r in runs))
            review=pairs.review(pair.id,decision='approve',command=self.command('review',pair.id,version=1,actor=REVIEWER,decision='approve'))
            self.assertEqual(review.version,2)
            self.assertEqual(pairs.inspect(pair.id,actor=REVIEWER).current_status,'eligible')

    def services(self):
        try:
            from backend.waveforms.quality_service import QualityService
            from backend.waveforms.pairing import PairService
        except ModuleNotFoundError: self.fail('Task11 durable services absent')
        options=dict(limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        return QualityService(lambda:self.connect('operator'),**options),PairService(lambda:self.connect('operator'),**options)

    def selection(self):
        return dict(purpose='raw_archive',profile=None,captures=[dict(key=dict(gateway_id='GW',device_id='D',capture_id=str(UUID(int=1))),
            manifest_sha256=None,origins=[])],channels=[],cycle=None,sync=None)

    def command(self,operation,target=None,version=0,actor=None,**values):
        from dataclasses import replace
        from uuid import uuid4
        from backend.waveforms.commands import pair_request_hash,quality_request_hash
        actor=actor or Actor('local','creator','C','admin')
        command=Command(actor,uuid4(),version,'Synthetic test only','0'*64)
        digest=quality_request_hash(target,command,**values) if operation=='record' else pair_request_hash(operation,target,command,**values)
        return replace(command,request_hash=digest)

    def test_server_records_missing_dependencies_and_replay_is_noop(self):
        quality,_=self.services()
        from backend.waveforms.contracts import CaptureKey
        key=CaptureKey('GW','D',UUID(int=1)); selection=self.selection()
        command=self.command('record',key,body=selection)
        result=quality.record(key,selection,command)
        self.assertEqual(result.runs[0].status,'pending')
        self.assertIsNone(result.runs[0].manifest_sha256)
        self.assertIsNone(result.runs[0].profile_id)
        revision=self.scalar('SELECT version FROM waveforms.state_head',role='operator')
        rows=self.scalar("SELECT used_metadata_rows FROM waveforms.capacity_metadata WHERE bucket='GW'")
        self.assertEqual(quality.record(key,selection,command),result)
        self.assertEqual(self.scalar('SELECT version FROM waveforms.state_head',role='operator'),revision)
        self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity_metadata WHERE bucket='GW'"),rows)

    def test_pending_pair_never_approves_and_self_review_refuses(self):
        _,pairs=self.services(); body=dict(selection=self.selection(),method_version='linear_adjacent/1')
        command=self.command('propose',body=body)
        pair=pairs.propose(body,command)
        self.assertEqual(pair.left_quality_run_id,pair.right_quality_run_id)
        self.assertEqual(pair.version,1)
        from backend.waveforms.errors import WaveformError
        for actor in (command.actor,Actor('local','other','Other','admin')):
            with self.assertRaises(WaveformError):
                pairs.review(pair.id,decision='approve',command=self.command('review',pair.id,version=1,decision='approve',actor=actor))
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.pair_reviews'),0)
        rejected=pairs.review(pair.id,decision='reject',command=self.command('review',pair.id,version=1,decision='reject',actor=Actor('local','other','Other','admin')))
        self.assertEqual(rejected.decision,'reject')
        self.assertEqual(pairs.get(pair.id).review_state,'reject')
