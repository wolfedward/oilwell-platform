"""Governed synthetic evidence; these tests never certify field instruments."""
from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
import unittest
from uuid import UUID, uuid4

from backend.waveforms.commands import Actor, Command
from backend.waveforms.errors import ContractError
from tests.waveform_support import WaveformDatabaseCase, LIMITS


def sampling_body():
    return dict(source_kind='synthetic', valid_from='2026-09-29T00:00:00.000000Z',
        valid_to=None, attachment_ids=[], notes='Synthetic timing report',
        endpoint=dict(gateway_id='OTHER', device_id='D', stream_id='s', channel_id='c'),
        reported_config_id='CFG1', sensor_id=None, quantity='load', unit='kN',
        encoding='i32le', measurement_source='synthetic', signal_kind='instantaneous',
        effective_bits=None, input_range=None, gain=None, bias=None, wiring=None,
        analog_filter=None, digital_filter=None, rate=dict(num=100, den=1),
        clock=None, window=None, power=None, reported_point_map_version=None)


class EvidenceContractTests(unittest.TestCase):
    def sync_case(self):
        from dataclasses import replace
        from tests.waveform_vectors import golden_capture
        from backend.waveforms.wire import parse_frame
        frame=parse_frame(golden_capture()[0])
        stream=replace(frame.streams[0],rate_num=3,rate_den=2,frame_count=4,first_sample_index=9007199254741001,offset_ns=-100000000)
        frame=replace(frame,streams=(stream,))
        endpoint=dict(capture=dict(gateway_id=frame.key.gateway_id,device_id=frame.key.device_id,capture_id=str(frame.key.capture_id)),
                      source=dict(stream.source),stream_id=stream.stream_id,clock_domain_id=stream.clock_domain_id)
        item=dict(endpoint=endpoint,reference_sample_index='9007199254741002',reference_event='documented edge',
            nominal_reference_ns='1700000000/3',rate=dict(num=3,den=2),valid_start_ns='-100000000',
            valid_end_ns='2000000000',offset_error_ns='0',frequency_error_ppm='0',residual_ns='2',clock_divisor=None)
        body=dict(source_kind='synthetic',valid_from=None,valid_to=None,attachment_ids=[str(uuid4())],
                  notes='Synthetic independent event correspondence report',method='verified_linear_mapping',reference=endpoint,endpoints=[item])
        return body,{frame.key:frame}

    def test_fractional_sync_reference_checks_manifest_once(self):
        from backend.waveforms.evidence_contracts import sync_proof
        body,manifests=self.sync_case()
        proof=sync_proof(body,manifests,approved=True)
        self.assertEqual(proof['status'],'resolved')
        self.assertEqual(proof['endpoints'][0]['nominal_reference_ns'],'1700000000/3')
        body['endpoints'][0]['nominal_reference_ns']='566666667'
        with self.assertRaises(ContractError): sync_proof(body,manifests,approved=True)

    def test_sync_label_or_empty_report_cannot_qualify(self):
        from backend.waveforms.evidence_contracts import sync_proof
        body,manifests=self.sync_case()
        body['attachment_ids']=[]
        self.assertEqual(sync_proof(body,manifests,approved=True)['status'],'pending')
        body,manifests=self.sync_case()
        self.assertEqual(sync_proof(body,manifests,approved=False)['status'],'pending')
        body['endpoints'][0]['reference_sample_index']='9007199254741005'
        with self.assertRaises(ContractError): sync_proof(body,manifests,approved=True)

    def test_reference_drift_unknown_is_not_zero(self):
        from backend.waveforms.evidence_contracts import sync_proof
        body,manifests=self.sync_case()
        body['endpoints'][0]['frequency_error_ppm']=None
        self.assertEqual(sync_proof(body,manifests,approved=True)['status'],'pending')

    def test_cross_capture_event_requires_absolute_anchor_uncertainty(self):
        from dataclasses import replace
        from backend.waveforms.contracts import CaptureKey
        from backend.waveforms.evidence_contracts import sync_proof
        body,manifests=self.sync_case(); original=next(iter(manifests.values()))
        key=CaptureKey(original.key.gateway_id,original.key.device_id,uuid4())
        second=replace(original,key=key)
        endpoint=deepcopy(body['endpoints'][0]); endpoint['endpoint']['capture']['capture_id']=str(key.capture_id)
        body['endpoints'].append(endpoint); manifests[key]=second
        self.assertEqual(sync_proof(body,manifests,approved=True)['status'],'resolved')
        manifests[key]=replace(second,body=dict(second.body,utc_uncertainty_ns=None))
        self.assertEqual(sync_proof(body,manifests,approved=True)['status'],'pending')
        manifests[key]=replace(second,body=dict(second.body,utc_start='2026-09-30T00:00:00.000000Z'))
        self.assertEqual(sync_proof(body,manifests,approved=True)['status'],'ineligible')

    def test_endpoint_stream_and_vendor_label_are_hashed(self):
        from backend.waveforms.evidence_contracts import evidence_content
        body = sampling_body()
        canonical, digest = evidence_content('sampling_config', body)
        self.assertEqual(sha256(canonical).hexdigest(), digest)
        body['endpoint']['stream_id'] = 'other'
        self.assertNotEqual(evidence_content('sampling_config', body)[1], digest)
        body['reported_config_id'] = None
        self.assertIn(b'"reported_config_id":null', evidence_content('sampling_config', body)[0])
        body['external_url'] = 'https://example.invalid/evidence'
        with self.assertRaises(ContractError): evidence_content('sampling_config', body)

    def test_exact_decimal_rejects_coercion(self):
        from backend.waveforms.evidence_contracts import decimal_value
        self.assertEqual(decimal_value('-0.125'), Fraction(-1, 8))
        self.assertEqual(decimal_value('999999999999999999.000000000000000001'),
                         Fraction(999999999999999999000000000000000001, 10**18))
        for value in (True, 0.1, 'NaN', '1e2', '-0', '01', '1.0', '1\\2',
                      '1000000000000000000', '0.0000000000000000001'):
            with self.subTest(value=value), self.assertRaises(ContractError): decimal_value(value)

    def test_command_digest_literal_and_actor_identity(self):
        from backend.waveforms.commands import evidence_request_hash
        command = Command(Actor('local', '1', 'One', 'admin'), uuid4(), 1,
                          'synthetic test', '0'*64)
        target = UUID('00000000-0000-0000-0000-000000000009')
        expected = 'f48624d945899a7eaadc6a82d9edbfbff25db2e48014db079f68b99513f25148'
        self.assertEqual(evidence_request_hash('review', target, command, decision='approve'), expected)
        changed = Command(Actor('local', '1', 'Renamed', 'operator'), uuid4(), 1,
                          'synthetic test', '0'*64)
        self.assertEqual(evidence_request_hash('review', target, changed, decision='approve'), expected)
        self.assertNotEqual(evidence_request_hash('review', target, command, decision='reject'), expected)

    def test_calibration_hash_binds_exact_config_and_unknowns(self):
        from backend.waveforms.evidence_contracts import evidence_content
        body = dict(source_kind='synthetic', valid_from=None, valid_to=None,
            attachment_ids=[], notes=None, endpoint=sampling_body()['endpoint'],
            sampling_config_version_id=str(uuid4()), sampling_config_sha256='a'*64,
            scale='1', offset='-2', unit='kN', calibrated_range=None, zero=None,
            direction=None, measurement_uncertainty=None, time_correction_ns=None,
            residual_ns=None, method=None, basis=None, calibrated_at=None)
        original = evidence_content('calibration', body)[1]
        body['time_correction_ns'] = '0'
        self.assertNotEqual(evidence_content('calibration', body)[1], original)
        body['scale'] = '0'
        with self.assertRaises(ContractError): evidence_content('calibration', body)

    def test_calibration_content_matches_independent_literal_and_digest(self):
        from backend.waveforms.evidence_contracts import evidence_content
        body=dict(unit='kN',scale='1.25',offset='-2.5',source_kind='synthetic',
            endpoint=dict(device_id='D',gateway_id='OTHER',stream_id='s',channel_id='c'),
            sampling_config_version_id='00000000-0000-0000-0000-000000000001',sampling_config_sha256='a'*64,
            attachment_ids=[],notes=None,valid_from=None,valid_to=None,calibrated_range=None,zero=None,
            direction=None,measurement_uncertainty=None,time_correction_ns=None,residual_ns=None,
            method=None,basis=None,calibrated_at=None)
        expected=b'{"body":{"attachment_ids":[],"basis":null,"calibrated_at":null,"calibrated_range":null,"direction":null,"endpoint":{"channel_id":"c","device_id":"D","gateway_id":"OTHER","stream_id":"s"},"measurement_uncertainty":null,"method":null,"notes":null,"offset":"-2.5","residual_ns":null,"sampling_config_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sampling_config_version_id":"00000000-0000-0000-0000-000000000001","scale":"1.25","source_kind":"synthetic","time_correction_ns":null,"unit":"kN","valid_from":null,"valid_to":null,"zero":null},"kind":"calibration","protocol":"waveform.evidence-content/1"}'
        content,digest=evidence_content('calibration',body)
        self.assertEqual(content,expected)
        self.assertEqual(digest,'87871b6d56bc0ded1de39f82ec59f9b9f9d90a566cbcb3dbc91a5e27c7c90927')

    def test_attachment_signatures_size_and_external_content(self):
        from backend.waveforms.evidence_contracts import validate_attachment
        validate_attachment(b'%PDF-1.7\nsynthetic', 'application/pdf')
        validate_attachment(b'\x89PNG\r\n\x1a\nsynthetic', 'image/png')
        validate_attachment(b'\xff\xd8\xff\xe0synthetic\xff\xd9', 'image/jpeg')
        for data, mime in ((b'<svg/>', 'image/png'), (b'%PDF-1.7', 'text/html'),
                           (b'https://example.invalid/a.pdf', 'application/pdf'),
                           (b'%PDF-'+b'x'*(4*1024*1024), 'application/pdf')):
            with self.subTest(mime=mime), self.assertRaises(ContractError): validate_attachment(data, mime)


