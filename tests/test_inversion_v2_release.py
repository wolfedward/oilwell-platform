"""Release gates use disposable paths only; no databases or service startup."""
import importlib
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_review_deploy import _add_public_kit_fixture
from tools.review_deploy_paths import claim_target, write_json_private
from tools.review_deploy_source import export_committed_source

REPO=Path(__file__).resolve().parents[1]


def repository(root):
    root.mkdir();subprocess.run(['git','init','-q',str(root)],check=True)
    subprocess.run(['git','-C',str(root),'config','core.autocrlf','false'],check=True)
    _add_public_kit_fixture(root)
    names=['server.py','requirements-production.txt','requirements-inversion-v2.txt',
        'backend/inversion/coverage_sources.py','backend/inversion/synthetic_registry.py',
        'tools/provision_inversion_v2.py','tools/review_deploy_numeric.py','tools/review_deploy_offline.py']
    names += [p.relative_to(REPO).as_posix() for p in (REPO/'tools/testpack').glob('*.py')]
    names += [p.relative_to(REPO).as_posix() for p in (REPO/'config/testpack').glob('*.json')]
    for name in names:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture\n')
    subprocess.run(['git','-C',str(root),'add','.'],check=True)
    subprocess.run(['git','-C',str(root),'-c','user.name=Test','-c','user.email=t@example.test','commit','-qm','fixture'],check=True)
    return names


