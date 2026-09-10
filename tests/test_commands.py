"""Signed virtual-terminal command, receipt, retry and rollback tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from backend.audit import AuditService
from backend.database import Database
from backend.http_server import create_server
from backend.seed import seed_database
from backend.services.commands import (
    CommandNotFound,
    CommandService,
    CommandValidationError,
)
from backend.services.strategies import StrategyService
from backend.simulator import (
    SimulatorRegistry, load_command_signing_key, sign_control_command,
)


SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 2, 10, 0, tzinfo=SHANGHAI)
LIMITS = {
    "min_on_minutes": 20,
    "max_on_minutes": 120,
    "min_off_minutes": 15,
    "max_off_minutes": 180,
    "min_liquid_m3_day": 2.4,
    "max_starts_per_day": 12,
    "motor_kw": 18.5,
}


class CommandServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "platform.db")
        self.database.migrate()
        seed_database(self.database)
        self.audit = AuditService(self.database)
        self.registry = SimulatorRegistry(self.database, clock=lambda: NOW)
        self.strategies = StrategyService(self.database, self.audit)
        self.commands = CommandService(self.database, self.audit, self.registry)
        self.creator = self.database.scalar(
            "SELECT id FROM users WHERE username='production_engineer'"
        )
        self.approver = self.database.scalar(
            "SELECT id FROM users WHERE username='administrator'"
        )
        self.dispatcher = self.database.scalar(
            "SELECT id FROM users WHERE username='maintenance_engineer'"
        )

    def approved_strategy(self):
        draft = self.strategies.optimize("W-001", LIMITS, self.creator)
        submitted = self.strategies.submit(draft["id"], draft["version"], self.creator)
        return self.strategies.decide(
            submitted["id"], "approve", "安全约束与产量目标复核通过",
            self.approver, submitted["version"],
        )

    def effective_strategy(self):
        strategy = self.approved_strategy()
        command = self.commands.dispatch_strategy(strategy["id"], self.dispatcher, NOW)
        self.commands.process(NOW)
        self.assertEqual(self.commands.get(command["id"])["state"], "success")
        return self.strategies.detail(strategy["id"])

    def test_online_terminal_returns_signed_five_stage_receipt_and_effective_strategy(self):
        strategy = self.approved_strategy()
        command = self.commands.dispatch_strategy(strategy["id"], self.dispatcher, NOW)
        payload = command["payload"]
        self.assertEqual(payload["device_id"], "D-001")
        self.assertEqual(payload["strategy_version_id"], strategy["current_version_id"])
        self.assertEqual(
            datetime.fromisoformat(payload["expires_at"])
            - datetime.fromisoformat(payload["issued_at"]),
            timedelta(minutes=15),
        )
        self.assertEqual(len(payload["nonce"]), 32)
        self.assertEqual(len(payload["signature"]), 64)
        self.assertEqual(self.strategies.detail(strategy["id"])["status"], "pending_dispatch")

        summary = self.commands.process(NOW)

        self.assertEqual(summary, {"processed": 1, "succeeded": 1, "deferred": 0, "failed": 0})
        completed = self.commands.get(command["id"])
        self.assertEqual(completed["state"], "success")
        effective = self.strategies.detail(strategy["id"])
        self.assertEqual(effective["status"], "effective")
        self.assertEqual(
            datetime.fromisoformat(effective["effective_at"]), NOW,
        )
        self.assertEqual(
            self.database.column(
                "SELECT state FROM command_attempts WHERE command_id=? ORDER BY id",
                (command["id"],),
            ),
            ["sent", "received", "validated", "executing", "success"],
        )
        self.assertEqual(self.registry.get("D-001").control_cycle["off_minutes"],
                         payload["off_minutes"])

    def test_offline_terminal_defers_then_succeeds_when_online(self):
        strategy = self.approved_strategy()
        terminal = self.registry.get("D-001")
        terminal.apply_scenario("offline", 60, now=NOW)
        command = self.commands.dispatch_strategy(strategy["id"], self.dispatcher, NOW)

        first = self.commands.process(NOW)
        self.assertEqual((first["deferred"], self.commands.get(command["id"])["state"]), (1, "queued"))
        terminal.apply_scenario("normal", 1, now=NOW + timedelta(minutes=1))
        second = self.commands.process(NOW + timedelta(minutes=1))

        self.assertEqual(second["succeeded"], 1)
        self.assertEqual(self.commands.get(command["id"])["attempt_count"], 2)
        self.assertEqual(self.strategies.detail(strategy["id"])["status"], "effective")

    def test_three_unavailable_attempts_fail_and_create_one_linked_alarm(self):
        strategy = self.approved_strategy()
        self.registry.get("D-001").apply_scenario("offline", 60, now=NOW)
        command = self.commands.dispatch_strategy(strategy["id"], self.dispatcher, NOW)
        for offset in range(3):
            self.commands.process(NOW + timedelta(minutes=offset))

        failed = self.commands.get(command["id"])
        self.assertEqual((failed["state"], failed["attempt_count"]), ("failed", 3))
        self.assertEqual(self.strategies.detail(strategy["id"])["status"], "failed")
        alarm = self.database.query_one("SELECT * FROM alarms WHERE command_id=?", (command["id"],))
        self.assertEqual((alarm["strategy_id"], alarm["alarm_type"], alarm["status"]),
                         (strategy["id"], "command_delivery_failed", "new"))
        self.assertEqual(
            (alarm["source_object_type"], alarm["source_object_id"]),
            ("command", command["id"]),
        )
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM audit_logs WHERE action='alarm.create' AND object_id=?",
            (alarm["id"],),
        ), 1)
        self.commands.process(NOW + timedelta(minutes=3))
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM alarms WHERE command_id=?", (command["id"],)
        ), 1)

    def test_expired_or_tampered_command_fails_closed(self):
        strategy = self.approved_strategy()
        command = self.commands.dispatch_strategy(strategy["id"], self.dispatcher, NOW)
        summary = self.commands.process(NOW + timedelta(minutes=16))
        self.assertEqual((summary["failed"], self.commands.get(command["id"])["state"]), (1, "expired"))

        second = self.approved_strategy()
        tampered = self.commands.dispatch_strategy(second["id"], self.dispatcher, NOW)
        with self.database.transaction(
            self.dispatcher, "test.tamper_command", "device_command", tampered["id"],
        ) as connection:
            payload = json.loads(connection.execute(
                "SELECT payload FROM device_commands WHERE id=?", (tampered["id"],)
            ).fetchone()[0])
            payload["on_minutes"] = 10
            unsigned = {key: value for key, value in payload.items() if key != "signature"}
            payload["signature"] = sign_control_command(
                unsigned, self.registry.command_signing_key,
            )
            connection.execute(
                "UPDATE device_commands SET payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), tampered["id"]),
            )
        self.commands.process(NOW)
        self.assertEqual(self.commands.get(tampered["id"])["state"], "failed")

        third = self.approved_strategy()
        rebound = self.commands.dispatch_strategy(third["id"], self.dispatcher, NOW)
        with self.database.transaction(
            self.dispatcher, "test.rebind_command", "device_command", rebound["id"],
        ) as connection:
            payload = json.loads(connection.execute(
                "SELECT payload FROM device_commands WHERE id=?", (rebound["id"],)
            ).fetchone()[0])
            payload["strategy_version_id"] = "SV-ALTERED"
            unsigned = {key: value for key, value in payload.items() if key != "signature"}
            payload["signature"] = sign_control_command(
                unsigned, self.registry.command_signing_key,
            )
            connection.execute(
                "UPDATE device_commands SET payload=?,signature=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 payload["signature"], rebound["id"]),
            )
        self.commands.process(NOW)
        self.assertEqual(self.commands.get(rebound["id"])["state"], "failed")

    def test_dispatch_is_idempotent_and_audit_failure_rolls_back(self):
        strategy = self.approved_strategy()
        first = self.commands.dispatch_strategy(strategy["id"], self.dispatcher, NOW)
        second = self.commands.dispatch_strategy(strategy["id"], self.dispatcher, NOW)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM audit_logs WHERE action='command.dispatch' AND object_id=?",
            (first["id"],),
        ), 1)

        another = self.approved_strategy()
        before = self.database.scalar("SELECT COUNT(*) FROM device_commands")
        self.commands.audit.record = Mock(side_effect=RuntimeError("audit unavailable"))
        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            self.commands.dispatch_strategy(another["id"], self.dispatcher, NOW)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM device_commands"), before)
        self.assertEqual(self.strategies.detail(another["id"])["status"], "approved")

    def test_success_audit_failure_keeps_database_and_terminal_unchanged(self):
        strategy = self.approved_strategy()
        command = self.commands.dispatch_strategy(strategy["id"], self.dispatcher, NOW)
        terminal = self.registry.get("D-001")
        self.assertIsNone(terminal.control_cycle)
        self.commands.audit.record = Mock(side_effect=RuntimeError("audit unavailable"))

        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            self.commands.process(NOW)

        self.assertEqual(self.commands.get(command["id"])["state"], "queued")
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM command_attempts WHERE command_id=?", (command["id"],)
        ), 0)
        self.assertEqual(self.strategies.detail(strategy["id"])["status"], "pending_dispatch")
        self.assertIsNone(terminal.control_cycle)

    def test_terminal_commit_failure_never_leaves_persisted_success(self):
        strategy = self.approved_strategy()
        command = self.commands.dispatch_strategy(strategy["id"], self.dispatcher, NOW)
        terminal = self.registry.get("D-001")
        terminal.commit_control_command = Mock(side_effect=RuntimeError("terminal write failed"))

        summary = self.commands.process(NOW)

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(self.commands.get(command["id"])["state"], "failed")
        self.assertEqual(self.strategies.detail(strategy["id"])["status"], "failed")
        self.assertIsNone(terminal.control_cycle)
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM alarms WHERE command_id=?", (command["id"],)
        ), 1)

    def test_rollback_sends_parent_effective_version(self):
        parent = self.effective_strategy()
        child = self.strategies.copy(parent["id"], self.creator)
        child = self.strategies.update(
            child["id"], {"off_minutes": 150}, child["version"], self.creator,
        )
        child = self.strategies.submit(child["id"], child["version"], self.creator)
        child = self.strategies.decide(
            child["id"], "approve", "修订周期复核通过", self.approver, child["version"],
        )
        child_command = self.commands.dispatch_strategy(child["id"], self.dispatcher, NOW)
        self.commands.process(NOW)
        self.assertEqual(self.commands.get(child_command["id"])["state"], "success")

        rollback = self.commands.rollback(
            child["id"], self.dispatcher, "效果未达到预期，恢复上一已生效周期",
            NOW + timedelta(minutes=2),
        )
        self.assertEqual(rollback["payload"]["strategy_version_id"], parent["current_version_id"])
        self.commands.process(NOW + timedelta(minutes=2))
        self.assertEqual(self.strategies.detail(child["id"])["status"], "rolled_back")
        self.assertEqual(
            self.registry.get("D-001").control_cycle["off_minutes"],
            parent["configuration"]["selected_candidate"]["off_minutes"],
        )
        repeated = self.commands.rollback(
            child["id"], self.dispatcher, "效果未达到预期，恢复上一已生效周期",
            NOW + timedelta(minutes=3),
        )
        self.assertEqual(repeated["id"], rollback["id"])

    def test_installation_signing_key_is_private_stable_and_unique(self):
        same_installation = SimulatorRegistry(self.database, clock=lambda: NOW)
        self.assertEqual(
            same_installation.command_signing_key,
            self.registry.command_signing_key,
        )
        self.assertGreaterEqual(len(self.registry.command_signing_key), 32)
        other_database = Database(Path(self.temporary.name) / "other.db")
        other_database.migrate()
        seed_database(other_database)
        other_installation = SimulatorRegistry(other_database, clock=lambda: NOW)
        self.assertNotEqual(
            other_installation.command_signing_key,
            self.registry.command_signing_key,
        )
        race_path = Path(self.temporary.name) / "concurrent-install.db"
        with ThreadPoolExecutor(max_workers=12) as executor:
            concurrent_keys = list(executor.map(
                lambda _: load_command_signing_key(race_path), range(36),
            ))
        self.assertEqual(len(set(concurrent_keys)), 1)
        self.assertFalse(list(race_path.parent.glob("*.command-key.*.tmp")))

    def test_successful_control_cycle_is_restored_after_registry_restart(self):
        strategy = self.approved_strategy()
        command = self.commands.dispatch_strategy(strategy["id"], self.dispatcher, NOW)
        self.commands.process(NOW)

        restarted = SimulatorRegistry(self.database, clock=lambda: NOW + timedelta(minutes=1))
        restored = restarted.get("D-001")

        self.assertEqual(restored.state, "running")
        self.assertEqual(restored.control_cycle["command_id"], command["id"])
        self.assertEqual(
            restored.control_cycle["off_minutes"], command["payload"]["off_minutes"],
        )

    def test_legacy_duplicate_attempt_states_receive_deterministic_numbers(self):
        command_id = self.database.scalar(
            "SELECT id FROM device_commands ORDER BY id LIMIT 1"
        )
        with self.database.transaction(
            self.dispatcher, "test.legacy_attempts", "device_command", command_id,
        ) as connection:
            connection.execute("DROP INDEX command_attempts_stage_unique")
            connection.execute(
                "UPDATE command_attempts SET attempt_number=NULL,state=NULL,receipt_at=NULL "
                "WHERE command_id=?",
                (command_id,),
            )
            original = connection.execute(
                "SELECT attempted_at,result_detail,status,actor_id,source_type "
                "FROM command_attempts WHERE command_id=? ORDER BY id LIMIT 1",
                (command_id,),
            ).fetchone()
            connection.execute(
                """INSERT INTO command_attempts(
                       command_id,attempted_at,result_detail,status,actor_id,source_type
                   ) VALUES(?,?,?,?,?,?)""",
                (command_id, original["attempted_at"], original["result_detail"],
                 original["status"], original["actor_id"], original["source_type"]),
            )

        self.database.migrate()

        attempts = self.database.query_all(
            "SELECT attempt_number,state FROM command_attempts "
            "WHERE command_id=? ORDER BY attempt_number,id",
            (command_id,),
        )
        self.assertEqual([row["attempt_number"] for row in attempts], [1, 2])
        self.assertEqual(len({row["state"] for row in attempts}), 1)

    def test_failed_rollback_retry_returns_the_original_command(self):
        parent = self.effective_strategy()
        child = self.strategies.copy(parent["id"], self.creator)
        child = self.strategies.submit(child["id"], child["version"], self.creator)
        child = self.strategies.decide(
            child["id"], "approve", "替代周期复核通过",
            self.approver, child["version"],
        )
        child_command = self.commands.dispatch_strategy(
            child["id"], self.dispatcher, NOW,
        )
        self.commands.process(NOW)
        self.assertEqual(self.commands.get(child_command["id"])["state"], "success")
        self.registry.get("D-001").apply_scenario(
            "offline", 60, now=NOW + timedelta(minutes=1),
        )
        rollback = self.commands.rollback(
            child["id"], self.dispatcher, "终端效果异常，恢复上一已生效周期",
            NOW + timedelta(minutes=1),
        )
        for offset in range(1, 4):
            self.commands.process(NOW + timedelta(minutes=offset))
        self.assertEqual(self.commands.get(rollback["id"])["state"], "failed")

        repeated = self.commands.rollback(
            child["id"], self.dispatcher, "终端效果异常，恢复上一已生效周期",
            NOW + timedelta(minutes=5),
        )
        self.assertEqual(repeated["id"], rollback["id"])

    def test_listing_detail_and_validation(self):
        with self.assertRaises(CommandNotFound):
            self.commands.get("CMD-NOT-FOUND")
        with self.assertRaises(CommandNotFound):
            self.commands.dispatch_strategy("NO-STRATEGY", self.dispatcher, NOW)
        command = self.commands.dispatch_strategy(
            self.approved_strategy()["id"], self.dispatcher, NOW,
        )
        listing = self.commands.list_commands({
            "device_id": "D-001", "state": "queued", "page": 1, "page_size": 5,
        })
        self.assertTrue(any(item["id"] == command["id"] for item in listing["items"]))
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM pragma_foreign_key_check"), 0)


class CommandHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = create_server(
            "127.0.0.1", 0,
            database_path=Path(self.temporary.name) / "platform.db",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        self.production = self._login("production_engineer", "Production@2026")
        self.admin = self._login("administrator", "Admin@2026")
        self.maintenance = self._login("maintenance_engineer", "Maintenance@2026")
        self.reviewer = self._login("reviewer", "Review@2026")

    def _close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def _request(self, method, path, token=None, body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=8,
        )
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def _login(self, username, password):
        status, body = self._request(
            "POST", "/api/auth/login", body={"username": username, "password": password},
        )
        self.assertEqual(status, 200)
        return body["data"]["token"]

    def _approved_strategy(self):
        status, created = self._request(
            "POST", "/api/strategies/optimize", self.production,
            {"well_id": "W-001", "constraints": LIMITS},
        )
        self.assertEqual(status, 201)
        strategy = created["data"]
        status, submitted = self._request(
            "POST", f"/api/strategies/{strategy['id']}/submit", self.production,
            {"version": strategy["version"]},
        )
        self.assertEqual(status, 200)
        status, approved = self._request(
            "POST", f"/api/strategies/{strategy['id']}/approve", self.admin,
            {"version": submitted["data"]["version"], "comment": "控制参数复核通过"},
        )
        self.assertEqual(status, 200)
        return approved["data"]

    def test_dispatch_process_and_command_query_routes(self):
        strategy = self._approved_strategy()
        path = f"/api/strategies/{strategy['id']}/dispatch"
        self.assertEqual(self._request(
            "POST", "/api/strategies/NO-STRATEGY/dispatch", self.maintenance, {},
        )[0], 404)
        self.assertEqual(self._request("POST", path, self.reviewer, {})[0], 403)
        self.assertEqual(self._request("POST", path, self.maintenance, {"extra": True})[0], 422)

        status, queued = self._request("POST", path, self.maintenance, {})
        self.assertEqual((status, queued["data"]["state"]), (202, "queued"))
        repeated = self._request("POST", path, self.maintenance, {})[1]
        self.assertEqual(repeated["data"]["id"], queued["data"]["id"])
        self.assertEqual(self._request("GET", "/api/commands", self.maintenance)[0], 403)

        status, listing = self._request(
            "GET", "/api/commands?device_id=D-001&state=queued&page_size=5", self.reviewer,
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(item["id"] == queued["data"]["id"] for item in listing["data"]["items"]))
        self.assertEqual(self._request(
            "GET", f"/api/commands/{queued['data']['id']}?bad=1", self.reviewer,
        )[0], 422)

        status, processed = self._request("POST", "/api/commands/process", self.maintenance, {})
        self.assertEqual((status, processed["data"]["succeeded"]), (200, 1))
        status, detail = self._request(
            "GET", f"/api/commands/{queued['data']['id']}", self.reviewer,
        )
        self.assertEqual((status, detail["data"]["state"]), (200, "success"))
        self.assertEqual(len(detail["data"]["receipts"]), 5)
        self.assertEqual(self._request(
            "GET", "/api/commands/CMD-NOT-FOUND", self.reviewer,
        )[0], 404)


if __name__ == "__main__":
    unittest.main()
