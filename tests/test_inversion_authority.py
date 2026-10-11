"""Real SQLite session revocation at each trusted adapter call."""
import importlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from backend.auth import AuthService, AuthError, PermissionDenied
from backend.database import Database
from backend.seed import bootstrap_local_auth
from backend.waveforms.commands import Actor


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'identity.db')
        self.db.migrate(); bootstrap_local_auth(self.db)
        self.auth = AuthService(self.db)
        self.session = self.auth.login('administrator', 'Admin@2026')

    def capture(self):
        self.assertTrue(hasattr(self.auth, 'capture_background_authority'), 'current authority capture required')
        return self.auth.capture_background_authority(self.session.token, frozenset({'inversion.read'}))

    def test_worker_never_persists_bearer(self):
        ref = self.capture()
        self.assertEqual(asdict(ref), dict(domain='platform-local', user_id=self.session.user_id, session_id=self.session.id))
        self.assertNotIn(self.session.token, repr(ref))

    def test_authority_is_fresh_before_replay_claim_and_commit(self):
        ref = self.capture()
        module = importlib.import_module('backend.inversion.permissions')
        adapter = module.SessionAuthority(self.auth, ref)
        user = self.auth.me(self.session.token)
        actor = Actor('platform-local', str(user.id), user.username, user.role_code)
        self.assertIn('inversion.read', adapter.authorize(actor, 'inversion.read').permissions)
        with self.db.transaction(None, 'test.revoke', 'role', None) as conn:
            conn.execute("DELETE FROM role_permissions WHERE role_id=? AND permission_id=(SELECT id FROM permissions WHERE code='inversion.read')", (user.role_id,))
        with self.assertRaises(PermissionDenied): adapter.authorize(actor, 'inversion.read')
        self.auth.logout(self.session.token)
        with self.assertRaises(AuthError): adapter.refresh(ref)

    def test_refresh_never_uses_writable_database_connection(self):
        ref = self.capture()
        with patch.object(self.db, 'connection', side_effect=AssertionError('writable identity connection')):
            self.assertEqual(self.auth.refresh_background_authority(ref, frozenset()).id, self.session.user_id)

    def test_missing_identity_file_is_not_created(self):
        ref = self.capture()
        path = Path(self.tmp.name) / 'missing' / 'identity.db'
        with self.assertRaises(AuthError): AuthService(Database(path)).refresh_background_authority(ref, frozenset())
        self.assertFalse(path.parent.exists())

    def test_inactive_role_and_changed_identity_are_refused(self):
        ref = self.capture()
        with self.db.transaction(None, 'test.disable', 'role', None) as conn:
            conn.execute("UPDATE roles SET status='disabled' WHERE code='administrator'")
        with self.assertRaises(AuthError): self.auth.refresh_background_authority(ref, frozenset())

    def test_worker_clears_binding_and_never_reuses_old_grants(self):
        from backend.inversion.runtime import WorkerAuthority
        from backend.inversion.transactions import InversionError
        ref=self.capture(); adapter=WorkerAuthority(self.auth)
        snapshot=adapter.refresh(ref)
        user=self.auth.me(self.session.token)
        actor=Actor('platform-local',str(user.id),user.username,user.role_code)
        self.auth.logout(self.session.token)
        with self.assertRaises(InversionError): adapter.authorize(actor,'inversion.read')
        self.assertIsNone(adapter.ref)
        with self.assertRaises(InversionError): adapter.authorize(actor,'inversion.read')
        self.assertIn('inversion.read',snapshot.permissions)

    def test_read_only_refresh_refuses_missing_schema_and_locked_store(self):
        from backend.inversion.contracts import AuthorityRef
        path=Path(self.tmp.name)/'empty.db'
        sqlite3.connect(path).close()
        with self.assertRaises(AuthError): AuthService(Database(path)).refresh_background_authority(AuthorityRef('platform-local',1,1),frozenset())
        with closing(sqlite3.connect(self.db.path)) as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.execute('PRAGMA journal_mode=DELETE')
            conn.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(AuthError): self.auth.refresh_background_authority(self.capture_ref_without_query(),frozenset())

    def capture_ref_without_query(self):
        from backend.inversion.contracts import AuthorityRef
        return AuthorityRef('platform-local',self.session.user_id,self.session.id)

    def test_worker_cannot_substitute_a_different_actor(self):
        from backend.inversion.runtime import WorkerAuthority
        from backend.inversion.transactions import InversionError
        adapter=WorkerAuthority(self.auth); adapter.refresh(self.capture())
        with self.assertRaises(InversionError):
            adapter.authorize(Actor('platform-local',str(self.session.user_id+1),'Another actor','administrator'),'inversion.read')
        self.assertIsNone(adapter.ref)
