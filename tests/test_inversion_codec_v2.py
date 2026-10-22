from dataclasses import replace
from fractions import Fraction as Q
from importlib import import_module
from pathlib import Path
import json
import tempfile
import unittest

from backend.inversion.contracts import canonical_bytes
from backend.inversion.transactions import InversionError


class CodecV2Tests(unittest.TestCase):
    def module(self):
        try:return import_module('backend.inversion.result_codec_v2')
        except ModuleNotFoundError:self.fail('versioned proof result codec missing')

    def prepared(self):
        module=self.module()
        from tests.inversion_result_v2_support import calculated
        return module.encode_v2(*calculated())

    def test_v2_exact_proof_roundtrip(self):
        item=self.prepared()
        from backend.inversion.result_dispatch import write_result,read_result
        from tests.inversion_result_v2_support import rows,calculated
        predicted=rows(item,'prediction');proof=rows(item,'proof');original=calculated()[0]
        self.assertEqual(len(predicted),1025);self.assertEqual(len(proof),1025)
        self.assertEqual([p['power_index'] for p in predicted],[str(i) for i in range(1025)])
        self.assertEqual([p['power_index'] for p in proof],[str(i) for i in range(1025)])
        for p,a in zip(proof,original.evidence):
            self.assertEqual(p['time_ns'],str(a.motion.time_ns))
            self.assertEqual(p['angle_indices'],[str(i) for i in a.motion.angle_indices])
            self.assertEqual(p['physical_N_error_Nm'],None if a.physical_N_error_Nm is None else str(a.physical_N_error_Nm))
        self.assertTrue(any(p['load_N'] is None for p in predicted))
        self.assertTrue(all(p['load_N'] is None for p in predicted if p['status']!='valid'))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'result.bin';write_result(item,path);again=read_result(path,item.snapshot)
            self.assertEqual(again.content_sha256,item.content_sha256);self.assertEqual(again.chunks,item.chunks)
            self.assertLessEqual(path.stat().st_size,16*1024**2)
        self.assertFalse(item.body['field_accuracy_certified'])
        self.assertEqual(item.body['source_proof']['status'],'verified')
        self.assertNotIn('"_seal":',item.metadata.decode());self.assertNotIn('"_inputs":',item.metadata.decode())
        print('v2 result bytes',item.result_bytes,flush=True)

    def test_bad_schema_or_chunk_rejected(self):
        item=self.prepared();module=self.module()
        for mode in ('schema','unknown_column','duplicate','missing_proof','fake_valid','nan','index','proof_time','source_digest','proof_budget','wrong_window'):
            meta=item.body;chunks=list(item.chunks)
            if mode=='schema':meta['schema']='inversion.result/999'
            elif mode=='source_digest':meta['source_proof']['evidence_sha256']='a'*64
            elif mode=='duplicate':chunks.append(chunks[0])
            elif mode=='missing_proof':chunks=[c for c in chunks if json.loads(c)['kind']!='proof']
            else:
                idx=next(i for i,c in enumerate(chunks) if json.loads(c)['kind']==('proof' if mode in ('proof_time','proof_budget','wrong_window') else 'prediction'))
                block=json.loads(chunks[idx]);columns=block['columns']
                if mode=='unknown_column':columns[0]='invented'
                elif mode=='fake_valid':block['rows'][0][columns.index('status')]='valid'
                elif mode=='nan':block['rows'][100][columns.index('load_N')]='NaN'
                elif mode=='index':block['rows'][1][columns.index('power_index')]='0'
                elif mode=='proof_time':block['rows'][1][columns.index('time_ns')]='1'
                elif mode=='proof_budget':block['rows'][100][columns.index('physical_N_error_Nm')]='101'
                elif mode=='wrong_window':block['rows'][100][columns.index('angle_indices')]=[str(i) for i in range(7)]
                chunks[idx]=canonical_bytes(block)
            with self.subTest(mode=mode),self.assertRaises(InversionError):
                module.PreparedResultV2(item.snapshot,canonical_bytes(meta),tuple(chunks))

    def test_aggregate_16mib_limit(self):
        item=self.prepared();meta=item.body;meta['padding']='x'*(16*1024**2)
        with self.assertRaises(InversionError) as caught:
            self.module().PreparedResultV2(item.snapshot,canonical_bytes(meta),item.chunks)
        self.assertEqual(caught.exception.code,'inversion.result_size_limit')

    def test_filtered_native_and_proof_pages_cover_all_points(self):
        from types import SimpleNamespace
        from backend.inversion.queries import point_page
        from backend.inversion.routes import query
        item=self.prepared();view=SimpleNamespace(id='result',content_sha256=item.content_sha256,chunks=item.chunks)
        for kind in ('prediction','proof'):
            first=point_page(view,**query('offset=0&limit=1000&kind='+kind,'results.points'))
            last=point_page(view,**query('offset=1000&limit=1000&kind='+kind,'results.points'))
            self.assertEqual((first['total'],len(first['rows']),len(last['rows'])),(1025,1000,25))
            points=[dict(zip(r['columns'],r['values'])) for r in first['rows']+last['rows']]
            self.assertEqual([p['power_index'] for p in points],[str(i) for i in range(1025)])
        self.assertGreater(point_page(view,0,1000)['total'],1025)

    def test_v2_metadata_reaches_public_results_without_private_seal(self):
        item=self.prepared()
        from backend.inversion.exports import public_result
        public=public_result(item.body)
        self.assertEqual(public['proof_columns'],item.body['proof_columns'])
        self.assertEqual(public['source_proof'],item.body['source_proof'])

    def test_v2_terminal_source_refusal_is_public_but_unknown_text_is_not(self):
        from types import SimpleNamespace as N
        from backend.inversion.queries import terminal_diagnostics
        run=N(id='run',version=3,state='blocked',reason_code='not_applicable')
        details=dict(reasons=['coverage_basis_mismatch','no_valid_points','insufficient_coverage'],
            point_count=1025,valid_point_count=0,coverage_fraction='0')
        result=terminal_diagnostics(run,dict(reason_code='not_applicable',details=details))
        self.assertIsNotNone(result,'v2 refusal silently disappeared')
        self.assertEqual(result['reasons'],details['reasons'])
        details['reasons']=['private_or_unknown_reason']
        self.assertIsNone(terminal_diagnostics(run,dict(reason_code='not_applicable',details=details)))