class EvidenceDatabaseTests(WaveformDatabaseCase):
    def service(self,limits=LIMITS):
        from backend.waveforms.evidence import EvidenceService
        return EvidenceService(lambda:self.connect('operator'), limits=limits,
            shared_accounting_bucket='GW', read_connect=lambda:self.connect('reader'))

    def command(self, operation, target=None, *, actor=None, version=0, key=None, **body):
        from backend.waveforms.commands import evidence_request_hash
        from dataclasses import replace
        cmd=Command(actor or Actor('local','creator','Creator','admin'),key or uuid4(),version,'synthetic test','0'*64)
        return replace(cmd,request_hash=evidence_request_hash(operation,target,cmd,**body))

    def create(self):
        body=sampling_body()
        return self.service().create('sampling_config',body,self.command('create',kind='sampling_config',body=body))

    def test_review_not_creator_even_admin(self):
        from backend.waveforms.errors import WaveformError
        item=self.create()
        cmd=self.command('review',item.id,version=1,decision='approve')
        with self.assertRaises(WaveformError) as caught: self.service().review(item.id,decision='approve',command=cmd)
        self.assertEqual(caught.exception.code,'waveform.evidence_self_review')

    def test_preliminary_guard_bounds_fresh_and_persisted_replay(self):
        from concurrent.futures import ThreadPoolExecutor, TimeoutError
        import time
        import psycopg
        from backend.waveforms import business
        from backend.waveforms.evidence import EvidenceService
        body=sampling_body(); replay=self.command('create',kind='sampling_config',body=body)
        self.service().create('sampling_config',body,replay)
        before=self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner')
        for persisted in (False,True):
            for lock_ms,statement_ms in ((0,0),(150,500),(1500,100)):
                with self.subTest(persisted=persisted,lock_ms=lock_ms,statement_ms=statement_ms):
                    opened=[]
                    def connect():
                        conn=self.connect('operator'); opened.append(conn)
                        conn.execute("SELECT set_config('lock_timeout',%s,false),set_config('statement_timeout',%s,false)",(str(lock_ms),str(statement_ms)))
                        conn.commit(); return conn
                    service=EvidenceService(connect,limits=LIMITS,shared_accounting_bucket='GW')
                    command=replay if persisted else self.command('create',kind='sampling_config',body=body)
                    def blocked():
                        started=time.monotonic()
                        try: return service.create('sampling_config',body,command),time.monotonic()-started
                        except psycopg.Error as error: return error,time.monotonic()-started
                    with self.connect('operator') as guard:
                        business.lock_dependencies(guard.cursor())
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            future=pool.submit(blocked)
                            try:
                                result,elapsed=future.result(timeout=4.5)
                            except TimeoutError:
                                for connection in opened: connection.cancel()
                                result,elapsed=future.result(timeout=3)
                            finally:
                                guard.rollback()
                    expected=psycopg.errors.QueryCanceled if statement_ms==100 else psycopg.errors.LockNotAvailable
                    self.assertIsInstance(result,expected)
                    self.assertLess(elapsed,4 if lock_ms==0 else .9)
                    self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.evidence_versions'),1)
                    self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner'),before)

    def test_reversed_source_audits_do_not_hold_cross_gateway_quota_locks(self):
        from concurrent.futures import ThreadPoolExecutor
        from dataclasses import replace
        import threading
        from backend.waveforms.evidence import EvidenceService
        from backend.waveforms.repository import WaveformRepository
        from backend.waveforms.wire import parse_frame
        from tests.test_waveform_repository import capture,observation
        limits=replace(LIMITS,gateways={'GW':LIMITS.gateways['GW'],'B':LIMITS.gateways['GW']})
        with self.capacity_fixture(limits):
            rows=[]
            for gateway in ('GW','B'):
                raw,_=capture(gateway); frame=parse_frame(raw); stream=frame.streams[0]
                WaveformRepository(self.connect,limits=limits,prefix='oil',ingress_gateway_id=gateway).ingest(observation(raw,gateway=gateway))
                endpoint=dict(capture=dict(gateway_id=gateway,device_id=frame.key.device_id,capture_id=str(frame.key.capture_id)),
                    source=dict(stream.source),stream_id=stream.stream_id,clock_domain_id=stream.clock_domain_id)
                rows.append(dict(endpoint=endpoint,reference_sample_index=str(stream.first_sample_index),reference_event='synthetic same edge',
                    nominal_reference_ns=str(stream.offset_ns),rate=dict(num=stream.rate_num,den=stream.rate_den),
                    valid_start_ns=str(stream.offset_ns),valid_end_ns=str(stream.offset_ns+1000000000),
                    offset_error_ns='0',frequency_error_ppm='0',residual_ns='0',clock_divisor=None))
            barrier=threading.Barrier(2); local=threading.local(); case=self
            class Cursor:
                def __init__(self,inner): self.inner=inner
                def __getattr__(self,key): return getattr(self.inner,key)
                def execute(self,statement,params=None):
                    result=self.inner.execute(statement,params)
                    if statement.startswith('SELECT waveforms.audited_raw'):
                        local.audits=getattr(local,'audits',0)+1
                        if local.audits==1: barrier.wait(timeout=5)
                    return result
            class Connection:
                def __init__(self,inner): self.inner=inner
                def __getattr__(self,key): return getattr(self.inner,key)
                def __enter__(self): self.inner.__enter__(); return self
                def __exit__(self,*args): return self.inner.__exit__(*args)
                def cursor(self): return Cursor(self.inner.cursor())
            def create(order):
                body=dict(source_kind='synthetic',valid_from=None,valid_to=None,attachment_ids=[],notes='Synthetic overlap test',
                    method='verified_linear_mapping',reference=rows[0]['endpoint'],endpoints=[rows[index] for index in order])
                service=EvidenceService(lambda:case.connect('operator'),limits=limits,shared_accounting_bucket='GW',
                    read_connect=lambda:Connection(case.connect('reader')))
                try:
                    return service.create('sync',body,self.command('create',kind='sync',body=body))
                except Exception as error: return error
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(create,order) for order in ((0,1),(1,0))]
                results=[future.result(timeout=12) for future in futures]
            self.assertFalse([type(value).__name__+': '+str(value) for value in results if isinstance(value,Exception)])
            self.assertEqual([[row['endpoint']['capture']['gateway_id'] for row in value.body['endpoints']] for value in results],[['GW','B'],['B','GW']])
            self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.audit_metadata'),4)
            self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.evidence_versions'),2)

    def test_revoke_appends_not_rewrites(self):
        item=self.create()
        before=self.scalar('SELECT canonical_content FROM waveforms.evidence_versions WHERE evidence_id=%s',(item.id,),role='migration_owner')
        reviewer=Actor('local','reviewer','Reviewer','admin')
        approved=self.service().review(item.id,decision='approve',command=self.command('review',item.id,actor=reviewer,version=1,decision='approve'))
        self.assertEqual(approved.review_state,'approved')
        restriction=self.service().revoke(item.id,self.command('revoke',item.id,version=2))
        self.assertEqual(restriction.reason_code,'evidence_revoked')
        self.assertEqual(before,self.scalar('SELECT canonical_content FROM waveforms.evidence_versions WHERE evidence_id=%s',(item.id,),role='migration_owner'))
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.evidence_events WHERE evidence_id=%s',(item.id,),role='migration_owner'),3)

    def test_same_key_retry_is_not_double_charged(self):
        body=sampling_body(); cmd=self.command('create',kind='sampling_config',body=body)
        first=self.service().create('sampling_config',body,cmd)
        before=self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner')
        second=self.service().create('sampling_config',body,cmd)
        self.assertEqual(first,second)
        self.assertEqual(before,self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner'))
        self.assertEqual(first.accounting_bucket,'GW')
        self.assertEqual(first.body['endpoint']['gateway_id'],'OTHER')

    def test_field_profile_incomplete_cannot_be_approved(self):
        from backend.waveforms.errors import WaveformError
        body=dict(source_kind='synthetic',valid_from=None,valid_to=None,attachment_ids=[],notes='Synthetic profile validation only',
            purpose='field_pair_input',target_equipment_type=None,electrical_input=None,
            required_quantities=[dict(role='load',quantity='load',unit='kN',signal_kind='instantaneous',measurement_source='sensor')],
            min_rates=[dict(role='load',rate=None)],ranges=[dict(role='load',range=None)],max_sync_error_ns=None,required_evidence=[],trial_basis=None)
        item=self.service().create('profile',body,self.command('create',kind='profile',body=body))
        with self.assertRaises(WaveformError):
            self.service().review(item.id,decision='approve',command=self.command('review',item.id,version=1,decision='approve',actor=Actor('local','other','Other','admin')))

    def origin(self,raw,observation_id,transport,source_ref,*,import_id=None):
        import json
        manifest=json.loads(raw)
        with self.connect('reader') as conn:
            row=conn.execute('SELECT fact_id,outcome_id FROM waveforms.provenance_metadata WHERE observation_id=%s',(observation_id,)).fetchone()
        body=dict(source_kind='synthetic',valid_from=manifest['utc_start'],valid_to=None,attachment_ids=[],notes='Synthetic reviewed source',
            capture={k:manifest[k] for k in ('gateway_id','device_id','capture_id')},source_identity=manifest['streams'][0]['source'],
            provenance=dict(transport=transport,source_ref=str(source_ref),observation_id=str(observation_id),
                            outcome_id=str(row[1]),fact_id=str(row[0]),manifest_sha256=sha256(raw).hexdigest(),import_id=str(import_id) if import_id else None),
            verification_method='Synthetic fixture',verification_basis='Explicit controlled synthetic provenance',
            credential_attachment_ids=[],broker_config_ref='synthetic-ACL' if transport=='mqtt' else None)
        item=self.service().create('origin',body,self.command('create',kind='origin',body=body))
        return self.service().review(item.id,decision='approve',command=self.command('review',item.id,version=1,decision='approve',actor=Actor('local','other','Other','admin')))

    def test_pending_offline_origin_does_not_override_accepted_online(self):
        import json
        from dataclasses import replace
        from backend.waveforms.imports import ImportService
        from backend.waveforms.commands import import_request_hash
        from backend.waveforms.repository import WaveformRepository
        from tests.test_waveform_repository import capture,observation
        raw,chunk=capture(); body=json.loads(raw); file_bytes=raw+b'\n'+chunk
        imports=ImportService(self.connect,limits=LIMITS,prefix='oil')
        def command(op,target=None,version=0,**kw):
            value=Command(Actor('local','uploader','Uploader','operator'),uuid4(),version,'synthetic test','0'*64)
            return replace(value,request_hash=import_request_hash(op,target,value,**kw))
        meta=dict(file_sha256=sha256(file_bytes).hexdigest(),bytes=len(file_bytes),line_count=2,final_lf=False,
                  gateway_id='GW',device_id=body['device_id'],capture_id=body['capture_id'])
        session=imports.create(meta,command('create',metadata=meta))
        progress=imports.append(session.import_id,0,raw,command('append',session.import_id,session.version,line=0,payload=raw))
        offline_id=self.scalar('SELECT observation_id FROM waveforms.provenance_metadata WHERE source_ref=%s',(session.import_id,))
        offline=self.origin(raw,offline_id,'file',session.import_id,import_id=session.import_id)
        self.assertEqual(self.service().inspect(offline.id,actor=offline.creator)['status'],'pending')
        observation_online=observation(raw)
        WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW').ingest(observation_online)
        online_id=self.scalar('SELECT observation_id FROM waveforms.provenance_metadata WHERE source_ref=%s',(observation_online.source_ref,))
        online=self.origin(raw,online_id,'mqtt',observation_online.source_ref)
        WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW').ingest(observation(chunk))
        from backend.waveforms.assembly import Assembler
        from tests.waveform_support import FIXED_TIME
        assembler=Assembler(lambda:self.connect('assembler'),clock=lambda:FIXED_TIME,limits=LIMITS)
        lease=assembler.claim(uuid4())
        assembler.run(lease)
        self.assertEqual(self.scalar('SELECT state FROM waveforms.capture_metadata'),'complete')
        self.assertEqual(self.service().inspect(online.id,actor=online.creator)['status'],'resolved')
        self.assertEqual(self.service().inspect(offline.id,actor=offline.creator)['status'],'pending')
        cancelled=imports.create(meta,command('create',metadata=meta))
        cancelled_progress=imports.append(cancelled.import_id,0,raw,command('append',cancelled.import_id,cancelled.version,line=0,payload=raw))
        cancelled_observation=self.scalar('SELECT observation_id FROM waveforms.provenance_metadata WHERE source_ref=%s',(cancelled.import_id,))
        cancelled_origin=self.origin(raw,cancelled_observation,'file',cancelled.import_id,import_id=cancelled.import_id)
        imports.cancel(cancelled.import_id,command('cancel',cancelled.import_id,cancelled_progress.version))
        self.assertEqual(self.service().inspect(cancelled_origin.id,actor=cancelled_origin.creator)['status'],'pending')
        progress=imports.append(session.import_id,1,chunk,command('append',session.import_id,progress.version,line=1,payload=chunk))
        imports.seal(session.import_id,command('seal',session.import_id,progress.version))
        self.assertEqual(self.service().inspect(offline.id,actor=offline.creator)['status'],'resolved')

    def test_calibration_requires_exact_config_digest_and_endpoint(self):
        from backend.waveforms.errors import WaveformError
        config=self.create()
        body=dict(source_kind='synthetic',valid_from=None,valid_to=None,attachment_ids=[],notes=None,
            endpoint=sampling_body()['endpoint'],sampling_config_version_id=str(config.id),
            sampling_config_sha256=config.content_sha256,scale='1',offset='0',unit='kN',
            calibrated_range=None,zero=None,direction=None,measurement_uncertainty=None,
            time_correction_ns=None,residual_ns=None,method=None,basis=None,calibrated_at=None)
        for change in ({'sampling_config_sha256':'a'*64}, {'endpoint':dict(body['endpoint'],stream_id='different')}):
            invalid=body|change
            with self.assertRaises(WaveformError):
                self.service().create('calibration',invalid,self.command('create',kind='calibration',body=invalid))
        item=self.service().create('calibration',body,self.command('create',kind='calibration',body=body))
        self.assertEqual(item.body['time_correction_ns'],None)
        self.assertEqual(item.body['sampling_config_sha256'],config.content_sha256)
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.evidence_versions WHERE kind='calibration'"),1)

    def test_sync_uses_controlled_manifest_and_replay_does_not_reaudit(self):
        from backend.waveforms.repository import WaveformRepository
        from backend.waveforms.wire import parse_frame
        from tests.test_waveform_repository import capture,observation
        raw,_=capture(); frame=parse_frame(raw); stream=frame.streams[0]
        WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW').ingest(observation(raw))
        payload=b'%PDF-synthetic timing correspondence'
        meta=dict(media_type='application/pdf',payload_bytes=len(payload),payload_sha256=sha256(payload).hexdigest())
        attachment=self.service().upload_attachment(payload,media_type='application/pdf',command=self.command('upload',attachment=meta))
        endpoint=dict(capture=dict(gateway_id=frame.key.gateway_id,device_id=frame.key.device_id,capture_id=str(frame.key.capture_id)),
            source=dict(stream.source),stream_id=stream.stream_id,clock_domain_id=stream.clock_domain_id)
        body=dict(source_kind='synthetic',valid_from=None,valid_to=None,attachment_ids=[str(attachment.id)],
            notes='Synthetic timing report only',method='verified_linear_mapping',reference=endpoint,endpoints=[dict(
            endpoint=endpoint,reference_sample_index=str(stream.first_sample_index),reference_event='synthetic edge',
            nominal_reference_ns=str(stream.offset_ns),rate=dict(num=stream.rate_num,den=stream.rate_den),
            valid_start_ns=str(stream.offset_ns),valid_end_ns=str(stream.offset_ns+1000000000),
            offset_error_ns='0',frequency_error_ppm='0',residual_ns='0',clock_divisor=None)])
        cmd=self.command('create',kind='sync',body=body)
        item=self.service().create('sync',body,cmd)
        count=self.scalar('SELECT count(*) FROM waveforms.audit_metadata')
        self.assertEqual(self.service().create('sync',body,cmd),item)
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.audit_metadata'),count)
        self.assertEqual(self.service().inspect(item.id,actor=item.creator)['status'],'pending')
        self.service().review(item.id,decision='approve',command=self.command('review',item.id,version=1,decision='approve',actor=Actor('local','reviewer','R','admin')))
        result=self.service().inspect(item.id,actor=item.creator)
        self.assertEqual(result['status'],'resolved')
        self.assertEqual(result['endpoints'][0]['mapping_role'],'reference')
        self.assertEqual(result['endpoints'][0]['manifest_sha256'],sha256(raw).hexdigest())

    def test_attachment_download_returns_no_bytes_when_audit_commit_fails(self):
        import psycopg
        from backend.waveforms.evidence import EvidenceService
        payload=b'%PDF-synthetic'
        meta=dict(media_type='application/pdf',payload_bytes=len(payload),payload_sha256=sha256(payload).hexdigest())
        attachment=self.service().upload_attachment(payload,media_type='application/pdf',command=self.command('upload',attachment=meta))
        class FailedCommit:
            def __init__(self,connection): self.connection=connection
            def __getattr__(self,key): return getattr(self.connection,key)
            def __enter__(self): self.connection.__enter__(); return self
            def __exit__(self,*args): return self.connection.__exit__(*args)
            def commit(self): raise psycopg.OperationalError('synthetic commit unavailable')
        service=EvidenceService(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',
            read_connect=lambda:FailedCommit(self.connect('reader')))
        with self.assertRaises(psycopg.OperationalError):
            service.download_attachment(attachment.id,actor=Actor('local','reader','Reader','viewer'))
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.business_audit WHERE action='attachment.read'"),0)

    def test_attachment_download_audit_and_immutable_approved_basis(self):
        import psycopg
        payload=b'%PDF-1.7 synthetic attachment'
        meta=dict(media_type='application/pdf',payload_bytes=len(payload),payload_sha256=sha256(payload).hexdigest())
        cmd=self.command('upload',attachment=meta)
        attachment=self.service().upload_attachment(payload,media_type='application/pdf',command=cmd)
        self.assertEqual(self.service().upload_attachment(payload,media_type='application/pdf',command=cmd),attachment)
        self.assertEqual(self.scalar("SELECT used_attachment_bytes FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner'),len(payload))
        body=sampling_body(); body['attachment_ids']=[str(attachment.id)]
        item=self.service().create('sampling_config',body,self.command('create',kind='sampling_config',body=body))
        self.service().review(item.id,decision='approve',command=self.command('review',item.id,version=1,decision='approve',actor=Actor('local','r','R','admin')))
        download=self.service().download_attachment(attachment.id,actor=item.creator)
        self.assertEqual(download['payload'],payload)
        self.assertTrue(download['content_disposition'].startswith('attachment;'))
        self.assertEqual(self.scalar("SELECT count(*) FROM waveforms.business_audit WHERE action='attachment.read'",role='reader'),1)
        for table,field,idfield,id in (('attachments','payload','attachment_id',attachment.id),('evidence_versions','body','evidence_id',item.id)):
            with self.assertRaises(psycopg.Error):
                with self.connect('migration_owner') as conn: conn.execute(f'UPDATE waveforms.{table} SET {field}={field} WHERE {idfield}=%s',(id,))
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.connect('reader') as conn: conn.execute('SELECT payload FROM waveforms.attachments')

    def test_attachment_quota_boundary_rolls_back_all_business_rows(self):
        from dataclasses import replace
        from backend.waveforms.config import CapacityLimits
        from backend.waveforms.errors import WaveformError
        payload=b'%PDF-x'
        limits=CapacityLimits(LIMITS.global_quota,{'GW':replace(LIMITS.gateways['GW'],attachment_bytes=len(payload))})
        with self.capacity_fixture(limits):
            service=self.service(limits)
            meta=dict(media_type='application/pdf',payload_bytes=len(payload),payload_sha256=sha256(payload).hexdigest())
            service.upload_attachment(payload,media_type='application/pdf',command=self.command('upload',attachment=meta))
            before=self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner')
            with self.assertRaises(WaveformError): service.upload_attachment(payload,media_type='application/pdf',command=self.command('upload',attachment=meta))
            self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.attachment_metadata'),1)
            self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.business_receipts',role='operator'),1)
            self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner'),before)
            self.assertEqual(self.scalar("SELECT used_metadata_rows+used_attachment_bytes FROM waveforms.capacity WHERE bucket='*'",role='migration_owner'),0)

    def test_metadata_quota_charges_four_actual_create_rows_atomically(self):
        from dataclasses import replace
        from backend.waveforms.config import CapacityLimits
        from backend.waveforms.errors import WaveformError
        limits=CapacityLimits(LIMITS.global_quota,{'GW':replace(LIMITS.gateways['GW'],metadata_rows=4)})
        with self.capacity_fixture(limits):
            body=sampling_body(); service=self.service(limits)
            service.create('sampling_config',body,self.command('create',kind='sampling_config',body=body))
            with self.assertRaises(WaveformError):
                service.create('sampling_config',body,self.command('create',kind='sampling_config',body=body))
            self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='GW'",role='migration_owner'),4)
            self.assertEqual(self.scalar("SELECT used_metadata_rows FROM waveforms.capacity WHERE bucket='*'",role='migration_owner'),0)
            self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.business_receipts',role='operator'),1)

    def test_future_storage_preserves_missing_dependencies_and_raw_archive_unknown_registry(self):
        from dataclasses import asdict
        import psycopg
        from psycopg.types.json import Jsonb
        from backend.waveforms import business
        from backend.waveforms.capacity import prepare_transaction
        capture=self.insert_capture()
        body=dict(source_kind='synthetic',valid_from=None,valid_to=None,attachment_ids=[],notes=None,
            purpose='raw_archive',target_equipment_type=None,electrical_input=None,required_quantities=[],
            min_rates=[],ranges=[],max_sync_error_ns=None,required_evidence=[],trial_basis=None)
        profile=self.service().create('profile',body,self.command('create',kind='profile',body=body))
        pending,eligible=uuid4(),uuid4()
        with self.connect('operator') as conn:
            cur=conn.cursor(); prepare_transaction(cur); business.lock_dependencies(cur); business.lock_payer(cur,'GW',LIMITS)
            for id,status,cap,digest,pid,phash in ((pending,'pending',None,None,None,None),
                    (eligible,'eligible',capture['capture_pk'],capture['manifest_sha256'],profile.id,profile.content_sha256)):
                cur.execute("INSERT INTO waveforms.quality_runs(quality_run_id,accounting_bucket,capture_pk,source_key,manifest_sha256,validator_version,profile_id,profile_sha256,purpose,status,parameters,evidence,registry_snapshot,result,creator) VALUES(%s,'GW',%s,%s,%s,'synthetic/1',%s,%s,'raw_archive',%s,'{}','[]',NULL,'{}',%s)",
                    (id,cap,Jsonb(dict(gateway_id='GW',device_id='D',capture_id=str(capture['capture_id']))),digest,pid,phash,status,Jsonb(asdict(profile.creator))))
            prepare_transaction(cur)
        self.assertIsNone(self.scalar('SELECT manifest_sha256 FROM waveforms.quality_runs WHERE quality_run_id=%s',(pending,)))
        self.assertIsNone(self.scalar('SELECT registry_snapshot FROM waveforms.quality_runs WHERE quality_run_id=%s',(eligible,)))
        with self.assertRaises(psycopg.Error):
            with self.connect('migration_owner') as conn:
                conn.execute("UPDATE waveforms.quality_runs SET status='eligible' WHERE quality_run_id=%s",(pending,))

    def test_parallel_review_and_revoke_have_single_version_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from backend.waveforms.errors import WaveformError
        item=self.create(); reviewer=Actor('local','reviewer','R','admin')
        def operation(name):
            try:
                if name=='review': return self.service().review(item.id,decision='approve',command=self.command('review',item.id,version=1,actor=reviewer,decision='approve'))
                return self.service().revoke(item.id,self.command('revoke',item.id,version=1))
            except WaveformError as error: return error
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(operation,('review','revoke')))
        self.assertEqual(sum(isinstance(r,WaveformError) for r in results),1)
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.evidence_events WHERE evidence_id=%s',(item.id,),role='operator'),2)

    def test_configured_payer_cannot_change_with_actor_or_session(self):
        from backend.waveforms.evidence import EvidenceService
        from backend.waveforms.config import CapacityLimits
        from backend.waveforms.errors import WaveformError
        limits=CapacityLimits(LIMITS.global_quota,{'GW':LIMITS.gateways['GW'],'OTHER':LIMITS.gateways['GW']})
        body=sampling_body()
        for actor in (Actor('local','creator','C','admin'),Actor('external','new','N','admin')):
            service=EvidenceService(lambda:self.connect('operator'),limits=limits,shared_accounting_bucket='OTHER')
            with self.assertRaises(WaveformError) as caught:
                service.create('sampling_config',body,self.command('create',kind='sampling_config',body=body,actor=actor))
            self.assertEqual(caught.exception.code,'waveform.business_payer_mismatch')
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.evidence_versions'),0)

    def test_commit_failure_does_not_return_success_and_replay_recovers(self):
        import psycopg
        from backend.waveforms.evidence import EvidenceService
        from backend.waveforms.errors import WaveformError
        case=self
        class CommitLost:
            def __init__(self,connection,after): self.connection,self.after=connection,after
            def __getattr__(self,key): return getattr(self.connection,key)
            def __enter__(self): self.connection.__enter__(); return self
            def __exit__(self,*args): return self.connection.__exit__(*args)
            def commit(self):
                if self.after: self.connection.commit()
                raise psycopg.OperationalError('synthetic lost commit response')
        for after in (False,True):
            body=sampling_body(); cmd=self.command('create',kind='sampling_config',body=body)
            service=EvidenceService(lambda:CommitLost(case.connect('operator'),after),limits=LIMITS,shared_accounting_bucket='GW')
            with self.assertRaises(WaveformError): service.create('sampling_config',body,cmd)
            count=self.scalar('SELECT count(*) FROM waveforms.business_receipts WHERE idempotency_key=%s',(cmd.key,),role='operator')
            self.assertEqual(count,1 if after else 0)
            recovered=self.service().create('sampling_config',body,cmd)
            self.assertEqual(recovered.version,1)
