"""One-time v8 grants cannot overwrite passwords, manual grants or revocations."""
import importlib
from pathlib import Path
import tempfile
import unittest

from backend.auth import AuthService
from backend.database import Database
from backend.seed import bootstrap_local_auth


EXPECTED = {
    "administrator": {"registry.read", "registry.wells.manage", "registry.devices.manage", "registry.bindings.manage", "registry.maps.manage", "registry.review"},
    "production_engineer": {"registry.read", "registry.wells.manage", "registry.bindings.manage", "registry.review"},
    "algorithm_engineer": {"registry.read", "registry.maps.manage"},
    "maintenance_engineer": {"registry.read", "registry.devices.manage", "registry.bindings.manage", "registry.maps.manage"},
    "reviewer": {"registry.read", "registry.review"},
}


class RegistryPermissionsTests(unittest.TestCase):
    def setUp(self):
        try:
            self.permissions = importlib.import_module("backend.registry.permissions")
        except ModuleNotFoundError as error:
            if error.name != "backend.registry.permissions":
                raise
            self.fail("Registry permission migration has not been implemented")
        self.temp = tempfile.TemporaryDirectory(prefix="registry-permissions-")
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name)/"auth.sqlite3")
        self.db.migrate()
        bootstrap_local_auth(self.db)

    def grants(self, role):
        return set(self.db.column("SELECT p.code FROM role_permissions g JOIN roles r ON r.id=g.role_id JOIN permissions p ON p.id=g.permission_id WHERE r.code=? AND p.code LIKE 'registry.%'",(role,)))

    def simulate_v7(self):
        with self.db.connection() as db:
            db.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code LIKE 'registry.%')")
            db.execute("DELETE FROM permissions WHERE code LIKE 'registry.%'")
            db.execute("UPDATE auth_bootstrap_state SET bootstrap_version=7 WHERE id=1")

    def hashes(self):
        return [(row["username"],row["password_hash"],row["password_salt"]) for row in self.db.query_all("SELECT username,password_hash,password_salt FROM users ORDER BY username")]

    def test_fresh_defaults_match_five_role_matrix(self):
        for role,want in EXPECTED.items():
            with self.subTest(role=role):
                self.assertEqual(self.grants(role),want)
        self.assertEqual(self.db.scalar("SELECT bootstrap_version FROM auth_bootstrap_state WHERE id=1"),8)

    def test_v7_upgrade_preserves_passwords_and_existing_unrelated_grants(self):
        self.simulate_v7()
        before=self.hashes()
        with self.db.connection() as db:
            db.execute("INSERT INTO permissions(code,name,source_type) VALUES('site.custom','自定义','manual')")
            db.execute("INSERT INTO role_permissions(role_id,permission_id,source_type) SELECT r.id,p.id,'manual' FROM roles r,permissions p WHERE r.code='reviewer' AND p.code='site.custom'")
        bootstrap_local_auth(self.db)
        self.assertEqual(self.hashes(),before)
        for role,want in EXPECTED.items():
            self.assertEqual(self.grants(role),want)
        self.assertEqual(self.db.scalar("SELECT g.source_type FROM role_permissions g JOIN permissions p ON p.id=g.permission_id WHERE p.code='site.custom'"),"manual")

    def test_preexisting_named_permission_manual_grants_are_preserved_during_upgrade(self):
        self.simulate_v7()
        with self.db.connection() as db:
            db.execute("INSERT INTO permissions(code,name,source_type) VALUES('registry.read','手工台账读','manual')")
            db.execute("INSERT INTO role_permissions(role_id,permission_id,source_type) SELECT r.id,p.id,'manual' FROM roles r,permissions p WHERE r.code='reviewer' AND p.code='registry.read'")
            db.execute("INSERT INTO permissions(code,name,source_type) VALUES('registry.maps.manage','手工点表管理','manual')")
            db.execute("INSERT INTO role_permissions(role_id,permission_id,source_type) SELECT r.id,p.id,'manual' FROM roles r,permissions p WHERE r.code='reviewer' AND p.code='registry.maps.manage'")
        bootstrap_local_auth(self.db)
        self.assertEqual(self.grants("reviewer"),EXPECTED["reviewer"]|{"registry.maps.manage"})
        self.assertEqual(self.grants("production_engineer"),EXPECTED["production_engineer"])
        self.assertEqual(self.db.scalar("SELECT g.source_type FROM role_permissions g JOIN roles r ON r.id=g.role_id JOIN permissions p ON p.id=g.permission_id WHERE r.code='reviewer' AND p.code='registry.read'"),"manual")
        self.assertEqual(self.db.scalar("SELECT name FROM permissions WHERE code='registry.read'"),"手工台账读")

    def test_restart_does_not_restore_revoked_non_administrator_permissions(self):
        before=self.hashes()
        with self.db.connection() as db:
            db.execute("DELETE FROM role_permissions WHERE role_id=(SELECT id FROM roles WHERE code='maintenance_engineer') AND permission_id=(SELECT id FROM permissions WHERE code='registry.devices.manage')")
        bootstrap_local_auth(self.db)
        bootstrap_local_auth(self.db)
        self.assertNotIn("registry.devices.manage",self.grants("maintenance_engineer"))
        self.assertEqual(self.hashes(),before)

    def test_administrator_full_registry_permissions_are_restored_without_other_role_changes(self):
        with self.db.connection() as db:
            db.execute("DELETE FROM role_permissions WHERE role_id IN (SELECT id FROM roles WHERE code IN ('administrator','algorithm_engineer')) AND permission_id IN (SELECT id FROM permissions WHERE code LIKE 'registry.%')")
        bootstrap_local_auth(self.db)
        self.assertEqual(self.grants("administrator"),EXPECTED["administrator"])
        self.assertEqual(self.grants("algorithm_engineer"),set())

    def test_required_permission_groups_are_and_of_or_and_unknown_actions_fail_closed(self):
        require=self.permissions.required_permissions
        self.assertEqual(require("device.state",{"status":"suspended"}),(("registry.read",),("registry.devices.manage","registry.review")))
        for status in ("active","retired"):
            self.assertEqual(require("device.state",{"status":status}),(("registry.read",),("registry.review",)))
        for action,extra in (("well.create","registry.wells.manage"),("well.change","registry.wells.manage"),
                             ("device.create","registry.devices.manage"),("device.change","registry.devices.manage"),
                             ("device.bind","registry.bindings.manage"),("device.unbind","registry.bindings.manage"),
                             ("map.create","registry.maps.manage"),("map.assign","registry.maps.manage"),("map.review","registry.review"),
                             ("resolved_telemetry","telemetry.read"),("link_status","acquisition.read"),("audit","audit.read")):
            self.assertEqual(require(action,{}),(("registry.read",),(extra,)))
        from backend.registry.errors import RegistryError
        with self.assertRaises(RegistryError):
            require("unregistered.action",{})
        with self.assertRaises(RegistryError):
            require("device.state",{"status":"pending"})
