import importlib.util
import json
import unittest
from unittest.mock import patch
from tests.inversion_result_support import ResultCase, ACTOR, REVIEWER, command

class ExportFoundationTests(unittest.TestCase):
    def test_export_service_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.inversion.exports'),'durable audited exporter required')

    def test_sensitive_values_and_nested_formula_strings(self):
        from backend.inversion.exports import safe_text, csv_cell, public_result
        for value in ('/secret','C:\\private\\secret.toml','postgresql://user:pass@server/db','postgres://x',
                      'pwd=private','connection_string=private','api_key=private'):
            with self.subTest(value=value): self.assertEqual(safe_text(value),'[redacted]')
        for value in (' =formula','\tformula','\rformula','\nformula','@formula','-formula'):
            self.assertTrue(csv_cell(value).startswith("'"))
        clean=public_result({'metadata':{'password':'hidden','private_key':'hidden','api_key':'hidden','nested':{'credentials':'hidden'}},'actor':'hidden'})
        self.assertNotIn('hidden',json.dumps(clean))

    def test_serialization_keeps_full_exact_points_and_nullable_metrics(self):
        # Pure formatting vector, not an acquired/publication or physics fixture.
        import csv
        import io
        from backend.inversion.exports import _serialize
        rows=[['9007199254740993','27021597764222979/7',None,False,'=untrusted'] for _ in range(513)]
        body=dict(result={'metric':{'value':None,'reason':'insufficient_valid_support'}},
            chunks=[dict(kind='comparison',columns=['power_index','time_ns','value','valid','reason'],rows=rows)])
        output=json.loads(_serialize(body,'json'))
        self.assertEqual(len(output['chunks'][0]['rows']),513)
        self.assertEqual(output['chunks'][0]['rows'][-1],['9007199254740993','27021597764222979/7',None,False,'=untrusted'])
        csv_rows=list(csv.DictReader(io.StringIO(_serialize(body,'csv').decode())))
        self.assertEqual(sum(r['field']=='power_index' for r in csv_rows),513)
        self.assertEqual({r['value'] for r in csv_rows if r['field']=='time_ns'},{'27021597764222979/7'})
        self.assertEqual({r['value'] for r in csv_rows if r['field']=='reason'},{"'=untrusted"})

    def test_context_binding_cannot_be_substituted_or_fabricated(self):
        from uuid import uuid4
        from backend.inversion.results import ResultService
        from backend.inversion.exports import ResultExporter, ResultExportContext
        service=ResultService(lambda:None,read_connect=lambda:None,authorize=lambda *args:None)
        exporter=ResultExporter(service)
        context=ResultExportContext(exporter,uuid4(),ACTOR)
        for field,value in (('actor',REVIEWER),('export_id',uuid4()),('exporter',ResultExporter(service))):
            with self.subTest(field=field),self.assertRaises(AttributeError): setattr(context,field,value)
        with self.assertRaises(RuntimeError): iter(context)

