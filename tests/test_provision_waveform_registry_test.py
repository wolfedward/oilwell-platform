"""Sidecar refusal gates run before credentials, connections or mutations."""
from pathlib import Path
from unittest.mock import patch
import unittest
from tools.provision_waveform_test import ProvisionError


class RegistrySidecarSafetyTests(unittest.TestCase):
    def test_unpinned_path_never_looks_up_credentials(self):
        from tools.provision_waveform_registry_test import provision
        with patch('tools.provision_waveform_registry_test.read_windows_credential',
                   side_effect=AssertionError('unexpected credential resolution')):
            with self.assertRaises(ProvisionError): provision('C:/unowned/identity.json')

    def test_sidecar_identity_pins_parent_and_separate_owner(self):
        from tools.provision_waveform_registry_test import sidecar_identity
        parent=dict(fixture_id='a'*32, database='oilwell_waveforms_test',host='127.0.0.1',port=3822,
                    data='C:/pinned/data',roles={'migration_owner':'waveform_test_'+'a'*16+'_migration_owner'})
        value=sidecar_identity(parent)
        self.assertNotEqual(value['role'], parent['roles']['migration_owner'])
        self.assertEqual(value['role'],'waveform_test_'+'a'*16+'_registry_owner')
        self.assertEqual(value['parent_fixture_id'], 'a'*32)
        self.assertEqual(value['port'],3822)
