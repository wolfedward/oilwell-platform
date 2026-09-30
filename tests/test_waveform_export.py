"""Exact LF export bytes and bound request contexts."""
import unittest
from hashlib import sha256
from uuid import UUID,uuid4
from dataclasses import replace
from tests.test_waveform_datasets import feature
from tests.waveform_dataset_support import DatasetCase

ORACLE=(b'{"boot_id":"4fec5d97-8ad6-4194-b9bd-21e57af2d40c","capture_id":"0acfbc0b-64ab-4226-94a0-c3f2de5feabc","capture_kind":"event","clock_domain_id":"clock","cycle":null,"device_id":"D0","gateway_id":"GW","kind":"manifest","producer":{"manufacturer":"Synthetic fixture","recorder_version":"1"},"schema":"OWP-WAVE/1.0","source_evidence":null,"source_kind":"synthetic","streams":[{"channels":[{"calibration_id":"a9b45173-741d-4eca-88c9-4105d4fb4e14","channel_id":"total_active_power","config_id":"vendor-label","encoding":"i32le","measurement_source":"synthetic","quantity":"total_active_power","signal_kind":"instantaneous","unit":"W"}],"chunk_count":1,"chunk_frames":3,"clock_domain_id":"clock","data_sha256":"6fe4664d6b7a211081c354e2d677e39842b49c9b14fd8dc69a978d0078922d93","decoded_bytes":15,"first_sample_index":"0","frame_bytes":5,"frame_count":3,"offset_ns":"0","rate_den":1,"rate_num":1,"source":{"boot_id":"4fec5d97-8ad6-4194-b9bd-21e57af2d40c","device_id":"D0","gateway_id":"GW"},"stream_id":"electrical"}],"sync_evidence_id":null,"utc_start":"2026-09-29T00:00:00.000000Z","utc_uncertainty_ns":"0","window_ns":"2000000000"}\n'
        b'{"capture_id":"0acfbc0b-64ab-4226-94a0-c3f2de5feabc","chunk_index":0,"data_base64":"/f///wAAAAAAAAMAAAAA","data_sha256":"6fe4664d6b7a211081c354e2d677e39842b49c9b14fd8dc69a978d0078922d93","device_id":"D0","gateway_id":"GW","kind":"chunk","manifest_sha256":"db894d56304d675335e7a84a098682b1f54442ff3de2301bf8c795c3b0dd7e83","sample_count":3,"sample_start":0,"schema":"OWP-WAVE/1.0","stream_id":"electrical"}\n')


class ExportContractTests(unittest.TestCase):
    def test_export_lf_and_order_stable(self):
        exporter=feature(self,'export')
        lines=(b'{"kind":"manifest"}',b'{"kind":"chunk","chunk_index":0}')
        expected=b'{"kind":"manifest"}\n{"kind":"chunk","chunk_index":0}\n'
        self.assertEqual(b''.join(exporter.lf_lines(lines)),expected)

    def test_valid_literal_export_oracle(self):
        from backend.waveforms.wire import parse_frame
        from backend.waveforms.export import lf_lines
        lines=ORACLE.splitlines(); [parse_frame(line) for line in lines]
        self.assertEqual(len(ORACLE),1533)
        self.assertEqual(sha256(b''.join(lf_lines(lines))).hexdigest(),'b20765e2733fddf57e1da9e3d3de127c48afe8c8b771f948db04c4c1c1b9e5fd')
        self.assertEqual(sha256(ORACLE[:-1]).hexdigest(),'3a84507fcca5d8674a196bcab85cc515d18ad9f8bafb2b78600c2b6f034792d6')


