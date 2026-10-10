import importlib.util
import unittest
from dataclasses import replace
from tests.inversion_result_support import ResultCase, ACTOR, REVIEWER, command

class ResultFoundationTests(unittest.TestCase):
    def test_result_governance_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.inversion.results'),'audited result governance required')

    def test_field_classification_requires_actual_source_and_admitted_independence(self):
        from backend.inversion.results import field_validation
        for source,independent,want in (
            ('synthetic',True,'not_field_validated'),('bench_measurement',True,'not_field_validated'),
            ('field_measurement',False,'not_field_validated'),('field_measurement',None,'not_field_validated'),
            ('field_measurement',True,'single_field_comparison')):
            value=field_validation(source,dict(independent_comparison=independent,independence_reasons=['original_basis']))
            self.assertEqual(value['state'],want)
            self.assertFalse(value['certified'])
            self.assertEqual(value['independence_reasons'],['original_basis'])

    def test_none_or_wrong_identity_authority_fails_before_read(self):
        from datetime import datetime, timezone
        from uuid import uuid4
        from backend.inversion.results import ResultService
        from backend.inversion.contracts import AuthorityRef, AuthoritySnapshot
        bad=(None,AuthoritySnapshot(AuthorityRef('local',2,1),frozenset({'inversion.read'}),datetime.now(timezone.utc),1))
        for value in bad:
            def no_connection(): self.fail('Unauthorized read reached database')
            service=ResultService(no_connection,read_connect=no_connection,authorize=lambda actor,cap:value)
            with self.assertRaises(RuntimeError): service.get(uuid4(),actor=ACTOR)

