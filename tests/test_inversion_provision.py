"""Safety gates must precede secrets and side effects."""
import importlib.util
import unittest
from unittest.mock import patch
from tools.provision_waveform_test import ProvisionError


class ProvisionTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('tools.provision_inversion_test'),
                             'isolated inversion provisioner is required')
        from tools import provision_inversion_test
        return provision_inversion_test

    def test_unpinned_output_never_resolves_credentials(self):
        m = self.module()
        with patch.object(m, 'read_windows_credential', side_effect=AssertionError('secret accessed')):
            with self.assertRaises(ProvisionError):
                m.provision(output='C:/unowned', expected_system_identifier='123')

    def test_missing_system_identifier_never_resolves_credentials(self):
        m = self.module()
        with patch.object(m, 'read_windows_credential', side_effect=AssertionError('secret accessed')):
            with self.assertRaises(ProvisionError): m.provision(expected_system_identifier='')

    def test_wrong_cluster_causes_zero_ddl(self):
        m = self.module()
        class WrongCluster:
            def execute(self, statement, params=None):
                if statement == 'SELECT system_identifier::text FROM pg_control_system()':
                    return self
                raise AssertionError('query beyond system identity gate: ' + statement)
            def fetchone(self): return ('999',)
        with self.assertRaises(ProvisionError):
            m.verify_live(WrongCluster(), {}, {}, expected_system_identifier='123')

    def test_no_escalation_of_receiver(self):
        m = self.module()
        parent = dict(fixture_id='a'*32, data='C:/pinned/data', roles={'migration_owner':'parent_owner','receiver':'receiver'})
        sidecar = m.sidecar_identity(parent, '123')
        self.assertEqual(set(sidecar['roles']), {'owner','reader','operator'})
        self.assertNotIn('receiver', sidecar['roles'].values())
        self.assertNotIn('parent_owner', sidecar['roles'].values())

    def test_admin_role_gate_rejects_changed_authority(self):
        m=self.module()
        class AdminChanged:
            def execute(self,statement,params=None):
                self.statement=statement
                return self
            def fetchone(self):
                if 'system_identifier' in self.statement: return ('123',)
                if 'current_user' in self.statement: return ('pinned_admin',)
                if 'pg_roles' in self.statement: return (False,False)
                if 'pg_database' in self.statement: return ('parent_owner','oilwell-waveform-test:'+'a'*32)
                raise AssertionError(self.statement)
        identity={'roles':{'admin':'pinned_admin'}}
        parent={'roles':{'migration_owner':'parent_owner'},'fixture_id':'a'*32}
        with patch.object(m,'_verify_live'):
            with self.assertRaises(ProvisionError):
                m.verify_live(AdminChanged(),identity,parent,expected_system_identifier='123')
