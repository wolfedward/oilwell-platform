"""Large retained evidence must remain lossless, bounded and fail closed."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.testpack.contracts import CheckResult, PackConfig
from tools.testpack.storage import write_pack
from tools.testpack.report import write_report, export_public_report
from tests.test_testpack_contracts import example, GENERATOR


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False)+'\n').encode()


class ShardedReportTests(unittest.TestCase):
    def small_sharded(self,root,*,v2=False):
        run=root/'run';run.mkdir()
        pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR)
        checks=[CheckResult('row-'+str(i),'numeric','passed',{},
                {'points':[{'index':i,'exact':'7/9','private':'x'*6000}]}) for i in range(2)]
        from tests.test_testpack_report_v2 import positive
        with patch('tools.testpack.report_evidence.MAX_FILE_BYTES',8192):
            path=write_report(pack,checks,run,run_metadata={},inversion_outcomes=(positive(),) if v2 else None)
        return run,path,json.loads(path.read_bytes())

    def test_large_evidence_round_trip_is_lossless_and_not_whole_project_pass(self):
        # Break caught: one >32 MiB envelope makes a completed scenario run unexportable.
        with TemporaryDirectory() as folder:
            root=Path(folder); run=root/'run'; run.mkdir()
            pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR)
            checks=[CheckResult('large-'+str(i),'numeric','passed',{},
                    {'points':[{'index':i,'exact':'7/9','private':'x'*(17*1024*1024)}]})
                    for i in range(2)]
            path=write_report(pack,checks,run,run_metadata={})
            try: output=export_public_report(run,root/'public')
            except ValueError as exc: self.fail('large evidence must export without relaxing limits: '+str(exc))
            public=json.loads(output.read_bytes()); record=json.loads(path.read_bytes())
            self.assertEqual(public['status'],'incomplete')
            self.assertEqual(sum(c['status']=='not_run' for c in public['checks']),5)
            self.assertEqual(record['schema'],'owp.testpack.private-report/2')
            rows=[json.loads((run/r['path']).read_bytes()) for r in record['evidence']['checks']]
            self.assertEqual(rows[:2],json.loads(encoded([asdict(c) for c in checks])))
            expected_evidence={'checks':rows,'run_metadata':{}}
            self.assertEqual(public['private_evidence_sha256'],sha256(encoded(expected_evidence)).hexdigest())
            self.assertTrue(all(p.stat().st_size<=32*1024*1024 for p in run.rglob('*') if p.is_file()))
            self.assertFalse((run/'.report-incomplete').exists())
            self.assertLess(output.stat().st_size,10000)

    def test_single_oversized_check_rejected_before_writing_any_report(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);run=root/'run';run.mkdir()
            pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR)
            check=CheckResult('oversized','numeric','passed',{}, {'private':'x'*(32*1024*1024)})
            with self.assertRaises(ValueError):write_report(pack,[check],run,run_metadata={})
            self.assertEqual(list(run.iterdir()),[])

    def test_shard_tamper_missing_duplicate_escape_and_extra_rejected_without_public(self):
        # Each mutation invalidates a real stored report, not a mock's assertion.
        for change in ('tamper','missing','duplicate','traversal','extra','hardlink','wrong_size','wrong_sha',
                       'bool_size','unknown_field','private_changed_rehashed','public_changed','incomplete'):
            with self.subTest(change=change),TemporaryDirectory() as folder:
                root=Path(folder);run,path,record=self.small_sharded(root)
                refs=record['evidence']['checks'];first=run/refs[0]['path']
                if change=='tamper':first.write_bytes(first.read_bytes()+b' ')
                elif change=='missing':first.unlink()
                elif change=='duplicate':refs[1]=refs[0]
                elif change=='traversal':refs[0]['path']='../outside.json'
                elif change=='extra':(run/'report-evidence/extra.json').write_bytes(b'{}')
                elif change=='hardlink':(root/'linked.json').hardlink_to(first)
                elif change=='wrong_size':refs[0]['bytes']+=1
                elif change=='wrong_sha':refs[0]['sha256']='0'*64
                elif change=='bool_size':refs[0]['bytes']=True
                elif change=='unknown_field':refs[0]['other']='wrong'
                elif change=='private_changed_rehashed':
                    body=json.loads(first.read_bytes());body['actual']['points'][0]['exact']='8/9'
                    raw=encoded(body);first.write_bytes(raw);refs[0].update(bytes=len(raw),sha256=sha256(raw).hexdigest())
                elif change=='public_changed':record['public']['checks'][0]['actual']['counts']['frame_count']=999
                elif change=='incomplete':(run/'.report-incomplete').write_bytes(b'failed')
                path.write_bytes(encoded(record))
                with self.assertRaises(ValueError):export_public_report(run,root/'public')
                self.assertFalse((root/'public').exists())

    def test_aggregate_budget_is_preflight_and_oversized_declared_total_is_rejected(self):
        from tools.testpack import report_evidence
        with TemporaryDirectory() as folder:
            root=Path(folder);run,path,record=self.small_sharded(root)
            new=root/'new';new.mkdir()
            retained=[json.loads((run/r['path']).read_bytes()) for r in record['evidence']['checks']]
            evidence=record['evidence']|{'checks':retained}
            with patch.object(report_evidence,'MAX_TOTAL_BYTES',10000):
                with self.assertRaises(ValueError):report_evidence.write_private(new,record['public'],evidence)
                self.assertEqual(list(new.iterdir()),[])
                with self.assertRaises(ValueError):export_public_report(run,root/'public')
                self.assertFalse((root/'public').exists())

    def test_partial_write_kept_nonexportable_and_cannot_be_resumed(self):
        from tools.testpack import report_evidence
        with TemporaryDirectory() as folder:
            root=Path(folder);run,path,record=self.small_sharded(root)
            evidence=record['evidence']|{'checks':[json.loads((run/r['path']).read_bytes()) for r in record['evidence']['checks']]}
            new=root/'new';new.mkdir();original_open=Path.open
            def fail_second(path,*args,**kwargs):
                if path==new/'report-evidence/check-00001.json':raise OSError('controlled storage failure')
                return original_open(path,*args,**kwargs)
            with patch.object(report_evidence,'MAX_FILE_BYTES',8192),patch.object(Path,'open',fail_second):
                with self.assertRaises(OSError):report_evidence.write_private(new,record['public'],evidence)
            self.assertTrue((new/'.report-incomplete').is_file())
            self.assertTrue((new/'report-evidence/check-00000.json').is_file())
            self.assertFalse((new/'report.json').exists())
            with self.assertRaises(ValueError):export_public_report(new,root/'public')
            with self.assertRaises(ValueError):report_evidence.write_private(new,record['public'],evidence)

    def test_sharded_v2_preserves_outcomes_and_complete_evidence_digest(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);run,path,record=self.small_sharded(root,v2=True)
            public=json.loads(export_public_report(run,root/'public').read_bytes())
            rows=[json.loads((run/r['path']).read_bytes()) for r in record['evidence']['checks']]
            expected=record['evidence']|{'checks':rows}
            self.assertEqual(public['private_evidence_sha256'],sha256(encoded(expected)).hexdigest())
            self.assertEqual(public['inversion_outcomes'][0]['approved_execution_status'],'passed')
            self.assertEqual(public['status'],'incomplete')
            self.assertFalse(public['inversion_outcomes'][0]['field_accuracy_certified'])

    def test_legacy_small_report_stays_compatible_and_exclusive(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);run=root/'run';run.mkdir()
            pack=write_pack(root/'pack',PackConfig(),(example(),),GENERATOR)
            path=write_report(pack,[],run,run_metadata={});raw=path.read_bytes()
            self.assertEqual(json.loads(raw)['schema'],'owp.testpack.private-report/1')
            self.assertFalse((run/'report-evidence').exists())
            export_public_report(run,root/'public')
            with self.assertRaises(ValueError):write_report(pack,[],run,run_metadata={})
            self.assertEqual(path.read_bytes(),raw)

    def test_exported_runtime_contains_shard_reader(self):
        from tools.review_deploy_source import _included
        self.assertTrue(_included('tools/testpack/report_evidence.py'))


if __name__=='__main__':unittest.main()