class ResultTests(ResultCase):
    def test_completed_is_not_approved(self):
        view=self.results.get(self.published.id,actor=ACTOR)
        self.assertEqual(view.execution,'completed')
        self.assertEqual(view.source,'synthetic')
        self.assertEqual(view.review['state'],'unreviewed')
        self.assertEqual(view.current_applicability['state'],'applicable')
        self.assertFalse(view.field_validation['certified'])
        self.assertNotEqual(view.field_validation['state'],'single_field_comparison')
        self.assertEqual(view.content_sha256,self.published.content_sha256)
        self.assertEqual(view.snapshot_sha256,self.prepared_result.snapshot.content_sha256)

    def test_result_cannot_be_self_reviewed(self):
        for actor in (ACTOR,replace(ACTOR,username='Renamed',role_code='admin')):
            with self.assertRaisesRegex(RuntimeError,'self_review'):
                self.results.review(self.published.id,'approve',command(actor))
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.result_reviews'),0)

    def test_related_revoke_restricts_but_keeps_history(self):
        self.approve(); before=self.results.get(self.published.id,actor=ACTOR)
        self.revoke_parameter(); after=self.results.get(self.published.id,actor=ACTOR)
        self.assertEqual(after.review['state'],'approved')
        self.assertEqual(after.current_applicability['state'],'restricted')
        self.assertTrue(after.current_applicability['reasons'])
        self.assertEqual(before.body,after.body)
        self.assertEqual(before.content_sha256,after.content_sha256)

    def test_unrelated_registry_revision_preserves_validity(self):
        with self.registry_connect() as conn:
            conn.execute('UPDATE device_registry.registry_head SET revision=revision+1 WHERE singleton=1')
        self.assertEqual(self.results.get(self.published.id,actor=ACTOR).current_applicability['state'],'applicable')

    def test_later_assignment_closure_preserves_immutable_result_and_export(self):
        from backend.inversion.exports import ResultExporter
        before=self.results.get(self.published.id,actor=ACTOR)
        snapshot=self.prepared_result.snapshot.canonical
        assignment=self.prepared_result.snapshot.body['applicability']['assignment']
        self.parameters.assign(dict(assembly_id=str(self.request.assembly_id),well_id='test:next-well',
            valid_from='2026-12-01T00:00:00.000000Z',valid_to=None,close_assignment_id=assignment['id']),command(version=1))
        after=self.results.get(self.published.id,actor=ACTOR)
        self.assertEqual(after.current_applicability['state'],'applicable')
        self.approve()
        exporter=ResultExporter(self.results)
        exported=exporter.create(self.published.id,'quantitative','json',command(ACTOR))
        self.assertTrue(b''.join(exporter.open(exported.id,actor=ACTOR)))
        self.assertEqual((before.body,before.chunks,before.content_sha256,before.snapshot_sha256),
                         (after.body,after.chunks,after.content_sha256,after.snapshot_sha256))
        self.assertEqual(self.prepared_result.snapshot.canonical,snapshot)
        from backend.inversion.contracts import canonical_bytes
        self.assertEqual(canonical_bytes(self.scalar('SELECT input_snapshot FROM inversion.results WHERE id=%s',
            (self.published.id,))),snapshot)

    def test_later_mechanical_successor_preserves_historical_result(self):
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.exports import ResultExporter
        import json
        before=self.results.get(self.published.id,actor=ACTOR)
        original=self.scalar('SELECT body FROM inversion.assemblies WHERE id=%s',(self.request.assembly_id,))
        successor=json.loads(canonical_bytes(original)); successor.update(parent_version=1,
            valid_from='2026-12-01T00:00:00.000000Z',asset_number='TEST-SUCCESSOR')
        self.parameters.create_assembly(successor,command(version=1))
        after=self.results.get(self.published.id,actor=ACTOR)
        self.assertEqual(after.current_applicability['state'],'applicable')
        self.approve()
        exporter=ResultExporter(self.results)
        exported=exporter.create(self.published.id,'quantitative','json',command(ACTOR))
        self.assertTrue(b''.join(exporter.open(exported.id,actor=ACTOR)))
        self.assertEqual((before.body,before.chunks,before.content_sha256,before.snapshot_sha256),
                         (after.body,after.chunks,after.content_sha256,after.snapshot_sha256))

    def test_assignment_closure_inside_uncertain_support_restricts_result(self):
        from datetime import datetime,timedelta,timezone
        from fractions import Fraction
        from backend.inversion.exports import ResultExporter
        before=self.results.get(self.published.id,actor=ACTOR)
        observation=self.prepared_result.snapshot.body['observation']
        upper=Fraction(observation['end_ns'])+Fraction(observation['uncertainty_ns'])
        close=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(microseconds=upper//1000)
        assignment=self.prepared_result.snapshot.body['applicability']['assignment']
        self.parameters.assign(dict(assembly_id=str(self.request.assembly_id),well_id='test:next-well',
            valid_from=close.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),valid_to=None,close_assignment_id=assignment['id']),command(version=1))
        after=self.results.get(self.published.id,actor=ACTOR)
        self.assertEqual(after.current_applicability['state'],'restricted')
        self.assertIn('assignment_interval',after.current_applicability['reasons'])
        with self.assertRaises(RuntimeError): self.approve()
        with self.assertRaises(RuntimeError): ResultExporter(self.results).create(self.published.id,'quantitative','json',command(ACTOR))
        self.assertEqual((before.body,before.chunks,before.snapshot_sha256),(after.body,after.chunks,after.snapshot_sha256))

    def test_review_replay_authorizes_and_revoke_is_append_only(self):
        cmd=command(REVIEWER); first=self.results.review(self.published.id,'approve',cmd)
        self.assertTrue(self.results.review(self.published.id,'approve',cmd).replayed)
        self.denied.add('inversion.review')
        with self.assertRaises(RuntimeError): self.results.review(self.published.id,'approve',cmd)
        self.denied.clear(); self.results.revoke(self.published.id,command(REVIEWER,first.version))
        view=self.results.get(self.published.id,actor=ACTOR)
        self.assertEqual(view.review['state'],'revoked')
        self.assertEqual(len(view.review['history']),2)
        self.assertIn('result_revoked',view.current_applicability['reasons'])
        self.assertEqual(view.content_sha256,self.published.content_sha256)

    def test_restricted_approval_and_stale_precondition_refuse(self):
        with self.assertRaises(RuntimeError): self.results.review(self.published.id,'approve',command(REVIEWER,1))
        self.revoke_parameter()
        with self.assertRaises(RuntimeError): self.approve()
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.result_reviews'),0)
        self.results.review(self.published.id,'reject',command(REVIEWER))
        view=self.results.get(self.published.id,actor=ACTOR)
        self.assertEqual(view.review['state'],'rejected')
        self.assertIn('result_rejected',view.current_applicability['reasons'])

    def test_current_read_authority_is_required(self):
        self.session_valid=False
        with self.assertRaises(RuntimeError): self.results.get(self.published.id,actor=ACTOR)

    def test_reference_edge_rows_survive_published_readback_and_legacy_bytes(self):
        import json
        from uuid import uuid4
        from backend.inversion.result_codec import encode,PreparedResult
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.comparison import compare
        from backend.inversion.queries import point_page
        from tests.test_inversion_comparison import prediction,reference,policy
        from tests.inversion_worker import REF,authority
        pred=prediction((0,3),(12.,12.)); ref=reference((0,1,2,3),(10,None,10,10))
        current=encode(pred,compare(pred,ref,policy()),self.prepared_result.snapshot)
        legacy=[]
        for raw in current.chunks:
            block=json.loads(raw)
            if block['kind']=='comparison':
                block['columns'].pop(); [row.pop() for row in block['rows']]
            legacy.append(canonical_bytes(block))
        old=PreparedResult(current.snapshot,current.metadata,tuple(legacy))
        for prepared in (current,old):
            run=self.create(); attempt=self.service.claim(uuid4())
            published=self.service.publish(attempt,prepared,authority(REF))
            view=self.results.get(published.id,actor=ACTOR)
            self.assertEqual((view.chunks,view.content_sha256),(prepared.chunks,prepared.content_sha256))
            page=point_page(view,2,2)
            self.assertEqual((page['offset'],len(page['rows'])),(2,2))
            values=[dict(zip(row['columns'],row['values'])) for row in page['rows']]
            self.assertEqual([row['time_ns'] for row in values],['0','3000000000'])
            if prepared is current:
                self.assertEqual([row['reference_edge_from_previous'] for row in values],[False,False])
            else: self.assertTrue(all('reference_edge_from_previous' not in row for row in values))

    def test_reader_rejects_corrupt_chunks_snapshot_and_aggregate_digest(self):
        from unittest.mock import patch
        from copy import deepcopy
        original=self.results.read_connect
        case=self
        class Cursor:
            def __init__(self,cursor,mode): self.cursor,self.mode=cursor,mode; self.statement=''
            def execute(self,statement,params=()):
                self.statement=statement
                if self.mode=='oversize' and statement.startswith('SELECT ordinal'):
                    case.fail('Oversized chunks were fetched before their byte budget was checked')
                return self.cursor.execute(statement,params)
            def fetchone(self):
                row=self.cursor.fetchone()
                if self.mode=='oversize' and self.statement.startswith('SELECT count(*)'):
                    return row[0],17*1024**2
                if row and self.statement.startswith('SELECT r.run_id'):
                    row=list(deepcopy(row))
                    if self.mode=='aggregate': row[3]='0'*64
                    if self.mode=='snapshot': row[1]['accounting_bucket']='corrupted'
                    return tuple(row)
                return row
            def __iter__(self):
                rows=self.cursor.fetchall()
                if rows and self.statement.startswith('SELECT ordinal'):
                    rows=list(rows); ordinal,payload,digest=rows[0]
                    if self.mode=='ordinal': rows[0]=(ordinal+1,payload,digest)
                    if self.mode=='chunk': rows[0]=(ordinal,bytes(payload)+b' ',digest)
                    if self.mode=='missing': rows=rows[1:]
                return iter(rows)
        class Connection:
            def __init__(self,mode): self.connection=original(); self.mode=mode
            def __enter__(self): self.connection.__enter__(); return self
            def __exit__(self,*args): return self.connection.__exit__(*args)
            def execute(self,*args): return self.connection.execute(*args)
            def cursor(self): return Cursor(self.connection.cursor(),self.mode)
        for mode in ('aggregate','snapshot','ordinal','chunk','missing','oversize'):
            with self.subTest(mode=mode),patch.object(self.results,'read_connect',side_effect=lambda:Connection(mode)):
                with self.assertRaises(RuntimeError): self.results.get(self.published.id,actor=ACTOR)
