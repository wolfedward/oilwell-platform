"""Public mechanical history projection; I/O doubles complement the live HTTP case."""
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock
from uuid import UUID
from backend.inversion.queries import InversionQueries
from backend.inversion.transactions import InversionError


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