class ReleaseV2Tests(unittest.TestCase):
    def module(self,name):
        self.assertIsNotNone(importlib.util.find_spec(name),'release implementation missing')
        return importlib.import_module(name)

    def test_package_generation_budget_covers_extended_without_changing_worker_deadline(self):
        module=self.module('tools.testpack.deliver_v2')
        timeout=getattr(module,'generation_timeout',None)
        self.assertTrue(callable(timeout),'profile-specific preparation bound absent')
        self.assertEqual(timeout('smoke'),900)
        self.assertEqual(timeout('extended'),7200)
        with self.assertRaises(ValueError):timeout('unknown')
        from backend.inversion.capacity import InversionLimits
        self.assertEqual(InversionLimits(80*1024**2).deadline_seconds,180)

    def test_release_contains_v2_runtime_and_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);expected=repository(root/'repo')
            exported=export_committed_source(root/'repo',root/'code')
            self.assertTrue(set(expected)<=set(exported['files']),set(expected)-set(exported['files']))
            self.assertIn('backend/inversion/coverage_sources.py',exported['files'])
            self.assertIn('tools/testpack/forward_v2.py',exported['files'])

    def test_dirty_source_existing_target_refused(self):
        module=self.module('tools.testpack.deliver_v2')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);repository(root/'repo')
            existing=root/'existing';existing.mkdir();(existing/'keep').write_text('keep')
            with self.assertRaises(ValueError):module.deliver(existing,root/'repo',profile='smoke')
            (root/'repo/untracked-note.txt').write_text('not committed')
            with self.assertRaises(ValueError):module.deliver(root/'new',root/'repo',profile='smoke')
            self.assertFalse((root/'new').exists());self.assertEqual((existing/'keep').read_text(),'keep')

    def test_missing_proof_stays_blocked(self):
        module=self.module('tools.testpack.deliver_v2')
        report={'status':'passed','checks':[]}
        with self.assertRaises(ValueError):module.require_offline_proofs(report,('cycle-base',))
        report['checks']=[{'check_id':'cycle-base-finite-inversion','status':'passed',
            'actual':{'source_proof_status':'unverified'}}]
        with self.assertRaises(ValueError):module.require_offline_proofs(report,('cycle-base',))

    def test_rollback_disables_only_owned_v2(self):
        module=self.module('tools.testpack.deliver_v2')
        owned={'monitoring':'new-a','review':'new-b'}
        plan=module.rollback_plan({'schema':'owp.testpack.v2-release/1','instances':owned},owned)
        self.assertLessEqual(set(plan),set(owned.values()))
        with self.assertRaises(ValueError):
            module.rollback_plan({'schema':'owp.testpack.v2-release/1','instances':owned},dict(owned,review='old-instance'))

    def test_export_provenance_is_owned_and_byte_verified(self):
        from tools.testpack import storage
        self.assertTrue(callable(getattr(storage,'generator_commit_v2',None)))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);repository(root/'repo')
            stage=root/'stage';marker=claim_target(stage,forbidden=(root/'repo',))
            marker['source']=export_committed_source(root/'repo',stage/'code')
            write_json_private(stage/'private/marker.json',marker)
            self.assertEqual(storage.generator_commit_v2(stage/'code'),marker['source']['commit'])
            (stage/'code/unlisted.txt').write_text('extra')
            with self.assertRaises(ValueError):storage.generator_commit_v2(stage/'code')

    def test_native_readiness_compares_actual_inventory(self):
        module=self.module('tools.review_deploy_numeric')
        wheel='1f3a3672d589e376d47288dcebcb712997dc719880a4dbf1779bf0f2b20d0293'
        installed={'inventory':{'wheel_sha256':wheel,'native':{'a.pyd':'a'*64},'licenses':{'license':'b'*64}}}
        actual={'approved_wheel_sha256':wheel,'native_files':[{'name':'a.pyd','sha256':'a'*64}],
                'licenses':[{'name':'license','sha256':'b'*64}]}
        self.assertEqual(module.compare_inventory(installed,actual)['installed_wheel_sha256'],wheel)
        actual['native_files'][0]['sha256']='c'*64
        with self.assertRaises(ValueError):module.compare_inventory(installed,actual)

    def test_live_completeness_cannot_hide_missing_negative_or_extended_case(self):
        module=self.module('tools.testpack.owned_v2_verify')
        self.assertTrue(callable(getattr(module,'require_complete',None)))
        from tools.testpack.inversion_http_v2 import InversionOutcome
        one=InversionOutcome('cycle-base','passed','completed','completed','export','','passed',{})
        with self.assertRaises(ValueError):module.require_complete((one,),('cycle-base','variant-000'))

    def test_offline_placeholder_does_not_prevent_actual_http_validation(self):
        from tools.testpack import inversion_run_v2
        self.assertTrue(callable(getattr(inversion_run_v2,'require_offline_checks',None)))
        from tools.testpack.contracts import CheckResult
        checks=(CheckResult('fixture','offline','passed',{},{}),CheckResult('studies-http','http','not_run',{},{}))
        inversion_run_v2.require_offline_checks(checks)
        with self.assertRaises(ValueError):
            inversion_run_v2.require_offline_checks((CheckResult('fixture','numeric','failed',{},{}),))

    def test_resource_settlement_and_deadline_are_not_inferred_from_http_status(self):
        module=self.module('tools.testpack.resources_v2')
        row={'state':'completed','request_bytes':100,'snapshot_bytes':200,'metadata_bytes':300,
             'chunk_bytes':400,'input_transport_bytes':310,'result_transport_bytes':724,
             'reservation_state':'settled','used_bytes':1000,'deadline_seconds':180,
             'attempt_seconds':41,'terminal_before_deadline':True,'attachment_bytes':[1024]}
        module.require_run_resources(row)
        for key,value in (('used_bytes',999),('attempt_seconds',181),('terminal_before_deadline',False),
                          ('input_transport_bytes',67108865),('attachment_bytes',[4194305])):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):module.require_run_resources(dict(row,**{key:value}))

    def test_credential_free_child_does_not_mutate_immutable_source_inventory(self):
        from unittest.mock import patch
        from backend.inversion import worker
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);code=root/'code';package=code/'backend/inversion';package.mkdir(parents=True)
            (code/'backend/__init__.py').write_text('')
            (package/'__init__.py').write_text('')
            (package/'compute_child.py').write_text('def compute(source,target):\n    target.write_bytes(source.read_bytes())\n')
            source=root/'input.json';source.write_bytes(b'owned input');target=root/'result.bin'
            with patch.object(worker,'__file__',str(package/'worker.py')):
                child=worker.launch_compute(source,target)
                try:self.assertEqual(child.wait(timeout=10),0)
                finally:
                    if child.poll() is None:child.kill();child.wait(timeout=3)
            self.assertEqual(target.read_bytes(),b'owned input')
            self.assertEqual(list(code.rglob('*.pyc')),[],'isolated child wrote unlisted release bytes')


if __name__=='__main__':unittest.main()
