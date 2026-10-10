"""Real owned broker, PostgreSQL and sender receipts; synthetic bytes only."""
import importlib.util
import unittest
from tests.waveform_support import WaveformDatabaseCase
from tests.waveform_dataset_support import DatasetCase
from tests.inversion_e2e_support import EndToEndCase
from tests.waveform_stack import Stack
import tempfile
from pathlib import Path


class RecoveryInventoryTests(WaveformDatabaseCase):
    def test_effective_relation_acls_preserve_empty_public_runtime_grant_option_and_owner(self):
        from psycopg import sql
        from tools.waveform_restore import security_inventory
        conn=self.admin()
        try:
            conn.execute('CREATE TABLE waveforms.task17_acl_probe(value int)')
            conn.execute('CREATE SEQUENCE waveforms.task17_acl_sequence')
            conn.execute('CREATE INDEX task17_acl_index ON waveforms.task17_acl_probe(value)')
            def row(name): return next(r for r in security_inventory(conn)['relations'] if r[1]==name)
            def fresh(): return security_inventory(conn,profile='wp05a')
            original=row('task17_acl_probe'); sequence=row('task17_acl_sequence')
            original_fresh=fresh()
            self.assertTrue(conn.execute("SELECT relacl IS NULL FROM pg_class WHERE oid='waveforms.task17_acl_probe'::regclass").fetchone()[0])
            conn.execute('GRANT ALL ON TABLE waveforms.task17_acl_probe TO CURRENT_USER')
            conn.execute('GRANT ALL ON SEQUENCE waveforms.task17_acl_sequence TO CURRENT_USER')
            self.assertFalse(conn.execute("SELECT relacl IS NULL FROM pg_class WHERE oid='waveforms.task17_acl_probe'::regclass").fetchone()[0])
            self.assertEqual(row('task17_acl_probe'),original)
            self.assertEqual(row('task17_acl_sequence'),sequence)
            self.assertEqual(fresh(),original_fresh,'Default owner ACL and explicit owner-all are effectively identical')
            self.assertEqual({grant[2] for grant in sequence[4]},{'SELECT','UPDATE','USAGE'})
            self.assertEqual(row('task17_acl_index')[4],[],'Index is not a table ACL object')
            conn.execute('SAVEPOINT grants')
            conn.execute('REVOKE ALL ON TABLE waveforms.task17_acl_probe FROM CURRENT_USER')
            self.assertEqual(row('task17_acl_probe')[4],[]); self.assertNotEqual(row('task17_acl_probe'),original)
            self.assertNotEqual(fresh(),original_fresh,'Explicit empty ACL is not the owner default')
            conn.execute('ROLLBACK TO SAVEPOINT grants')
            conn.execute('GRANT SELECT ON waveforms.task17_acl_probe TO PUBLIC')
            public=row('task17_acl_probe'); self.assertNotEqual(public,original)
            self.assertNotEqual(fresh(),original_fresh)
            self.assertIn(['PUBLIC','SELECT',False],[grant[1:] for grant in public[4]])
            conn.execute('ROLLBACK TO SAVEPOINT grants')
            receiver=sql.Identifier(self.manifest['roles']['receiver'])
            conn.execute(sql.SQL('GRANT SELECT ON waveforms.task17_acl_probe TO {}').format(receiver))
            runtime=row('task17_acl_probe'); self.assertNotEqual(runtime,original)
            runtime_fresh=fresh(); self.assertNotEqual(runtime_fresh,original_fresh)
            conn.execute(sql.SQL('GRANT SELECT ON waveforms.task17_acl_probe TO {} WITH GRANT OPTION').format(receiver))
            self.assertNotEqual(row('task17_acl_probe'),runtime)
            self.assertNotEqual(fresh(),runtime_fresh)
            conn.execute('ROLLBACK TO SAVEPOINT grants')
            conn.execute(sql.SQL('ALTER TABLE waveforms.task17_acl_probe OWNER TO {}').format(receiver))
            self.assertNotEqual(row('task17_acl_probe'),original)
            self.assertNotEqual(fresh(),original_fresh)
        finally: conn.rollback()

    def test_native_check_roundtrip_keeps_operand_boolean_and_constraint_flags_distinct(self):
        from tools.waveform_restore import security_inventory
        conn=self.connect('migration_owner')
        try:
            conn.execute('CREATE TABLE waveforms.task17_check_probe(a int,b int,CONSTRAINT task17_check CHECK (a BETWEEN 1 AND 9 AND b<10))')
            def row(): return next(r for r in security_inventory(conn)['constraints'] if r[2]=='task17_check')
            original=row()
            native=conn.execute("SELECT pg_get_constraintdef(oid,false) FROM pg_constraint WHERE conrelid='waveforms.task17_check_probe'::regclass").fetchone()[0]
            conn.execute('ALTER TABLE waveforms.task17_check_probe DROP CONSTRAINT task17_check')
            conn.execute('ALTER TABLE waveforms.task17_check_probe ADD CONSTRAINT task17_check '+native)
            self.assertEqual(row(),original,'Native dump/reparse grouping must compare equally')
            for expression in ('CHECK (a BETWEEN 1 AND 9 OR b<10)','CHECK (a BETWEEN 0 AND 9 AND b<10)',
                'CHECK (a BETWEEN 1 AND 9 AND b<10) NOT VALID','CHECK (a BETWEEN 1 AND 9 AND b<10) NOT ENFORCED'):
                conn.execute('SAVEPOINT check_change')
                conn.execute('ALTER TABLE waveforms.task17_check_probe DROP CONSTRAINT task17_check')
                conn.execute('ALTER TABLE waveforms.task17_check_probe ADD CONSTRAINT task17_check '+expression)
                self.assertNotEqual(row(),original)
                conn.execute('ROLLBACK TO SAVEPOINT check_change')
            # Independently vary a deferred FK, not SQL parentheses.
            conn.execute('ALTER TABLE waveforms.task17_check_probe ADD CONSTRAINT task17_key UNIQUE(a)')
            conn.execute('ALTER TABLE waveforms.task17_check_probe ADD CONSTRAINT task17_fk FOREIGN KEY(b) REFERENCES waveforms.task17_check_probe(a) DEFERRABLE INITIALLY IMMEDIATE')
            before=security_inventory(conn)['constraints']
            conn.execute('ALTER TABLE waveforms.task17_check_probe ALTER CONSTRAINT task17_fk INITIALLY DEFERRED')
            self.assertNotEqual(security_inventory(conn)['constraints'],before)
        finally: conn.rollback()


