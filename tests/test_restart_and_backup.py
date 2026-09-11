from pathlib import Path
import tempfile
import unittest

from backend.backup import backup_database, restore_database
from backend.database import Database
from backend.seed import seed_database


class RestartAndBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "platform.db"
        self.database = Database(self.database_path)
        self.database.migrate()
        seed_database(self.database)

    def test_restart_preserves_effective_control_and_audit_chain(self):
        before = self.database.scalar(
            "SELECT COUNT(*) FROM audit_logs WHERE object_id='CMD-CONTROL-001'",
        )
        restarted = Database(self.database_path)
        restarted.migrate()
        seed_database(restarted)
        strategy = restarted.query_one(
            "SELECT current_version_id,status FROM energy_strategies WHERE id='ES-CONTROL-001'",
        )
        command = restarted.query_one(
            "SELECT strategy_version_id,status,signature FROM device_commands WHERE id='CMD-CONTROL-001'",
        )
        self.assertEqual((strategy["status"], command["status"]), ("effective", "success"))
        self.assertEqual(strategy["current_version_id"], command["strategy_version_id"])
        self.assertEqual(len(command["signature"]), 64)
        self.assertEqual(
            restarted.scalar("SELECT COUNT(*) FROM audit_logs WHERE object_id='CMD-CONTROL-001'"),
            before,
        )

    def test_backup_is_consistent_restorable_and_retained(self):
        backup_dir = self.root / "backups"
        result = backup_database(self.database, backup_dir)
        self.assertTrue(result.path.is_file())
        self.assertEqual(result.integrity, "ok")
        restored_path = self.root / "restored" / "platform.db"
        restore_database(result.path, restored_path)
        restored = Database(restored_path)
        self.assertEqual(restored.scalar("PRAGMA integrity_check"), "ok")
        self.assertEqual(restored.scalar("SELECT COUNT(*) FROM wells"), 48)
        self.assertIsNone(restored.query_one("PRAGMA foreign_key_check"))


if __name__ == "__main__":
    unittest.main()
