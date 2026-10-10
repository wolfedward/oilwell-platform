"""Public mechanical history projection; I/O doubles complement the live HTTP case."""
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock
from uuid import UUID
from backend.inversion.queries import InversionQueries
from backend.inversion.transactions import InversionError


class DiagnosticTests(unittest.TestCase):
    def run_record(self,**changes):
        from types import SimpleNamespace
        return SimpleNamespace(**(dict(id=UUID(int=1),version=3,state='blocked',reason_code='not_applicable')|changes))

    def test_terminal_diagnostics_expose_only_bounded_allowlisted_facts(self):
        from backend.inversion.queries import terminal_diagnostics
        from backend.inversion.contracts import canonical_bytes
        run=self.run_record()
        details=dict(reasons=['insufficient_coverage'],coverage_fraction='1/3',point_count='3',valid_point_count='2',
                     raw_input='secret',session='secret',path='C:/secret')
        # Result metadata counts are native ints, even though point coordinates
        # and fractions remain exact strings in persisted numerical rows.
        details.update(point_count=3,valid_point_count=2)
        event=dict(reason_code='not_applicable',details=details,private='secret')
        result=terminal_diagnostics(run,event)
        self.assertEqual(result,dict(run_id=str(run.id),version=3,state='blocked',reason_code='not_applicable',
            reasons=['insufficient_coverage'],coverage_fraction='1/3',point_count=3,valid_point_count=2))
        self.assertNotIn(b'secret',canonical_bytes(result)); self.assertLessEqual(len(canonical_bytes(result)),8192)
        self.assertIn('raw_input',event['details'])
        for updates in (dict(reasons=['C:/secret']),dict(reasons=['insufficient_coverage']*33),
            dict(point_count=True),dict(point_count=32769),dict(valid_point_count=4),
            dict(coverage_fraction='2/1'),dict(coverage_fraction='01/3'),dict(coverage_fraction='1/0'),
            dict(coverage_fraction='1'*257),dict(coverage_fraction='0.5')):
            self.assertIsNone(terminal_diagnostics(run,dict(reason_code='not_applicable',details=details|updates)))
        for event in (None,{},dict(reason_code='input_changed',details=details),dict(reason_code='not_applicable',details=[])):
            self.assertIsNone(terminal_diagnostics(run,event))
        self.assertIsNone(terminal_diagnostics(self.run_record(state='running'),dict(reason_code='not_applicable',details=details)))

    def test_input_change_diagnostics_and_exact_current_event_lookup(self):
        from backend.inversion.queries import terminal_diagnostics
        run=self.run_record(reason_code='input_changed')
        event=dict(reason_code='input_changed',details=dict(reasons=['parameter_pin_changed']))
        self.assertEqual(terminal_diagnostics(run,event)['reasons'],['parameter_pin_changed'])
        conn=MagicMock(); conn.__enter__.return_value=conn; conn.execute.return_value.fetchone.return_value=(event,)
        self.assertEqual(InversionQueries(lambda:conn).run_diagnostics(run),terminal_diagnostics(run,event))
        statement,args=conn.execute.call_args.args
        self.assertIn('e.version=s.version',statement); self.assertIn('e.state=s.state',statement)
        self.assertIn('octet_length',statement); self.assertIn('LIMIT 1',statement)
        self.assertEqual(args,(run.id,3,'blocked','input_changed','input_changed'))
        conn.execute.return_value.fetchone.return_value=None
        self.assertIsNone(InversionQueries(lambda:conn).run_diagnostics(run))


class ReadQueryTests(unittest.TestCase):
    def connection(self, rows, exists=True):
        conn=MagicMock(); conn.__enter__.return_value=conn
        conn.execute.return_value.fetchone.return_value=(1,) if exists else None
        conn.execute.return_value.fetchall.return_value=rows
        return conn

    def test_history_allowlists_declaration_without_overwriting_its_dates(self):
        assembly=UUID(int=1); version=UUID(int=2)
        start=datetime(2026,1,1,tzinfo=timezone.utc); end=datetime(2026,7,1,tzinfo=timezone.utc)
        body=dict(assembly_id=str(assembly),valid_from='2026-01-01T00:00:00.000000Z',valid_to=None,
                  manufacturer='Known',source_organization='password=secret',actor={'id':'private'},
                  unexpected='not public',installation_evidence=[{'id':'evidence','private_key':'secret'}])
        conn=self.connection([(version,1,'a'*64,body,2,start,end)])
        result=InversionQueries(lambda:conn).fetch('assembly_versions.list',assembly,offset=0,limit=1)
        self.assertEqual((result['assembly_id'],result['offset'],result['limit']),(str(assembly),0,1))
        row=result['items'][0]
        self.assertEqual(set(row),{'assembly_id','version_id','version','content_sha256','declared','effective'})
        self.assertEqual((row['version_id'],row['content_sha256']),(str(version),'a'*64))
        self.assertTrue(row['declared']['interval_known']); self.assertIsNone(row['declared']['valid_to'])
        self.assertEqual(row['declared']['body'],dict(assembly_id=str(assembly),valid_from=body['valid_from'],valid_to=None,
                         manufacturer='Known',source_organization='[redacted]',installation_evidence=[{'id':'evidence'}]))
        self.assertEqual(row['effective'],dict(revision=2,valid_from=start.isoformat(),valid_to=end.isoformat()))

    def test_missing_assembly_is_not_an_empty_high_offset_history(self):
        conn=self.connection([],exists=False)
        with self.assertRaises(InversionError) as error:
            InversionQueries(lambda:conn).fetch('assembly_versions.list',UUID(int=1),offset=100000)
        self.assertEqual((error.exception.code,error.exception.status),('inversion.assembly_missing',404))

    def test_parameter_filter_uses_bounded_descending_version_sql(self):
        assembly=UUID(int=1); parameter=UUID(int=2)
        conn=self.connection([(parameter,assembly,1,7,'a'*64,2,'submit')])
        result=InversionQueries(lambda:conn).fetch('parameters.list',offset=0,limit=1,assembly_id=assembly)
        self.assertEqual(result['items'][0]['version'],7)
        statement,args=conn.execute.call_args_list[-1].args
        self.assertIn('WHERE p.assembly_id=%s',statement)
        self.assertIn('ORDER BY p.version DESC',statement)
        self.assertEqual(args,(assembly,1,0))