class WaveformEndToEndTests(WaveformDatabaseCase):
    def test_blocked_storage_child_cannot_stall_owned_work_deadline(self):
        import json,os,sys,time
        from uuid import uuid4
        from unittest.mock import patch
        from tests.waveform_resource_scenarios import PreparationDeadline,RecoveryStorage,measured_job
        from tests.waveform_measurements import host_resources,safety_check
        from tools.verify_waveforms import source_inventory,inventory_sha256
        from tools.verify_ingest_reliability import protected_snapshot
        protection=protected_snapshot()
        source=source_inventory(); digest=inventory_sha256(source)
        expected=os.environ.get('OILWELL_WAVEFORM_FIX1_SOURCE')
        if expected is not None: self.assertEqual(digest,expected)
        run_id=os.environ.get('OILWELL_WAVEFORM_FIX1_RUN_ID',uuid4().hex)
        self.assertRegex(run_id,r'^[0-9a-f]{32}$')
        root=Path('logs/waveforms-wp04')/('r88-fix1-'+run_id); root.mkdir(parents=True,exist_ok=False)
        print('R88_FIX1_EVIDENCE',root.resolve(),flush=True)
        (root/'source-inventory.json').write_text(json.dumps(source,indent=2),encoding='utf-8')
        safety_check(host_resources(root),preflight=True)
        deadline=PreparationDeadline(until=time.monotonic()+60)
        monitor=RecoveryStorage(self,root,deadline=deadline,outer=deadline.until,seconds=12,fault='blocked_storage')
        deadline.storage_guard=monitor; observed=[]; held=False
        self.guard.execute('SELECT pg_advisory_lock(499001219)'); held=True
        began=time.monotonic(); accept=monitor.accept
        def observe(event):
            accept(event)
            if event.get('event')=='storage_started' and event['sequence']==2:
                with self.connect('reader') as conn:
                    conn.execute("SET LOCAL statement_timeout='3s'")
                    until=time.monotonic()+2
                    while True:
                        count=conn.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) AND classid=0 AND objid=499001219 AND NOT granted").fetchone()[0]
                        if count: observed.append(dict(monotonic=time.monotonic(),blocked=count)); break
                        if time.monotonic()>=until: raise AssertionError('Expected owned storage SQL waiter absent')
                        time.sleep(.05)
        try:
            with patch.object(monitor,'accept',side_effect=observe):
                with self.assertRaisesRegex(RuntimeError,'safety.operation_deadline'):
                    with monitor: measured_job(self,root,dict(kind='diagnostic_wait'),deadline=deadline)
            self.assertEqual(len(observed),1); self.assertEqual(observed[0]['blocked'],1)
            self.assertLess(time.monotonic()-began,25)
            self.assertTrue(monitor.closed); self.assertFalse(monitor.watchdog.thread.is_alive())
            self.assertFalse(monitor.watchdog.live_handles())
            envelopes=[json.loads(path.read_text(encoding='utf-8')) for path in root.glob('measurement-*/result.json')]
            self.assertEqual({value['kind'] for value in envelopes},{'resource','storage'})
            self.assertTrue(all(value['owned_cleanup']['complete'] for value in envelopes))
            self.assertTrue(all(value['status']=='partial' for value in envelopes))
            self.assertTrue(any(len(json.loads(line)['processes'])==4 for value in envelopes for line in Path(value['samples_path']).read_text(encoding='utf-8').splitlines()))
            self.assertEqual(source_inventory(),source)
        finally:
            active=sys.exc_info()[1]; first=active
            try:
                if held:
                    self.assertTrue(self.guard.execute('SELECT pg_advisory_unlock(499001219)').fetchone()[0]); held=False
            except BaseException as cleanup:
                if first is None: first=cleanup
                else: first.add_note('Fault lock cleanup failure: '+type(cleanup).__name__)
            try:
                after=protected_snapshot()
                (root/'fault-result.json').write_text(json.dumps(dict(observed_waiters=observed,elapsed_seconds=time.monotonic()-began,
                    source_sha256=digest,monitor_closed=monitor.closed,watchdog_joined=not monitor.watchdog.thread.is_alive(),
                    owned_processes=[dict(pid=p.pid,exit_code=p.poll()) for p in monitor.watchdog.processes],fault_lock_released=not held,
                    protection=dict(before=protection,after=after,unchanged=protection==after)),indent=2),encoding='utf-8')
                self.assertEqual(after,protection)
            except BaseException as cleanup:
                if first is None: first=cleanup
                else: first.add_note('Fault evidence retention failure: '+type(cleanup).__name__)
            if first is not None and active is None: raise first

    def http_deadline_source(self):
        from uuid import uuid4
        from tests import waveform_measurements as measurements
        root=Path(__file__).resolve().parents[1]/'.superpowers/sdd/2026-09-29-synchronized-waveform-paired-dataset/task-17-r86-focused'/uuid4().hex
        root.mkdir(parents=True,exist_ok=False)
        path=root/'tiny.owpwave'
        measurements.build_capture(path,frames=64,channels=1,chunk_frames=16,channel_specs=measurements.calibrated_channels(self,1))
        _,pk,_=measurements.ingest_file(self,path)
        route=f'/captures/{pk}/streams/resource/window?start=0&count=64&mode=raw'
        print('R86_HTTP_EVIDENCE '+str(root),flush=True)
        return root,route

    def test_http_success_and_sampling_failure_retains_cleanup(self):
        import json
        from unittest.mock import patch
        from tests import waveform_measurements as measurements
        from tests.waveform_resource_scenarios import measured_http
        root,route=self.http_deadline_source()
        success=measured_http(self,root,route,expected_points=64)
        self.assertEqual(success['status'],'passed'); self.assertTrue(success['sampler_joined']); self.assertTrue(success['socket_closed'])
        with patch.object(measurements,'process_memory',side_effect=OSError('Injected sampler query error')):
            with self.assertRaisesRegex(OSError,'Injected sampler query error'): measured_http(self,root,route,fault='slow_http_progress')
        values=[json.loads(p.read_text(encoding='utf-8')) for p in root.glob('measurement-*/result.json')]
        self.assertEqual(len(values),2)
        for value in values:
            self.assertTrue(value['owned_cleanup']['complete']); self.assertTrue(value['sampler_joined']); self.assertTrue(value['socket_closed'])
            self.assertTrue(Path(value['samples_path']).read_text(encoding='utf-8').strip())

    def test_http_total_deadline_with_slow_progress_retains_cleanup(self):
        import json
        from tests.waveform_resource_scenarios import measured_http
        root,route=self.http_deadline_source()
        with self.assertRaisesRegex(RuntimeError,'safety.operation_deadline'):
            measured_http(self,root,route,seconds=3,fault='slow_http_progress')
        values=[json.loads(p.read_text(encoding='utf-8')) for p in root.glob('measurement-*/result.json')]
        self.assertEqual(len(values),1); slow=values[0]
        self.assertEqual(slow['reason'],'safety.operation_deadline')
        self.assertGreater(len([e for e in slow['events'] if e['event']=='http_slow_progress']),2)
        self.assertLess(slow['elapsed_seconds'],6)
        self.assertTrue(slow['owned_cleanup']['complete']); self.assertTrue(slow['sampler_joined']); self.assertTrue(slow['socket_closed'])
        self.assertTrue(Path(slow['samples_path']).read_text(encoding='utf-8').strip())

    def check_roundtrip(self,offline_first):
        self.assertIsNotNone(importlib.util.find_spec('tests.waveform_measurements'))
        from tests.waveform_measurements import protocol_roundtrip
        from dataclasses import replace
        from tests.waveform_support import LIMITS
        from tests.test_waveform_repository import capture
        limits=replace(LIMITS,gateways={'GW':LIMITS.gateways['GW'],'A':LIMITS.gateways['GW']})
        with self.capacity_fixture(limits),Stack(self,limits=limits) as stack:
            result=protocol_roundtrip(self,stack,offline_first=offline_first,payloads=capture('A' if offline_first else 'GW'))
            self.assertEqual(result['facts'],2)
            self.assertEqual(result['transports'],['file','mqtt'])
            self.assertEqual(result['sender_state'],'archived')
            self.assertEqual(result['receipts'],3)
            self.assertTrue(result['exact_bytes']); self.assertTrue(result['digests_match'])
            self.assertNotEqual(result['file_source_ref'],result['mqtt_source_ref'])

    def test_online_then_offline_same_exact_bytes(self): self.check_roundtrip(False)
    def test_offline_then_online_same_exact_bytes(self): self.check_roundtrip(True)

    def test_measured_http_child_returns_calibrated_points_and_joins(self):
        from tests.waveform_measurements import calibrated_channels,build_capture,ingest_file
        from tests.waveform_resource_scenarios import measured_http
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); specs=calibrated_channels(self,4)
            path=root/'small.owpwave'; build_capture(path,frames=64,channels=4,chunk_frames=16,channel_specs=specs)
            from tests.waveform_resource_scenarios import measured_job
            ingestion=measured_job(self,root,dict(kind='intake',path=str(path)))
            self.assertTrue(Path(ingestion['envelope_path']).is_file())
            pk=ingestion['result']['capture_pk']
            result=measured_http(self,root,f'/captures/{pk}/streams/resource/window?start=0&count=64&mode=raw',expected_points=256)
            self.assertEqual(result['points'],256); self.assertEqual(result['channels'],4)
            self.assertEqual(result['exit_code'],0); self.assertFalse(result['cleanup']['remaining_threads'])
            self.assertTrue(Path(result['envelope_path']).is_file())
            requests=[e for e in result['events'] if e['event']=='http_request_complete']
            self.assertEqual(requests[-1]['status'],200)
            self.assertEqual(requests[-1]['sample_passes'][0]['values'],256)
            self.assertGreater(requests[-1]['raw_queries']['calls'],0)
            summary=measured_http(self,root,f'/captures/{pk}/streams/resource/window?start=0&count=64&mode=summary&budget=64',expected_points=256)
            request=next(e for e in summary['events'] if e['event']=='http_request_complete')
            self.assertEqual([p['values'] for p in request['sample_passes']],[256,256])
            self.assertEqual(request['summary_plan']['calls'],4)
            self.assertGreater(request['prepare']['elapsed_seconds'],0)
            self.assertGreater(request['recheck']['elapsed_seconds'],0)
            self.assertGreater(request['json_before_headers_seconds'],0)
            from tests.waveform_resource_scenarios import measured_output_owners
            owners=measured_output_owners(self,root,f'/captures/{pk}/streams/resource/window?start=0&count=64&mode=raw')
            self.assertTrue(owners['healthy']); self.assertEqual(owners['cleanup']['connections'],0)
            self.assertTrue(Path(owners['envelope_path']).is_file())

    def test_resource_http_and_output_abort_preserve_independent_samples(self):
        import json
        from unittest.mock import patch
        from tests.waveform_measurements import calibrated_channels,build_capture,OwnedSampler
        from tests import waveform_resource_scenarios as resources
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/'small.owpwave'
            build_capture(path,frames=64,channels=4,chunk_frames=16,channel_specs=calibrated_channels(self,4))
            first=resources.measured_job(self,root,dict(kind='intake',path=str(path)))
            self.assertIn('envelope_path',first)
            original=Path(first['envelope_path']).read_bytes(); pk=first['result']['capture_pk']
            route=f'/captures/{pk}/streams/resource/window?start=0&count=64&mode=raw'
            with patch.object(resources,'safety_check',side_effect=RuntimeError('safety.host_reserve')):
                with self.assertRaisesRegex(RuntimeError,'safety.host_reserve'):
                    resources.measured_job(self,root,dict(kind='intake',path=str(path)))
            with self.assertRaises(AssertionError): resources.measured_http(self,root,'/captures/not-a-uuid')
            with patch.object(OwnedSampler,'check',side_effect=RuntimeError('safety.operation_deadline')):
                with self.assertRaisesRegex(RuntimeError,'safety.operation_deadline'):
                    resources.measured_output_owners(self,root,route)
            self.assertEqual(Path(first['envelope_path']).read_bytes(),original)
            envelopes=[json.loads(p.read_text(encoding='utf-8')) for p in root.glob('measurement-*/result.json')]
            failed=[e for e in envelopes if e['status']=='partial']; self.assertEqual(len(failed),3)
            self.assertEqual({e['kind'] for e in failed},{'resource','http','output_owners'})
            for value in failed:
                self.assertTrue(value['owned_cleanup']['complete'])
                self.assertTrue(Path(value['samples_path']).read_text(encoding='utf-8').strip())
                self.assertTrue(all(p['exit_code'] is not None for p in value['owned_cleanup']['processes']))
            http=next(e for e in failed if e['kind']=='http')
            response=next(e for e in http['events'] if e['event']=='http_request_complete')
            self.assertGreaterEqual(response['status'],400); self.assertTrue(response['error_code'])

    def test_small_distinct_archive_population_and_measured_publication(self):
        from tests import waveform_resource_scenarios as scenarios
        self.assertTrue(hasattr(scenarios,'prepare_members'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            members=scenarios.prepare_members(self,root,count=2)
            self.assertEqual(len({m['reference_id'] for m in members}),2)
            result=scenarios.measured_dataset(self,root,members)
            self.assertEqual(result['result']['members'],2)
            self.assertEqual(result['result']['files'],2)
            self.assertEqual(result['exit_code'],0)
            # This exercises recovery's comparison/delta logic on SOURCE only;
            # it is not a dump/restore experiment or recovered-target claim.
            from dataclasses import asdict
            from tests.waveform_pairing_support import REVIEWER
            from tests.waveform_support import LIMITS
            from tools.waveform_restore import content_inventory,verify_export_deltas,security_inventory
            with self.admin() as connection:
                connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
                before=content_inventory(connection)
                self.assertTrue(security_inventory(connection)['extension'])
            fixture=self
            class SourceOnlyProbe:
                profile='wp04'
                def connect(self,role): return fixture.admin() if role=='admin' else fixture.connect(role)
            value=result['result']; record=dict(version_id=value['version_id'],export_id=value['export_id'],actor=asdict(REVIEWER))
            compared=verify_export_deltas(SourceOnlyProbe(),before,[record],root/'reexport',limits=LIMITS,
                references={value['version_id']:value['export_root']})
            self.assertEqual(compared['raw_audits'],4); self.assertEqual(compared['business_audits'],3)
            self.assertEqual(compared['metadata_charges'],{'GW':7})

    def test_three_small_sender_sources_are_measured_in_one_actual_batch(self):
        from tests.waveform_measurements import build_capture
        from tests.waveform_resource_scenarios import measured_sender
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); paths=[]
            for index in range(3):
                path=root/(str(index)+'.owpwave'); build_capture(path,frames=4,channels=1,chunk_frames=4); paths.append(path)
            result=measured_sender(self,root,paths,with_receipts=True)
            self.assertEqual(result['captures'],3); self.assertEqual(result['diagnostic_events'],128)
            self.assertEqual(result['durability'],dict(journal_mode='delete',synchronous=3))
            self.assertEqual(result['exit_code'],0); self.assertTrue(result['samples'])
            self.assertTrue(any(e.get('captures')==3 for e in result['events'] if e['event']=='sender_batch'))
            started={(e['phase'],e['operation_id']) for e in result['events'] if e['event']=='sender_phase_started'}
            ended={(e['phase'],e['operation_id']) for e in result['events'] if e['event']=='sender_phase_complete'}
            self.assertEqual(started,ended); self.assertEqual({key[0] for key in started},{'run_once','prepare'})
            self.assertEqual(result['status'],'passed'); self.assertTrue(result['cleanup']['complete'])

    def test_sender_partial_samples_survive_second_case_abort_after_owned_cleanup(self):
        import json
        from unittest.mock import patch
        from tests.waveform_measurements import build_capture
        from tests import waveform_resource_scenarios as resources
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/'small.owpwave'
            build_capture(path,frames=4,channels=1,chunk_frames=4)
            first=resources.measured_sender(self,root,[path],with_receipts=True)
            self.assertIn('envelope_path',first)
            original=Path(first['envelope_path']).read_bytes()
            check=resources.SenderDeadline.check
            calls=0
            def abort_after_sample(deadline,now,events):
                nonlocal calls
                calls+=1
                if calls>=2: raise RuntimeError('safety.sender_operation_deadline')
                return check(deadline,now,events)
            with patch.object(resources.SenderDeadline,'check',abort_after_sample):
                with self.assertRaisesRegex(RuntimeError,'sender_operation_deadline'):
                    resources.measured_sender(self,root,[path],with_receipts=True)
            self.assertEqual(Path(first['envelope_path']).read_bytes(),original)
            envelopes=list(root.glob('sender-*/result.json')); self.assertEqual(len(envelopes),2)
            partial=next(json.loads(p.read_text(encoding='utf-8')) for p in envelopes if p!=Path(first['envelope_path']))
            self.assertEqual(partial['status'],'partial')
            self.assertEqual(partial['reason'],'safety.sender_operation_deadline')
            self.assertTrue(partial['cleanup']['complete'])
            self.assertTrue(all(p['exit_code'] is not None for p in partial['cleanup']['processes']))
            samples=[json.loads(line) for line in Path(partial['samples_path']).read_text(encoding='utf-8').splitlines()]
            self.assertGreaterEqual(len(samples),2)
            self.assertTrue(all(s['memory']['working_set_bytes']>0 and s['physical']['database_bytes']>0 for s in samples))

    def test_transport_retained_orphan_and_wrong_gateway_remain_distinct(self):
        from dataclasses import replace
        from tests.waveform_experiments import transport_scenario
        from tests.waveform_support import LIMITS
        limits=replace(LIMITS,gateways={'GW':LIMITS.gateways['GW'],'A':LIMITS.gateways['GW']})
        with tempfile.TemporaryDirectory() as directory:
            result=transport_scenario(self,Path(directory),limits)
            self.assertTrue(result['orphan_recovered']); self.assertTrue(result['cross_gateway_rejected'])

    def test_failed_owned_context_keeps_diagnostic_hashes_outside_removable_scratch(self):
        import json
        from tools.verify_waveforms import verify_artifacts
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError,'controlled failure') as caught:
                with Stack(self,evidence_root=Path(directory)) as stack:
                    stack.launch('receive'); stack.await_event('receive','subscribed',gateway='GW')
                    raise RuntimeError('controlled failure')
            note=next(n for n in caught.exception.__notes__ if n.startswith('Owned failure evidence: '))
            retained=Path(note.removeprefix('Owned failure evidence: '))
            self.assertFalse(retained.is_relative_to(Path(directory)))
            inventory=json.loads((retained/'evidence-hashes.json').read_text(encoding='utf-8'))
            self.assertEqual(verify_artifacts(retained,[dict(relative_path=i['path'],size=i['size'],sha256=i['sha256']) for i in inventory]),())

    def test_small_audit_volume_uses_real_late_commit_and_stale_token_refusal(self):
        from tests.waveform_resource_scenarios import prepare_members,audit_scenarios
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); prepare_members(self,root,count=1)
            result=audit_scenarios(self,root,levels=(3,5))
            self.assertEqual([r['actual_rows'] for r in result['levels']],[3,5])
            self.assertTrue(all(r['stale_page_rejected'] for r in result['levels']))


