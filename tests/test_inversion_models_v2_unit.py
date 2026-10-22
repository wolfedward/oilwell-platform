"""Installed version dispatch without database fixtures or native fallback."""
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from unittest.mock import patch
import unittest

from backend.inversion.models import InstalledModel, INSTALLED_MODELS
from backend.inversion.transactions import InversionError


class InstalledV2Tests(unittest.TestCase):
    def module(self):
        try: return import_module('backend.inversion.model_manifest_v2')
        except ModuleNotFoundError: self.fail('Explicit installed v2 manifest missing')

    def test_v2_is_installed_with_actual_runtime_and_source_hashes(self):
        model=self.module().installed_v2()
        self.assertIs(type(model),InstalledModel)
        self.assertIn(model.installed_id,INSTALLED_MODELS)
        result=model.snapshot()
        self.assertEqual(result['algorithm'],'beam_energy_balance/2')
        self.assertEqual(result['parameter_schema'],'inversion.parameter-set/2')
        files={item['name']:item['sha256'] for item in result['files']}
        for name in ('solver_v2.py','coverage_sources.py','motion_bounds.py','interval_runtime.py','synthetic_registry.py'):
            path=Path(__file__).parents[1]/'backend/inversion'/name
            self.assertEqual(files[name],sha256(path.read_bytes()).hexdigest())
        numeric=result['dependencies']['finite_input_runtime']
        self.assertEqual(numeric['runtime']['precision'],128)
        self.assertEqual(numeric['runtime']['threads'],1)
        self.assertTrue(numeric['native_files'])
        self.assertTrue(numeric['licenses'])
        self.assertTrue(all(len(row['sha256'])==64 for row in numeric['native_files']))

    def test_installed_v1_snapshot_unchanged_and_no_native_dependency(self):
        before=INSTALLED_MODELS['beam_energy_balance/1'].snapshot()
        self.module()
        with patch('backend.inversion.interval_runtime.require_runtime',side_effect=InversionError('proof_runtime_unavailable')):
            after=INSTALLED_MODELS['beam_energy_balance/1'].snapshot()
            self.assertEqual(after,before)
            self.assertEqual(after['algorithm'],'beam_energy_balance/1')
            self.assertEqual(after['parameter_schema'],'inversion.parameter-set/1')
            with self.assertRaises(InversionError) as caught:
                self.module().installed_v2().snapshot()
            self.assertEqual(caught.exception.code,'inversion.proof_runtime_unavailable')

    def test_changed_installed_source_is_not_silently_approved(self):
        model=self.module().installed_v2()
        before=model.snapshot()
        original=Path.read_bytes
        def changed(path):
            data=original(path)
            return data+b'\n# changed code' if path.name=='solver_v2.py' else data
        with patch.object(Path,'read_bytes',changed):
            self.assertNotEqual(model.snapshot(),before)

