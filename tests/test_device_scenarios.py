"""Atomic planning and audit regressions for virtual-device scenarios."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from backend.audit import AuditService
from backend.database import Database
from backend.seed import seed_database
from backend.services.devices import DevicesService
from backend.simulator import SimulatorRegistry


class DeviceScenarioAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "platform.db")
        self.database.migrate()
        seed_database(self.database)
        self.audit = AuditService(self.database)
        self.registry = SimulatorRegistry(self.database)
        self.service = DevicesService(self.database, self.audit, self.registry)
        self.device_id = "D-001"
        self.actor_id = self.database.scalar("SELECT id FROM users WHERE username='administrator'")

    def test_scenario_audit_captures_previous_state_before_memory_commit(self):
        result = self.service.apply_scenario(self.actor_id, self.device_id, "offline", 30)
        audit = self.database.query_one(
            "SELECT details_json FROM audit_logs WHERE action='device.scenario' ORDER BY id DESC LIMIT 1"
        )
        details = json.loads(audit["details_json"])
        self.assertEqual(details["before"]["name"], "normal")
        self.assertEqual(details["after"]["name"], "offline")
        self.assertEqual(details["after"]["expires_at"], result["expires_at"])
        self.assertEqual(self.database.scalar("SELECT status FROM devices WHERE id=?", (self.device_id,)), "offline")

    def test_audit_failure_leaves_database_and_terminal_unchanged(self):
        terminal = self.registry.get(self.device_id)
        original = terminal.scenario
        before_db = self.database.query_one(
            "SELECT status FROM devices WHERE id=?", (self.device_id,)
        )["status"]
        original_record = self.audit.record
        self.audit.record = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable"))
        self.addCleanup(lambda: setattr(self.audit, "record", original_record))
        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            self.service.apply_scenario(self.actor_id, self.device_id, "offline", 30)
        self.assertEqual(terminal.scenario, original)
        self.assertEqual(self.database.query_one("SELECT status FROM devices WHERE id=?", (self.device_id,))["status"], before_db)
        self.assertIsNone(self.database.query_one("SELECT 1 FROM device_scenarios WHERE device_id=?", (self.device_id,)))

    def test_database_constraint_failure_leaves_database_and_terminal_unchanged(self):
        terminal = self.registry.get(self.device_id)
        original = terminal.scenario
        with self.database.transaction(None, "test.inject_constraint", "device_event", self.device_id):
            self.database.execute(
                """CREATE TRIGGER fail_scenario_event BEFORE INSERT ON device_events
                   BEGIN SELECT RAISE(ABORT, 'injected device event failure'); END"""
            )
        self.addCleanup(lambda: self.database.execute("DROP TRIGGER IF EXISTS fail_scenario_event"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.apply_scenario(self.actor_id, self.device_id, "offline", 30)
        self.assertEqual(terminal.scenario, original)
        self.assertEqual(self.database.scalar("SELECT status FROM devices WHERE id=?", (self.device_id,)), "online")
        self.assertIsNone(self.database.query_one("SELECT 1 FROM device_scenarios WHERE device_id=?", (self.device_id,)))

    def test_concurrent_scenarios_have_serial_audit_before_after_chain(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(
                lambda name: self.service.apply_scenario(self.actor_id, self.device_id, name, 30),
                ("supply_shortage", "pump_leak"),
            ))
        rows = self.database.query_all(
            "SELECT details_json FROM audit_logs WHERE action='device.scenario' ORDER BY id"
        )
        self.assertEqual(len(rows), 2)
        details = [json.loads(row["details_json"]) for row in rows]
        self.assertEqual(details[0]["before"]["name"], "normal")
        self.assertEqual(details[1]["before"]["name"], details[0]["after"]["name"])


if __name__ == "__main__":
    unittest.main()
