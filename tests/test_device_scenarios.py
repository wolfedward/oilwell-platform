"""Atomic planning and audit regressions for virtual-device scenarios."""

import json
import http.client
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading

from backend.audit import AuditService
from backend.database import Database
from backend.seed import seed_database
from backend.services.devices import DevicesService
from backend.simulator import SimulatorRegistry
from backend.api import DeviceApi
from backend.http_server import create_server


SHANGHAI = timezone(timedelta(hours=8))


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

    def test_registry_restores_persisted_active_scenario_and_normalizes_expired_once(self):
        now = datetime(2026, 6, 22, 10, 0, tzinfo=SHANGHAI)
        restored = SimulatorRegistry(self.database, clock=lambda: now)
        service = DevicesService(self.database, self.audit, restored)
        service.apply_scenario(self.actor_id, self.device_id, "offline", 30)
        restart = SimulatorRegistry(self.database, clock=lambda: now + timedelta(minutes=1))
        terminal = restart.get(self.device_id)
        self.assertEqual((terminal.scenario.name, terminal.scenario.duration_minutes, terminal.state), ("offline", 30, "offline"))
        expired = SimulatorRegistry(self.database, clock=lambda: now + timedelta(minutes=31))
        self.assertEqual((expired.get(self.device_id).scenario.name, expired.get(self.device_id).state), ("normal", "online"))
        row = self.database.query_one("SELECT name, duration_minutes, expires_at FROM device_scenarios WHERE device_id=?", (self.device_id,))
        self.assertEqual((row["name"], row["duration_minutes"], row["expires_at"]), ("normal", 0, None))
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM device_events WHERE device_id=? AND event_type='scenario_expired'", (self.device_id,)), 1)
        audit = json.loads(self.database.query_one("SELECT details_json FROM audit_logs WHERE action='simulator.scenario_expired' ORDER BY id DESC LIMIT 1")["details_json"])
        self.assertEqual((audit["before"]["name"], audit["after"]["name"]), ("offline", "normal"))

    def test_registry_expiration_commits_before_terminal_memory_and_rolls_back_on_failure(self):
        now = datetime(2026, 6, 22, 10, 0, tzinfo=SHANGHAI)
        registry = SimulatorRegistry(self.database, clock=lambda: now)
        service = DevicesService(self.database, self.audit, registry)
        service.apply_scenario(self.actor_id, self.device_id, "offline", 1)
        with self.database.transaction(None, "test.fail_expiration", "device_event", self.device_id):
            self.database.execute("""CREATE TRIGGER fail_expiration BEFORE INSERT ON device_events
                WHEN NEW.event_type='scenario_expired' BEGIN SELECT RAISE(ABORT, 'expiration failure'); END""")
        self.addCleanup(lambda: self.database.execute("DROP TRIGGER IF EXISTS fail_expiration"))
        registry.run_once(now + timedelta(minutes=2))
        terminal = registry.get(self.device_id)
        self.assertEqual((terminal.scenario.name, terminal.state), ("offline", "offline"))
        self.assertEqual(self.database.scalar("SELECT status FROM devices WHERE id=?", (self.device_id,)), "offline")


class DeviceScenarioApiTests(unittest.TestCase):
    def test_scenario_post_requires_exact_object_shape_without_calling_service(self):
        class Handler:
            def __init__(self, body):
                self.body, self.server, self.sent = body, SimpleNamespace(database=None, audit=None, simulator=None), []
            def _require(self, _permission): return SimpleNamespace(id=1)
            def _json_body(self): return self.body
            def _error(self, code, message): return {"error": {"code": code, "message": message}}
            def _send_json(self, status, payload): self.sent.append((status, payload))
        for body in ([], {"name": "offline"}, {"name": "offline", "duration_minutes": 1, "extra": True}):
            handler = Handler(body)
            api = DeviceApi(handler)
            api.service = SimpleNamespace(apply_scenario=lambda *_args: self.fail("invalid body changed state"))
            self.assertTrue(api.post("/api/devices/D-001/scenarios"))
            self.assertEqual(handler.sent[-1][0], 422)

    def test_real_http_rejects_missing_or_unknown_fields_without_state_change(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        server = create_server("127.0.0.1", 0, database_path=Path(temporary.name) / "platform.db")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close); self.addCleanup(thread.join); self.addCleanup(server.shutdown)
        def request(path, payload, token=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                headers = {"Content-Type": "application/json"}
                if token: headers["Authorization"] = f"Bearer {token}"
                connection.request("POST", path, json.dumps(payload).encode("utf-8"), headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read().decode("utf-8"))
            finally:
                connection.close()
        _, login = request("/api/auth/login", {"username": "administrator", "password": "Admin@2026"})
        token = login["data"]["token"]
        before = tuple(server.database.query_one("SELECT status FROM devices WHERE id='D-001'")) + (server.database.scalar("SELECT COUNT(*) FROM device_scenarios WHERE device_id='D-001'"),)
        for payload in ({"name": "offline"}, {"name": "offline", "duration_minutes": 1, "unknown": True}):
            with self.subTest(payload=payload):
                status, body = request("/api/devices/D-001/scenarios", payload, token)
                self.assertEqual((status, body["error"]["code"]), (422, "validation_error"))
                after = tuple(server.database.query_one("SELECT status FROM devices WHERE id='D-001'")) + (server.database.scalar("SELECT COUNT(*) FROM device_scenarios WHERE device_id='D-001'"),)
                self.assertEqual(after, before)


class SimulatorQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "platform.db")
        self.database.migrate()
        seed_database(self.database)

    def test_queue_drains_once_and_sink_delivery_falls_back_without_loss(self):
        registry = SimulatorRegistry(self.database, queue_size=2)
        registry._publish_frames([b"one", b"two"])
        self.assertEqual(registry.drain_frames(1), [b"one"])
        self.assertEqual(registry.drain_frames(), [b"two"])
        delivered = []
        registry.set_frame_sink(delivered.extend)
        registry._publish_frames([b"three"])
        self.assertEqual(delivered, [b"three"])
        registry.set_frame_sink(lambda _batch: (_ for _ in ()).throw(RuntimeError("sink down")))
        registry._publish_frames([b"four", b"five", b"six"])
        self.assertEqual(registry.drain_frames(), [b"five", b"six"])
        self.assertEqual(registry.frame_stats()["dropped"], 1)
        for invalid in (-1, True, "1"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    registry.drain_frames(invalid)

    def test_queue_is_private_and_concurrent_publish_has_one_consumer_view(self):
        registry = SimulatorRegistry(self.database, queue_size=64)
        self.assertFalse(hasattr(registry, "frames"))
        batches = [[f"{worker}-{item}".encode() for item in range(8)] for worker in range(4)]
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(registry._publish_frames, batches))
        drained = registry.drain_frames()
        self.assertEqual(set(drained), {item for batch in batches for item in batch})
        self.assertEqual(registry.drain_frames(), [])

    def test_sink_callback_runs_after_registry_lock_is_released(self):
        registry = SimulatorRegistry(self.database, clock=lambda: datetime(2026, 6, 22, 10, 0, tzinfo=SHANGHAI))
        acquired = []
        def sink(_batch):
            worker = threading.Thread(target=lambda: acquired.append(registry.scenario_lock.acquire(timeout=.2)))
            worker.start(); worker.join()
            if acquired[-1]: registry.scenario_lock.release()
        registry.set_frame_sink(sink)
        registry.run_once()
        self.assertEqual(acquired, [True])


if __name__ == "__main__":
    unittest.main()