class ExportDatabaseTests(DatasetCase):
    def test_literal_oracle_through_audited_real_archive_export(self):
        from backend.waveforms.commands import Actor,Command,quality_request_hash
        from backend.waveforms.repository import WaveformRepository
        from backend.waveforms.assembly import Assembler
        from backend.waveforms.quality_service import QualityService
        from backend.waveforms.wire import parse_frame
        from tests.test_waveform_repository import observation
        from tests.waveform_support import LIMITS,FIXED_TIME
        lines=ORACLE.splitlines(); frame=parse_frame(lines[0]); repo=WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW')
        for raw in lines: self.assertEqual(repo.ingest(observation(raw,topic='oil/v2/GW/D0/waveform')).status,'stored')
        assembler=Assembler(lambda:self.connect('assembler'),clock=lambda:FIXED_TIME,limits=LIMITS)
        self.assertEqual(assembler.run(assembler.claim(uuid4())).status,'complete')
        actor=Actor('local','archive','Archive','operator'); reviewer=Actor('local','r','R','admin')
        key=dict(gateway_id='GW',device_id='D0',capture_id=str(frame.key.capture_id))
        selection=dict(purpose='raw_archive',profile=None,captures=[dict(key=key,manifest_sha256=frame.raw_sha256,origins=[])],channels=[],cycle=None,sync=None)
        cmd=Command(actor,uuid4(),0,'Literal archive certificate','0'*64)
        quality=QualityService(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        run=quality.record(frame.key,selection,replace(cmd,request_hash=quality_request_hash(frame.key,cmd,body=selection))).runs[0]
        capture=self.scalar('SELECT capture_pk FROM waveforms.quality_runs WHERE quality_run_id=%s',(run.id,)); service=self.dataset_service()
        body=dict(title='Fixed literal evidence',purpose='raw_archive',split_plan=dict(kind='none')); draft=service.create(body,self.command('create',body=body))
        body=dict(target_id=str(draft.id),changes=dict(upsert_members=[dict(kind='capture',reference_id=str(capture),quality_run_id=str(run.id),split='unassigned')]))
        draft=service.update(body,self.command('update',draft.id,draft.revision,body=body)); body=dict(target_id=str(draft.id))
        draft=service.submit(body,self.command('submit',draft.id,draft.revision,body=body))
        version=service.freeze(body,self.command('freeze',draft.id,draft.revision,reviewer,body=body))
        exporter=self.exporter(); body,cmd=self.export_command(version.id,'evidence',reviewer); record=exporter.create(body,cmd)
        output=b''.join(exporter.open(record.id,actor=reviewer).stream_capture(frame.key))
        self.assertEqual(output,ORACLE); self.assertEqual(len(output),1533)
        self.assertEqual(exporter.files(version.id,actor=reviewer)[0]['file_sha256'],'b20765e2733fddf57e1da9e3d3de127c48afe8c8b771f948db04c4c1c1b9e5fd')

    def test_selected_offline_original_digest_and_raw_archive_denial(self):
        from copy import deepcopy
        from backend.waveforms.commands import Actor,Command,import_request_hash,quality_request_hash,pair_request_hash
        from backend.waveforms.imports import ImportService
        from backend.waveforms.pairing import PairService
        from backend.waveforms.quality_service import QualityService
        from backend.waveforms.evidence_contracts import thaw
        from backend.waveforms.pair_contracts import capture_key
        from backend.waveforms.errors import WaveformError
        from tests.waveform_pairing_support import seed_pair,CREATOR,REVIEWER
        from tests.waveform_support import LIMITS
        vector=seed_pair(self,two=True); chosen=vector['selection']['captures'][0]; payloads=vector['wire_captures'][0]
        original=b'\n'.join(payloads); metadata=dict(chosen['key'],file_sha256=sha256(original).hexdigest(),bytes=len(original),line_count=len(payloads),final_lf=False)
        imports=ImportService(self.connect,limits=LIMITS,prefix='oil')
        def ic(op,target=None,version=0,**values):
            cmd=Command(CREATOR,uuid4(),version,'Synthetic dataset import','0'*64)
            return replace(cmd,request_hash=import_request_hash(op,target,cmd,**values))
        session=imports.create(metadata,ic('create',metadata=metadata))
        for i,payload in enumerate(payloads): session=imports.append(session.import_id,i,payload,ic('append',session.import_id,session.version,line=i,payload=payload))
        imports.seal(session.import_id,ic('seal',session.import_id,session.version))
        evidence=vector['evidence_service']; ec=vector['evidence_command']; origin_body=thaw(evidence.get(UUID(chosen['origins'][0]['id'])).body)
        with self.connect('reader') as conn:
            observation,fact,outcome=conn.execute("SELECT observation_id,fact_id,outcome_id FROM waveforms.provenance_metadata WHERE source_ref=%s AND kind='manifest'",(session.import_id,)).fetchone()
        origin_body['broker_config_ref']=None
        origin_body['provenance']=dict(transport='file',source_ref=str(session.import_id),import_id=str(session.import_id),observation_id=str(observation),fact_id=str(fact),outcome_id=str(outcome),manifest_sha256=chosen['manifest_sha256'])
        origin=evidence.create('origin',origin_body,ec('create',kind='origin',body=origin_body))
        origin=evidence.review(origin.id,decision='approve',command=ec('review',origin.id,1,REVIEWER,decision='approve'))
        selection=deepcopy(vector['selection']); selection['captures'][0]['origins']=[dict(id=str(origin.id),content_sha256=origin.content_sha256,version=str(origin.version))]
        options=dict(limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
        pairs=PairService(lambda:self.connect('operator'),**options); body=dict(selection=selection,method_version='linear_adjacent/1')
        cmd=Command(CREATOR,uuid4(),0,'Synthetic offline pair','0'*64); pair=pairs.propose(body,replace(cmd,request_hash=pair_request_hash('propose',None,cmd,body=body)))
        cmd=Command(REVIEWER,uuid4(),1,'Synthetic offline review','0'*64)
        pairs.review(pair.id,decision='approve',command=replace(cmd,request_hash=pair_request_hash('review',pair.id,cmd,decision='approve')))
        service,draft=self.submitted(pair); body=dict(target_id=str(draft.id)); reviewer=Actor('local','freeze','Freeze','admin')
        version=service.freeze(body,self.command('freeze',draft.id,draft.revision,reviewer,body=body)); exporter=self.exporter()
        descriptor=exporter.files(version.id,actor=reviewer)[0]
        self.assertEqual(dict(descriptor['source_files'][0]),dict(import_id=str(session.import_id),file_sha256=metadata['file_sha256'],bytes=len(original),line_count=len(payloads),final_lf=False))
        self.assertNotEqual(descriptor['file_sha256'],metadata['file_sha256'])
        body,cmd=self.export_command(version.id,'research',reviewer); record=exporter.create(body,cmd)
        self.assertEqual(b''.join(exporter.open(record.id,actor=reviewer).stream_capture(capture_key(chosen['key']))),original+b'\n')
        # Archive a complete capture with a deliberately non-qualifying certificate.
        selection=dict(purpose='raw_archive',profile=None,captures=[chosen],channels=[],cycle=None,sync=None)
        quality=QualityService(lambda:self.connect('operator'),**options); key=capture_key(chosen['key'])
        cmd=Command(CREATOR,uuid4(),0,'Synthetic raw archive','0'*64)
        run=quality.record(key,selection,replace(cmd,request_hash=quality_request_hash(key,cmd,body=selection))).runs[0]
        body=dict(title='Evidence only',purpose='raw_archive',split_plan=dict(kind='none')); draft=service.create(body,self.command('create',body=body))
        capture_id=self.scalar('SELECT capture_pk FROM waveforms.quality_runs WHERE quality_run_id=%s',(run.id,))
        body=dict(target_id=str(draft.id),changes=dict(upsert_members=[dict(kind='capture',reference_id=str(capture_id),quality_run_id=str(run.id),split='unassigned')]))
        draft=service.update(body,self.command('update',draft.id,draft.revision,body=body)); body=dict(target_id=str(draft.id))
        draft=service.submit(body,self.command('submit',draft.id,draft.revision,body=body))
        import json
        from backend.waveforms.repository import WaveformRepository
        from tests.test_waveform_repository import observation as mqtt_observation
        from tests.waveform_vectors import encoded
        conflicting=json.loads(payloads[0]); conflicting['producer']['recorder_version']='archive conflict one'
        WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW').ingest(
            mqtt_observation(encoded(conflicting),topic='oil/v2/GW/D0/waveform'))
        selected_origin=UUID(chosen['origins'][0]['id'])
        evidence.revoke(selected_origin,ec('revoke',selected_origin,2))
        archive=service.freeze(body,self.command('freeze',draft.id,draft.revision,reviewer,body=body))
        self.assertEqual(archive.purpose,'raw_archive'); self.assertEqual(archive.restriction_state,'restricted')
        snapshot=self.scalar('SELECT snapshot FROM waveforms.dataset_members WHERE version_id=%s',(archive.id,))
        actual=self.scalar('SELECT restriction_id FROM waveforms.capture_restrictions WHERE subject_id=%s',(capture_id,))
        self.assertTrue(any(isinstance(r,dict) and r.get('source_event_id')==str(actual) and r.get('subject_id')==str(capture_id) for r in snapshot['restrictions']))
        evidence_event=self.scalar('SELECT source_event_id FROM waveforms.restrictions WHERE evidence_id=%s',(selected_origin,))
        self.assertTrue(any(r['source_event_id']==str(evidence_event) and r['subject_id']==str(selected_origin) for r in snapshot['restrictions']))
        for intent in ('research','training'):
            body,cmd=self.export_command(archive.id,intent,reviewer)
            with self.assertRaises(WaveformError): exporter.create(body,cmd)
        body,cmd=self.export_command(archive.id,'evidence',reviewer); record=exporter.create(body,cmd)
        self.assertEqual(b''.join(exporter.open(record.id,actor=reviewer).stream_capture(key)),original+b'\n')
        body=dict(title='Second archival attempt',purpose='raw_archive',split_plan=dict(kind='none'))
        draft=service.create(body,self.command('create',body=body))
        body=dict(target_id=str(draft.id),changes=dict(upsert_members=[dict(kind='capture',reference_id=str(capture_id),quality_run_id=str(run.id),split='unassigned')]))
        draft=service.update(body,self.command('update',draft.id,draft.revision,body=body)); body=dict(target_id=str(draft.id))
        draft=service.submit(body,self.command('submit',draft.id,draft.revision,body=body)); prepare=service.prepare_dataset
        def second_conflict(*args):
            ready=prepare(*args); conflicting['producer']['recorder_version']='archive conflict two'
            WaveformRepository(self.connect,limits=LIMITS,prefix='oil',ingress_gateway_id='GW').ingest(
                mqtt_observation(encoded(conflicting),topic='oil/v2/GW/D0/waveform'))
            return ready
        service.prepare_dataset=second_conflict
        with self.assertRaises(WaveformError) as caught: service.freeze(body,self.command('freeze',draft.id,draft.revision,reviewer,body=body))
        self.assertEqual(caught.exception.code,'waveform.dataset_stale_preparation')
        self.assertEqual(self.scalar('SELECT count(*) FROM waveforms.dataset_versions WHERE dataset_id=%s',(draft.id,)),0)
        self.assertEqual(self.scalar('SELECT snapshot FROM waveforms.dataset_members WHERE version_id=%s',(archive.id,)),snapshot)

    def test_first_advance_audit_failures_close_and_membership(self):
        import psycopg
        from backend.waveforms.commands import Actor
        from backend.waveforms.contracts import CaptureKey
        from backend.waveforms.pair_contracts import capture_key
        from backend.waveforms.errors import WaveformError
        from backend.waveforms.export import ExportContext
        exporter=self.exporter(); _,version,vector=self.frozen(); actor=Actor('local','download','Download','admin')
        body,command=self.export_command(version.id,'research',actor); record=exporter.create(body,command)
        key=capture_key(vector['selection']['captures'][0]['key'])
        audits=lambda:self.scalar("SELECT count(*) FROM waveforms.business_audit WHERE action='exports.download'")
        raw_audits=lambda:self.scalar('SELECT count(*) FROM waveforms.audit_metadata')
        before=audits(); raw_before=raw_audits(); context=exporter.open(record.id,actor=actor)
        iterator=context.stream_capture(key)
        self.assertEqual(audits(),before); self.assertEqual(raw_audits(),raw_before)
        self.assertEqual(next(iterator),vector['wire_captures'][0][0])
        self.assertEqual(audits(),before+1); self.assertEqual(raw_audits(),raw_before+1)
        self.assertTrue(all(c.closed or c is self.guard for c in self.connections))
        iterator.close(); self.assertTrue(context._closed)
        with self.assertRaises(WaveformError): next(context.stream_capture(key))
        with self.assertRaises(WaveformError): ExportContext(exporter,record,actor,context.metadata)
        with self.assertRaises(WaveformError): next(exporter.open(record.id,actor=actor).stream_capture(CaptureKey(key.gateway_id,key.device_id,uuid4())))
        self.assertEqual(audits(),before+1)
        class FailedCommit:
            def __init__(self,conn,after): self.connection,self.after=conn,after
            def __getattr__(self,name): return getattr(self.connection,name)
            def __enter__(self): self.connection.__enter__(); return self
            def __exit__(self,*args): return self.connection.__exit__(*args)
            def commit(self):
                if self.after: self.connection.commit()
                raise psycopg.OperationalError('synthetic audit commit response failure')
        for after in (False,True):
            context=exporter.open(record.id,actor=actor); saved=exporter._connect; count=audits(); raw_count=raw_audits()
            exporter._connect=lambda:FailedCommit(self.connect('operator'),after)
            try:
                with self.assertRaises(WaveformError): next(context.stream_capture(key))
            finally: exporter._connect=saved
            self.assertEqual(audits(),count+int(after)); self.assertEqual(raw_audits(),raw_count)
            self.assertTrue(context._closed)
        for after in (False,True):
            context=exporter.open(record.id,actor=actor); saved=exporter._read_connect; count=raw_audits()
            exporter._read_connect=lambda:FailedCommit(self.connect('reader'),after)
            try:
                with self.assertRaises(psycopg.Error): next(context.stream_capture(key))
            finally: exporter._read_connect=saved
            self.assertEqual(raw_audits(),count+int(after)); self.assertTrue(context._closed)
        # Quota refusal during the durable download audit cannot reach raw access.
        context=exporter.open(record.id,actor=actor); count=audits(); raw_count=raw_audits()
        with self.connect('migration_owner') as conn:
            old=conn.execute("SELECT metadata_rows FROM waveforms.capacity WHERE bucket='GW'").fetchone()[0]
            conn.execute("UPDATE waveforms.capacity SET metadata_rows=used_metadata_rows WHERE bucket='GW'")
        try:
            with self.assertRaises(WaveformError): next(context.stream_manifest())
        finally:
            with self.connect('migration_owner') as conn: conn.execute("UPDATE waveforms.capacity SET metadata_rows=%s WHERE bucket='GW'",(old,))
        self.assertEqual(audits(),count); self.assertEqual(raw_audits(),raw_count)
        # Permission is refreshed at first advance, not merely when the context opens.
        context=exporter.open(record.id,actor=actor)
        vector['evidence_service'].revoke(vector['profile'].id,vector['evidence_command']('revoke',vector['profile'].id,2))
        count=audits()
        with self.assertRaises(WaveformError): next(context.stream_manifest())
        self.assertEqual(audits(),count)

    def exporter(self):
        module=feature(self,'export'); self.assertTrue(hasattr(module,'DatasetExporter'),'Actor-bound exporter absent')
        from tests.waveform_support import LIMITS
        return module.DatasetExporter(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))

    def export_command(self,version,intent,actor):
        from backend.waveforms.commands import Command,export_request_hash
        body=dict(version_id=str(version),purpose=intent); command=Command(actor,uuid4(),0,'Synthetic export','0'*64)
        return body,replace(command,request_hash=export_request_hash(version,command,body=body))

    def test_bound_export_retries_and_restriction_matrix(self):
        exporter=self.exporter()
        from backend.waveforms.commands import Actor
        from backend.waveforms.pair_contracts import capture_key
        from backend.waveforms.errors import WaveformError
        service,version,vector=self.frozen(two=True); actor=Actor('local','download','Download','admin')
        body,command=self.export_command(version.id,'research',actor)
        record=exporter.create(body,command)
        self.assertEqual(exporter.create(body,command).id,record.id)
        key=capture_key(vector['selection']['captures'][0]['key'])
        a=b''.join(exporter.open(record.id,actor=actor).stream_capture(key))
        b=b''.join(exporter.open(record.id,actor=actor).stream_capture(key))
        self.assertEqual(a,b); self.assertEqual(a,b'\n'.join(vector['wire_captures'][0])+b'\n')
        descriptor=exporter.files(version.id,actor=actor)[0]
        self.assertEqual(sha256(a).hexdigest(),descriptor['file_sha256'])
        self.assertEqual(len(a),descriptor['content_length'])
        manifest=b''.join(exporter.open(record.id,actor=actor).stream_manifest())
        self.assertEqual(sha256(manifest).hexdigest(),version.content_sha256)
        with self.assertRaises(WaveformError): exporter.open(record.id,actor=Actor('local','other','Other','admin'))
        body,cmd=self.export_command(version.id,'training',actor)
        with self.assertRaises(WaveformError): exporter.create(body,cmd)
        id=UUID(vector['selection']['channels'][-1]['calibration']['id'])
        vector['evidence_service'].revoke(id,vector['evidence_command']('revoke',id,2))
        self.assertEqual(service.get(version.id).restriction_state,'restricted')
        with self.assertRaises(WaveformError): exporter.open(record.id,actor=actor)
        body,cmd=self.export_command(version.id,'evidence',actor); historical=exporter.create(body,cmd)
        context=exporter.open(historical.id,actor=actor)
        self.assertEqual(context.metadata.restriction_state,'restricted')
        self.assertEqual(b''.join(context.stream_capture(key)),a)
        self.assertEqual(b''.join(exporter.open(historical.id,actor=actor).stream_manifest()),manifest)