class ExportTests(ResultCase):
    def setUp(self):
        if importlib.util.find_spec('backend.inversion.exports') is None:
            self.skipTest('Exporter not implemented yet')
        super().setUp()
        from backend.inversion.exports import ResultExporter
        self.exporter=ResultExporter(self.results)

    def export(self,intent='evidence',format='json',cmd=None):
        return self.exporter.create(self.published.id,intent,format,cmd or command(ACTOR))

    def audit_count(self,action):
        return self.scalar('SELECT count(*) FROM inversion.audit WHERE action=%s',(action,))

    def test_export_audit_failure_yields_zero_bytes(self):
        export=self.export(); context=self.exporter.open(export.id,actor=ACTOR)
        with patch.object(self.exporter,'_audit',side_effect=RuntimeError('audit unavailable')):
            received=b''
            with self.assertRaises(RuntimeError):
                for chunk in context: received+=chunk
        self.assertEqual(received,b'')
        self.assertEqual(self.audit_count('export.stream_finished'),0)

    def test_ambiguous_access_commit_and_transport_failure(self):
        export=self.export(); original=self.results.connect
        class CommitFailure:
            def __init__(self,connection,after): self.connection,self.after=connection,after
            def __enter__(self): self.connection.__enter__(); return self.connection
            def __exit__(self,kind,value,traceback):
                if kind is not None: return self.connection.__exit__(kind,value,traceback)
                if self.after: self.connection.__exit__(None,None,None)
                else: self.connection.rollback(); self.connection.close()
                raise RuntimeError('Lost commit acknowledgment')
        for after in (False,True):
            context=self.exporter.open(export.id,actor=ACTOR); before=self.audit_count('export.access_authorized')
            with patch.object(self.results,'connect',side_effect=lambda:CommitFailure(original(),after)):
                received=b''
                with self.assertRaises(RuntimeError):
                    for chunk in context: received+=chunk
            self.assertEqual(received,b'')
            self.assertEqual(self.audit_count('export.access_authorized'),before+int(after))
            self.assertEqual(self.audit_count('export.stream_finished'),0)
        context=self.exporter.open(export.id,actor=ACTOR); iterator=iter(context); next(iterator)
        with self.assertRaises(OSError): iterator.throw(OSError('Transport disconnected'))
        self.assertEqual(self.audit_count('export.stream_finished'),0)
        self.assertEqual(self.audit_count('export.stream_aborted'),1)

    def test_export_table_is_immutable_and_runtime_grants_are_exact(self):
        import psycopg
        from backend.inversion.privileges import probe_role
        from backend.inversion.migrate import check_schema
        export=self.export()
        for role in ('reader','operator'):
            probe_role(lambda:self.inversion_fixture.connect(role),role)
        with self.inversion_fixture.connect('owner') as conn:
            self.assertEqual(check_schema(conn,expected_database='oilwell_waveforms_test')['version'],'5')
            for statement in ('UPDATE inversion.result_exports SET intent=\'quantitative\' WHERE id=%s',
                              'DELETE FROM inversion.result_exports WHERE id=%s'):
                with self.assertRaises(psycopg.Error),conn.transaction(): conn.execute(statement,(export.id,))
        with self.inversion_fixture.connect('reader') as conn:
            self.assertFalse(conn.execute("SELECT has_table_privilege(current_user,'inversion.result_exports','INSERT')").fetchone()[0])

    def test_evidence_export_is_explicitly_restricted(self):
        with self.assertRaises(RuntimeError): self.export('quantitative')
        self.denied.add('inversion.audit.read')
        with self.assertRaises(RuntimeError): self.export()
        self.denied.clear(); export=self.export()
        body=json.loads(b''.join(self.exporter.open(export.id,actor=ACTOR)))
        self.assertTrue(body['restricted'])
        self.assertIn('evidence_only',body['restriction_reasons'])
        self.assertEqual(body['intent'],'evidence')
        self.assertEqual(body['historical']['review']['state'],'unreviewed')

    def test_actor_session_and_dependency_rechecked_at_first_byte(self):
        self.approve(); export=self.export('quantitative')
        with self.assertRaises(RuntimeError): self.exporter.open(export.id,actor=REVIEWER)
        context=self.exporter.open(export.id,actor=ACTOR); self.session_valid=False
        with self.assertRaises(RuntimeError): next(iter(context))
        self.session_valid=True; context=self.exporter.open(export.id,actor=ACTOR)
        self.revoke_parameter()
        with self.assertRaises(RuntimeError): next(iter(context))
        self.assertEqual(self.audit_count('export.access_authorized'),0)

    def test_identity_replay_and_frozen_review_with_fresh_restrictions(self):
        self.approve(); cmd=command(ACTOR); export=self.export(cmd=cmd)
        self.assertEqual(export,self.export(cmd=cmd))
        self.results.revoke(self.published.id,command(REVIEWER,1))
        body=json.loads(b''.join(self.exporter.open(export.id,actor=ACTOR)))
        self.assertEqual(body['historical']['review']['state'],'approved')
        self.assertIn('result_revoked',body['restriction_reasons'])
        self.assertEqual(self.scalar('SELECT count(*) FROM inversion.result_exports'),1)
        self.denied.add('inversion.export')
        with self.assertRaises(RuntimeError): self.export(cmd=cmd)

    def test_close_partial_stream_never_records_finished(self):
        from backend.inversion.exports import ResultExportContext
        export=self.export(); context=self.exporter.open(export.id,actor=ACTOR)
        with self.assertRaises(RuntimeError): iter(ResultExportContext(self.exporter,export.id,ACTOR))
        iterator=iter(context); self.assertTrue(next(iterator)); context.close()
        self.assertEqual(self.audit_count('export.access_authorized'),1)
        self.assertEqual(self.audit_count('export.stream_finished'),0)
        self.assertEqual(self.audit_count('export.stream_aborted'),1)
        with self.assertRaises(RuntimeError): iter(context)

    def test_full_chunks_empty_metrics_and_partial_support_remain_exact(self):
        export=self.export(); context=self.exporter.open(export.id,actor=ACTOR)
        self.assertEqual(context.format,'json')
        body=json.loads(b''.join(context))
        expected=[json.loads(c) for c in self.prepared_result.chunks]
        self.assertEqual(body['chunks'],expected)
        self.assertEqual(body['result_sha256'],self.published.content_sha256)
        self.assertEqual(body['snapshot_sha256'],self.prepared_result.snapshot.content_sha256)
        self.assertEqual(body['result']['comparison'],self.prepared_result.body['comparison'])
        self.assertEqual(self.audit_count('export.stream_finished'),1)
        self.assertEqual(self.scalar("SELECT count(*) FROM inversion.receipts WHERE response->>'delivery'='successful'"),0)

    def test_csv_formula_injection_and_secret_redaction(self):
        from backend.inversion.exports import safe_text, csv_cell, public_result
        for value in ('=HYPERLINK("bad")','+cmd','-cmd','@SUM(1)','\tcmd','\rcmd','\ncmd'):
            self.assertTrue(csv_cell(value).startswith("'"))
        for value in ('C:\\private\\secret.toml','/home/private/key','postgresql://user:password@host/db','password=secret'):
            self.assertEqual(safe_text(value),'[redacted]')
        metadata=self.prepared_result.body | {'password':'secret','actor':{'email':'private@example.com'},'path':'C:\\private'}
        self.assertNotIn('password',public_result(metadata))
        export=self.export(format='csv'); payload=b''.join(self.exporter.open(export.id,actor=ACTOR)).decode()
        self.assertIn('prediction',payload)
        self.assertNotIn('Run author',payload)
