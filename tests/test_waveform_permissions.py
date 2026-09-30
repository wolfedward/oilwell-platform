"""Waveform grants must survive upgrade without reviving other-role revocations."""
import importlib
from pathlib import Path
import tempfile
import unittest
from backend.database import Database
from backend.seed import bootstrap_local_auth


class WaveformPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db=Database(Path(self.temp.name)/'auth.sqlite3'); self.db.migrate(); bootstrap_local_auth(self.db)

    def grants(self,role):
        return set(self.db.column("SELECT p.code FROM permissions p JOIN role_permissions g ON g.permission_id=p.id JOIN roles r ON r.id=g.role_id WHERE r.code=? AND p.code LIKE 'waveform.%'",(role,)))

    def test_role_matrix_and_no_self_review(self):
        self.assertEqual(self.grants('production_engineer'),{'waveform.read','waveform.pair.manage','waveform.review'})
        self.assertEqual(self.grants('reviewer'),{'waveform.read','waveform.review'})
        self.assertEqual(self.grants('maintenance_engineer'),{'waveform.read','waveform.raw.read','waveform.import','waveform.evidence.manage'})
        self.assertNotIn('waveform.review',self.grants('algorithm_engineer'))
        self.assertIn('waveform.audit.read',self.grants('administrator'))

    def test_admin_union_preserves_revoked_other_roles(self):
        self.assertIn('waveform.import',self.grants('maintenance_engineer'))
        with self.db.connection() as c:
            c.execute("DELETE FROM role_permissions WHERE role_id IN (SELECT id FROM roles WHERE code IN ('administrator','maintenance_engineer')) AND permission_id IN (SELECT id FROM permissions WHERE code='waveform.import')")
        bootstrap_local_auth(self.db); bootstrap_local_auth(self.db)
        self.assertIn('waveform.import',self.grants('administrator'))
        self.assertNotIn('waveform.import',self.grants('maintenance_engineer'))

    def test_v9_existing_codes_introduced_once(self):
        with self.db.connection() as c:
            c.execute("UPDATE auth_bootstrap_state SET bootstrap_version=9")
            c.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code LIKE 'waveform.%')")
        bootstrap_local_auth(self.db)
        self.assertEqual(self.grants('reviewer'),{'waveform.read','waveform.review'})
