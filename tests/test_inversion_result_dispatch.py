from importlib import import_module
from pathlib import Path
import tempfile
import unittest

class DispatchTests(unittest.TestCase):
    def module(self):
        try:return import_module('backend.inversion.result_dispatch')
        except ModuleNotFoundError:self.fail('explicit result version dispatcher missing')

    def test_v1_roundtrip_unchanged(self):
        module=self.module()
        from backend.inversion.result_codec import encode,write,read
        from backend.inversion.comparison import compare
        from tests.test_inversion_comparison import prediction,reference,policy
        from tests.test_inversion_worker import CodecTests
        p=prediction();item=encode(p,compare(p,reference(),policy()),CodecTests().snapshot())
        with tempfile.TemporaryDirectory() as directory:
            old=Path(directory)/'old';new=Path(directory)/'new';write(item,old);module.write_result(item,new)
            self.assertEqual(old.read_bytes(),new.read_bytes())
            self.assertEqual(module.read_result(new,item.snapshot),read(old,item.snapshot))
        self.assertEqual(module.decode_result(item.snapshot,item.metadata,item.chunks),item)

    def test_duck_types_and_unknown_versions_refused(self):
        module=self.module()
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.transactions import InversionError
        from tests.test_inversion_worker import CodecTests
        from types import SimpleNamespace
        with self.assertRaises((InversionError,ValueError)):module.validate_result(SimpleNamespace(status='applicable'))
        with self.assertRaises(InversionError):module.decode_result(CodecTests().snapshot(),canonical_bytes({'schema':'inversion.result/3'}),())

    def test_truncated_or_oversized_frames_are_rejected_before_allocation(self):
        import struct
        from backend.inversion.transactions import InversionError
        from tests.test_inversion_worker import CodecTests
        module=self.module();snapshot=CodecTests().snapshot()
        for raw,code in ((b'abc','inversion.result_truncated'),(struct.pack('>Q',16*1024**2),'inversion.result_size_limit'),
                         (struct.pack('>Q',6)+b'abc','inversion.result_truncated')):
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'bad';path.write_bytes(raw)
                with self.assertRaises(InversionError) as caught:module.read_result(path,snapshot)
                self.assertEqual(caught.exception.code,code)

    def test_database_read_dispatch_and_revocation_leave_original_sha(self):
        module=self.module()
        from hashlib import sha256
        from types import SimpleNamespace as N
        from unittest.mock import patch
        from uuid import uuid4
        from backend.inversion.contracts import canonical_bytes
        from backend.inversion.result_codec_v2 import encode_v2
        from backend.inversion.results import ResultService
        from backend.inversion.exports import ResultExporter
        from backend.inversion.transactions import InversionError
        from tests.inversion_result_v2_support import calculated
        item=encode_v2(*calculated());run=uuid4();id=uuid4()
        class Connection:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def cursor(self):return self
            def execute(self,sql,args=()):
                if sql.startswith('SET '):return self
                if sql.startswith('SELECT r.run_id'):
                    self.answer=(run,item.snapshot.body,item.body,item.content_sha256,'completed',{'domain':'test','user_id':'author'},'a'*64)
                elif sql.startswith('SELECT count(*)'):self.answer=(len(item.chunks),sum(map(len,item.chunks)))
                elif sql.startswith('SELECT ordinal'):self.answer=[(i,c,sha256(c).hexdigest()) for i,c in enumerate(item.chunks)]
                else:raise AssertionError(sql)
                return self
            def fetchone(self):return self.answer
            def __iter__(self):return iter(self.answer)
        service=ResultService(Connection,read_connect=Connection,authorize=lambda *_:None)
        stored=service._load(id);self.assertEqual(stored.content_sha256,item.content_sha256)
        with patch.object(service,'_review',return_value={'state':'approved','version':1,'history':[]}),\
                patch('backend.inversion.results.recheck_with_cursor',return_value=('parameter_pin_changed',)):
            view=service._view(None,stored)
        self.assertEqual(view.current_applicability['state'],'restricted')
        with self.assertRaises(InversionError) as caught:ResultExporter._eligibility(view,'quantitative')
        self.assertEqual(caught.exception.code,'inversion.quantitative_export_restricted')
        self.assertIn('evidence_only',ResultExporter._eligibility(view,'evidence'))
        self.assertEqual(stored.prepared.content_sha256,item.content_sha256)
