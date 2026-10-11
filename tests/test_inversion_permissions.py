import tempfile
import unittest
from pathlib import Path
from backend.database import Database
from backend.seed import bootstrap_local_auth


class PermissionTests(unittest.TestCase):
    def test_selection_history_is_read_only_without_management_grant(self):
        from backend.inversion.permissions import required_permissions, ROLE_PERMISSIONS
        reader=frozenset(ROLE_PERMISSIONS['production_engineer'])
        self.assertTrue(required_permissions('selections.list') <= reader)
        self.assertTrue(required_permissions('assembly_versions.list') <= reader)
        for action in ('selections.create','selections.rollback'):
            self.assertFalse(required_permissions(action) <= reader)
            self.assertEqual(required_permissions(action),frozenset({'inversion.models.manage'}))

    def test_preexisting_definitions_upgrade_once_including_admin(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Database(Path(directory)/'auth.db'); db.migrate(); bootstrap_local_auth(db)
            with db.transaction(None,'test.old_marker','role',None) as conn:
                conn.execute('UPDATE auth_bootstrap_state SET bootstrap_version=10')
                conn.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code LIKE 'inversion.%')")
            bootstrap_local_auth(db)
            self.assertEqual(db.scalar("SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id JOIN roles r ON r.id=rp.role_id WHERE r.code='administrator' AND p.code LIKE 'inversion.%'"),9)
            with db.transaction(None,'test.revoke','role',None) as conn:
                conn.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code LIKE 'inversion.%')")
                conn.execute("DELETE FROM permissions WHERE code='inversion.run'")
            bootstrap_local_auth(db)
            self.assertEqual(db.scalar("SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id WHERE p.code LIKE 'inversion.%'"),0)

    def test_new_permissions_granted_once_not_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / 'auth.db'); db.migrate(); bootstrap_local_auth(db)
            self.assertEqual(db.scalar("SELECT count(*) FROM permissions WHERE code LIKE 'inversion.%'"), 9)
            with db.transaction(None, 'test.revoke', 'role', None) as conn:
                conn.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code LIKE 'inversion.%')")
            bootstrap_local_auth(db)
            self.assertEqual(db.scalar("SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id WHERE p.code LIKE 'inversion.%'"), 0)

    def test_role_defaults_do_not_grant_waveform_run_dependencies(self):
        from backend.inversion.permissions import required_permissions, ROLE_PERMISSIONS
        self.assertEqual(required_permissions('runs.create'), frozenset({'inversion.run','waveform.read','waveform.raw.read','waveform.export'}))
        self.assertNotIn('waveform.export', ROLE_PERMISSIONS['production_engineer'])