def _save_small_restore_result(root,result,source,protection,connections,guard,active):
    from tools.verify_waveforms import source_inventory
    from tools.verify_ingest_reliability import protected_snapshot
    from tests.waveform_experiments import save_result
    first=active
    for name,check in (
        ('source_unchanged',lambda:source_inventory()==source),
        ('protection_unchanged',lambda:protected_snapshot()==protection),
        ('fixture_connections_closed_except_guard',lambda:all(c.closed or c is guard for c in connections))):
        try:
            result[name]=check()
            if not result[name]: raise AssertionError('Small restore protection failed: '+name)
        except BaseException as error:
            result[name]=False
            if first is None: first=error
            else: first.add_note('Small restore check failure: '+name+': '+type(error).__name__)
    if first is not None: result.update(status='partial',error_type=type(first).__name__)
    try: save_result(root,'small-restore-result',result)
    except BaseException as error:
        if first is None: first=error
        else: first.add_note('Small restore evidence failure: '+type(error).__name__)
    if active is None and first is not None: raise first


class RecoveryPreparationVolumeTests(unittest.TestCase):
    def test_small_restore_receipt_refuses_failed_protection_and_preserves_first_error(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        checks=('source_unchanged','protection_unchanged','fixture_connections_closed_except_guard')
        for failed in checks:
            with self.subTest(failed=failed):
                result={'status':'passed'}; saved=[]
                with patch('tools.verify_waveforms.source_inventory',return_value={} if failed==checks[0] else {'a':'b'}), \
                     patch('tools.verify_ingest_reliability.protected_snapshot',return_value={} if failed==checks[1] else {'c':'d'}), \
                     patch('tests.waveform_experiments.save_result',side_effect=lambda root,name,value:saved.append(dict(value))):
                    with self.assertRaises(AssertionError):
                        _save_small_restore_result(Path('unused'),result,{'a':'b'},{'c':'d'},[SimpleNamespace(closed=failed!=checks[2])],object(),None)
                self.assertEqual(saved[0]['status'],'partial'); self.assertFalse(saved[0][failed])
        first=ValueError('body'); result={'status':'passed'}
        with patch('tools.verify_waveforms.source_inventory',return_value={}), \
             patch('tools.verify_ingest_reliability.protected_snapshot',return_value={}), \
             patch('tests.waveform_experiments.save_result',side_effect=OSError('save')):
            _save_small_restore_result(Path('unused'),result,{}, {},[],object(),first)
        self.assertEqual(result['status'],'partial'); self.assertEqual(result['error_type'],'ValueError')
        self.assertIn('OSError',first.__notes__[-1])

    def test_invalid_volumes_are_rejected_before_fixture_access(self):
        from tests.waveform_experiments import prepare_recovery
        for volumes in (None,(),[],{},'1',(0,),(-1,),(1001,),(True,),(1.0,),(2,1),(1,1),(1,'2')):
            with self.subTest(volumes=volumes),self.assertRaises(ValueError):
                prepare_recovery(object(),Path('unused'),volumes=volumes)


class RecoveryPreparationTests(EndToEndCase):
    def setUp(self):
        if self._testMethodName=='test_small_restore_uses_supervised_target_exports_and_exact_audit_deltas':
            from tests.inversion_governance_support import GovernanceCase
            GovernanceCase.setUp(self)
        else:
            DatasetCase.setUp(self)

    def test_small_restore_uses_supervised_target_exports_and_exact_audit_deltas(self):
        import json,os,time,sys
        from datetime import datetime,timezone
        from uuid import UUID,uuid4
        from backend.waveforms.commands import Actor
        from backend.waveforms.export import DatasetExporter
        from backend.waveforms.errors import WaveformError
        from tests.waveform_support import LIMITS
        from tests.waveform_experiments import prepare_recovery,save_result
        from tests.waveform_resource_scenarios import PreparationDeadline,supervised_target_exports
        from tests.waveform_measurements import host_resources,safety_check
        from tools.waveform_restore import ROOT,create_run_root,backup_restore,verify_export_deltas
        from tools.verify_waveforms import source_inventory,inventory_sha256
        from tools.verify_ingest_reliability import protected_snapshot
        source=source_inventory(); digest=inventory_sha256(source)
        if os.environ.get('OILWELL_WAVEFORM_R89_SOURCE'): self.assertEqual(digest,os.environ['OILWELL_WAVEFORM_R89_SOURCE'])
        name=os.environ.get('OILWELL_WAVEFORM_R89_RUN',datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex)
        root,run_id=create_run_root(ROOT/'logs/waveforms-wp04'/name)
        print('R89_RESTORE_ROOT',root,flush=True)
        safety_check(host_resources(root),preflight=True); protection=protected_snapshot()
        save_result(root,'source-inventory',source)
        deadline=PreparationDeadline(until=time.monotonic()+600); result=dict(status='partial',run_id=run_id,source_sha256=digest)
        try:
          with self.capacity_fixture(LIMITS):
            prepared=prepare_recovery(self,root,volumes=(1,),deadline=deadline,run_id=run_id,source_sha256=digest)
            save_result(root,'recovery-source',prepared)
            # The capacity context reset removed the setup attachment. Build the
            # genuine inversion source only after waveform recovery preparation.
            from hashlib import sha256
            payload=b'%PDF-1.7 Synthetic engineering report with explicitly known bounds'
            attachment=dict(media_type='application/pdf',payload_bytes=len(payload),payload_sha256=sha256(payload).hexdigest())
            self.attachment=self.evidence.upload_attachment(payload,media_type='application/pdf',command=self.wave_command('upload',attachment=attachment))
            self.start_http(); self.reviewer=self.server.auth.login('reviewer','Review@2026')
            self.prepare_native_input(); sentinel=self.completed_result()
            self.server.inversion_runtime.close(); self.close_server(); self.server.server_close()
            for connection in self.connections:
                if connection is not self.guard: connection.close()
            self.inversion_fixture.guard.close()
            result['inversion_sentinel']={key:sentinel[key] for key in ('id','content_sha256')}
            target,recovery=backup_restore(self,self.config_path,root,run_id,profile='wp05a')
            self.assertTrue(recovery['inversion_schema']); self.assertTrue(recovery['inversion_runtime_probes'])
            with target.connect('inversion_reader') as conn:
                self.assertEqual(conn.execute('SELECT content_sha256 FROM inversion.results WHERE id=%s',(sentinel['id'],)).fetchone(),(sentinel['content_sha256'],))
            baseline=json.loads((root/'source-fingerprint.json').read_text(encoding='utf-8'))['content']
            def execute(_connect,records,destination,*,limits,reference):
                self.assertEqual(Path(destination),root/'restored-exports')
                measured=supervised_target_exports(self,root,run_id,digest,records,reference,deadline=deadline)
                result['supervision']=measured; save_result(root,'target-export-supervision',measured)
                return measured['result']['exports']
            delta=verify_export_deltas(target,baseline,prepared['records'],root/'restored-exports',limits=LIMITS,references=prepared['references'],_execute=execute)
            restriction=prepared['restriction']; actor=Actor(**restriction['actor'])
            exporter=DatasetExporter(lambda:target.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:target.connect('reader'))
            with self.assertRaises(WaveformError) as denied: exporter.open(UUID(restriction['research_export_id']),actor=actor)
            self.assertEqual(denied.exception.code,restriction['code'])
            self.assertEqual(exporter.describe(UUID(restriction['version_id']),purpose='evidence',actor=actor).restriction_state,'restricted')
            self.assertEqual(delta['business_audits'],5); self.assertTrue(delta['unchanged_other_tables'])
            self.assertEqual(delta['profile'],'wp05a'); self.assertTrue(delta['inversion_unchanged']); self.assertTrue(delta['security_unchanged'])
            result['inventory_sensitivity']=self.check_restore_inventory(target)
            self.assertTrue(result['supervision']['owned_cleanup']['complete']); self.assertTrue(result['supervision']['watchdog_closed'])
            deadline.check(root,'small-restore-complete')
            recovery.update(exports=True,restrictions=True)
            result.update(status='passed',restore=recovery,deltas=delta,restrictions=True)
        finally:
            _save_small_restore_result(root,result,source,protection,self.connections,self.guard,sys.exc_info()[1])

    def test_two_stage_small_preparation_preserves_exact_revocation_and_all_exports(self):
        import json
        from uuid import UUID,uuid4
        from backend.waveforms.commands import Actor
        from backend.waveforms.errors import WaveformError
        from backend.waveforms.export import DatasetExporter
        from tests.waveform_support import LIMITS
        from tests.waveform_experiments import prepare_recovery
        from tools.verify_waveforms import source_inventory,inventory_sha256
        run_id=uuid4().hex; source=source_inventory(); source_sha256=inventory_sha256(source)
        root=Path('logs/waveforms-wp04')/('r88-small-'+run_id)
        root.mkdir(parents=True); print('R88_PREPARATION_EVIDENCE',root.resolve(),flush=True)
        (root/'source-inventory.json').write_text(json.dumps(source,indent=2),encoding='utf-8')
        with self.capacity_fixture(LIMITS):
            result=prepare_recovery(self,root,volumes=(1,2),run_id=run_id,source_sha256=source_sha256)
            restriction=result['restriction']; actor=Actor(**restriction['actor'])
            self.assertEqual(restriction['code'],'waveform.dataset_export_denied')
            exporter=DatasetExporter(lambda:self.connect('operator'),limits=LIMITS,shared_accounting_bucket='GW',read_connect=lambda:self.connect('reader'))
            with self.assertRaises(WaveformError) as caught: exporter.open(UUID(restriction['research_export_id']),actor=actor)
            self.assertEqual(caught.exception.code,'waveform.dataset_export_denied')
            self.assertEqual(exporter.open(UUID(restriction['evidence_export_id']),actor=actor).metadata.restriction_state,'restricted')
            self.assertEqual(len(result['records']),3); self.assertEqual(len(result['volumes']),2)
            self.assertEqual(len(result['source_exports']),1); self.assertEqual(len(result['source_exports'][0]['files']),2)
            self.assertTrue((root/'dataset-1.json').is_file()); self.assertTrue((root/'dataset-2.json').is_file()); self.assertFalse((root/'dataset-100.json').exists())
            self.assertEqual([json.loads(line)['count'] for line in (root/'population.jsonl').read_text(encoding='utf-8').splitlines()],[1,2])
            self.assertTrue(all(v['owned_cleanup']['complete'] for v in result['volumes']))
            clocks=[json.loads(line) for line in (root/'preparation-clock.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len({row['deadline'] for row in clocks}),2)
            self.assertEqual(clocks[-1]['phase'],'recovery-clean-complete')
            measurement=result['recovery_downloads']; self.assertEqual(len(measurement['downloads']),8)
            self.assertEqual(len([f for f in measurement['downloads'] if 'key' in f]),5)
            self.assertTrue(all(f['started']>=measurement['clock']['prepared'] for f in measurement['downloads']))
            self.assertTrue(all(c.closed or c is self.guard for c in self.connections))
            self.assertEqual(source_inventory(),source)
            (root/'small-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
            print('R88_STAGE_CLOCK',json.dumps(measurement['clock']),flush=True)
