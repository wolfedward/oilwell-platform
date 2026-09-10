"""Versioned energy-strategy lifecycle and HTTP authorization tests."""

from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock

from backend.audit import AuditService
from backend.database import Database
from backend.http_server import create_server
from backend.seed import seed_database
from backend.services.strategies import (
    StrategyNotFound,
    StrategyService,
    StrategyValidationError,
    StrategyVersionConflict,
)
from backend.state_machine import InvalidState, transition
from tests.test_energy import recovery_fixture


SHANGHAI = timezone(timedelta(hours=8))
RECOVERY_AT = datetime(2026, 7, 1, 9, 0, tzinfo=SHANGHAI).isoformat()


def constraints():
    return {
        "min_on_minutes": 20,
        "max_on_minutes": 120,
        "min_off_minutes": 15,
        "max_off_minutes": 180,
        "min_liquid_m3_day": 2.4,
        "max_starts_per_day": 12,
        "motor_kw": 18.5,
    }


def insert_recovery(database, well_id="W-001"):
    points, _ = recovery_fixture()
    payload = {
        "observations": [
            {
                "shut_in_minutes": point.shut_in_minutes,
                "liquid_rate_m3_day": point.liquid_rate_m3_day,
            }
            for point in points
        ],
        "units": {"time": "min", "liquid_rate": "m³/d"},
    }
    with database.transaction(None, "test.recovery", "recovery_test", "REC-TEST") as conn:
        conn.execute(
            """INSERT INTO recovery_tests(
                   id,well_id,tested_at,result_data,status,source_type
               ) VALUES('REC-TEST',?,?,?,'completed','manual')""",
            (well_id, RECOVERY_AT, json.dumps(payload, ensure_ascii=False)),
        )


class StateMachineTests(unittest.TestCase):
    def test_transition_accepts_only_declared_edges(self):
        allowed = {"draft": {"pending"}, "pending": {"approved", "rejected"}}
        self.assertIsNone(transition("draft", "pending", allowed))
        with self.assertRaises(InvalidState):
            transition("draft", "approved", allowed)
        with self.assertRaises(InvalidState):
            transition("unknown", "draft", allowed)


class StrategyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "platform.db")
        self.database.migrate()
        seed_database(self.database)
        insert_recovery(self.database)
        self.audit = AuditService(self.database)
        self.service = StrategyService(self.database, self.audit)
        self.creator = self.database.scalar("SELECT id FROM users WHERE username='production_engineer'")
        self.approver = self.database.scalar("SELECT id FROM users WHERE username='administrator'")

    def draft(self):
        return self.service.optimize("W-001", constraints(), self.creator)

    def test_optimize_persists_traceable_ranked_draft_and_one_audit(self):
        draft = self.draft()
        self.assertEqual((draft["well_id"], draft["status"], draft["version"]), ("W-001", "draft", 1))
        self.assertEqual(draft["configuration"]["recovery_test_id"], "REC-TEST")
        self.assertGreater(draft["configuration"]["recovery_model"]["r_squared"], .99)
        selected = draft["configuration"]["selected_candidate"]
        self.assertTrue(selected["feasible"])
        self.assertGreaterEqual(selected["predicted_liquid_m3"], 2.4)
        self.assertLess(selected["predicted_energy_kwh"], 18.5 * 24)
        self.assertEqual(len(draft["configuration"]["ranked_candidates"]), 10)
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM audit_logs WHERE action='strategy.optimize' AND object_id=?",
            (draft["id"],),
        ), 1)

    def test_seeded_recovery_observations_support_optimization_without_test_injection(self):
        with self.database.transaction(
            None, "test.remove_manual_recovery", "recovery_test", "REC-TEST",
        ) as connection:
            connection.execute("DELETE FROM recovery_tests WHERE id='REC-TEST'")
        draft = self.draft()
        self.assertEqual(draft["configuration"]["recovery_test_id"], "RT-001")
        self.assertGreaterEqual(
            len(draft["configuration"]["recovery_observations"]), 7,
        )

    def test_submitted_strategy_is_immutable_and_uses_optimistic_version(self):
        draft = self.draft()
        updated = self.service.update(
            draft["id"], {"off_minutes": 150}, draft["version"], self.creator,
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["configuration"]["selected_candidate"]["off_minutes"], 150)
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM strategy_versions WHERE strategy_id=?", (draft["id"],)
        ), 2)
        submitted = self.service.submit(updated["id"], updated["version"], self.creator)
        self.assertEqual((submitted["status"], submitted["version"]), ("pending_approval", 3))
        with self.assertRaises(InvalidState):
            self.service.update(submitted["id"], {"off_minutes": 120}, submitted["version"], self.creator)
        with self.assertRaises(StrategyVersionConflict):
            self.service.submit(updated["id"], updated["version"], self.creator)
        configurations = self.database.column(
            "SELECT configuration FROM strategy_versions WHERE strategy_id=? ORDER BY version_number",
            (draft["id"],),
        )
        self.assertEqual(json.loads(configurations[0])["selected_candidate"]["off_minutes"], draft["configuration"]["selected_candidate"]["off_minutes"])

    def test_submitted_version_is_immutable_at_the_database_boundary(self):
        submitted = self.service.submit(self.draft()["id"], 1, self.creator)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            with self.database.transaction(
                self.creator, "test.reopen", "strategy_version",
                submitted["current_version_id"],
            ) as connection:
                connection.execute(
                    "UPDATE strategy_versions SET status='draft',submitted_at=NULL WHERE id=?",
                    (submitted["current_version_id"],),
                )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            with self.database.transaction(
                self.creator, "test.tamper", "strategy_version",
                submitted["current_version_id"],
            ) as connection:
                connection.execute(
                    "UPDATE strategy_versions SET configuration='{}' WHERE id=?",
                    (submitted["current_version_id"],),
                )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            with self.database.transaction(
                self.creator, "test.delete", "strategy_version",
                submitted["current_version_id"],
            ) as connection:
                connection.execute(
                    "DELETE FROM strategy_versions WHERE id=?",
                    (submitted["current_version_id"],),
                )

    def test_current_version_cannot_point_to_another_strategy(self):
        first = self.draft()
        second = self.draft()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "another strategy"):
            with self.database.transaction(
                self.creator, "test.cross_link", "energy_strategy", first["id"],
            ) as connection:
                connection.execute(
                    "UPDATE energy_strategies SET current_version_id=? WHERE id=?",
                    (second["current_version_id"], first["id"]),
                )
        self.assertEqual(
            self.service.detail(first["id"])["current_version_id"],
            first["current_version_id"],
        )

    def test_distinct_approver_decides_with_comment_and_machine_config_unchanged(self):
        submitted = self.service.submit(self.draft()["id"], 1, self.creator)
        before = self.database.scalar(
            "SELECT configuration FROM strategy_versions WHERE id=?", (submitted["current_version_id"],)
        )
        with self.assertRaises(StrategyValidationError):
            self.service.decide(submitted["id"], "approve", "本人审批", self.creator, submitted["version"])
        approved = self.service.decide(
            submitted["id"], "approve", "安全约束和产量指标复核通过",
            self.approver, submitted["version"],
        )
        self.assertEqual((approved["status"], approved["version"]), ("approved", 3))
        self.assertEqual(before, self.database.scalar(
            "SELECT configuration FROM strategy_versions WHERE id=?", (approved["current_version_id"],)
        ))
        approval = self.database.query_one(
            "SELECT * FROM approvals WHERE object_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (approved["id"],),
        )
        self.assertEqual((approval["decision"], approval["comment"], approval["actor_id"]),
                         ("approved", "安全约束和产量指标复核通过", self.approver))
        with self.assertRaises(StrategyVersionConflict):
            self.service.decide(submitted["id"], "reject", "过期审批", self.approver, submitted["version"])

    def test_rejected_or_approved_strategy_is_copied_before_modification(self):
        submitted = self.service.submit(self.draft()["id"], 1, self.creator)
        rejected = self.service.decide(
            submitted["id"], "reject", "需降低每日启停次数", self.approver, submitted["version"],
        )
        copied = self.service.copy(rejected["id"], self.creator)
        self.assertNotEqual(copied["id"], rejected["id"])
        self.assertEqual(copied["parent_strategy_id"], rejected["id"])
        self.assertEqual((copied["status"], copied["version"]), ("draft", 1))
        self.assertEqual(copied["configuration"], rejected["configuration"])

    def test_unsafe_constraints_non_low_well_and_missing_records_fail_closed(self):
        for well_id, values in (
            ("W-001", {**constraints(), "max_off_minutes": 600}),
            ("W-002", constraints()),
            ("NO-WELL", constraints()),
        ):
            with self.subTest(well_id=well_id):
                with self.assertRaises((StrategyValidationError, StrategyNotFound)):
                    self.service.optimize(well_id, values, self.creator)

    def test_audit_failure_rolls_back_strategy_version_and_approval(self):
        before = {table: self.database.scalar(f"SELECT COUNT(*) FROM {table}") for table in (
            "energy_strategies", "strategy_versions", "approvals",
        )}
        self.service.audit.record = Mock(side_effect=RuntimeError("audit unavailable"))
        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            self.draft()
        self.assertEqual(before, {table: self.database.scalar(f"SELECT COUNT(*) FROM {table}") for table in before})

    def test_list_detail_and_schema_links(self):
        draft = self.draft()
        self.assertEqual(self.service.detail(draft["id"])["id"], draft["id"])
        self.assertGreaterEqual(self.service.list_strategies({"well_id": "W-001", "status": "draft", "page": 1, "page_size": 10})["total"], 1)
        strategy_fks = {row[3] for row in self.database.query_all("PRAGMA foreign_key_list(energy_strategies)")}
        self.assertTrue({"well_id", "current_version_id", "parent_strategy_id", "created_by", "submitted_by", "decision_by"}.issubset(strategy_fks))
        version_fks = {
            row[3] for row in self.database.query_all(
                "PRAGMA foreign_key_list(strategy_versions)"
            )
        }
        self.assertTrue({"strategy_id", "created_by"}.issubset(version_fks))


class StrategyHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = create_server("127.0.0.1", 0, database_path=Path(self.temporary.name) / "platform.db")
        insert_recovery(self.server.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        self.production = self._login("production_engineer", "Production@2026")
        self.admin = self._login("administrator", "Admin@2026")
        self.reviewer = self._login("reviewer", "Review@2026")

    def _close(self):
        self.server.shutdown(); self.thread.join(timeout=5); self.server.server_close()

    def _request(self, method, path, token=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8"); headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=encoded, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    def _login(self, username, password):
        status, body = self._request("POST", "/api/auth/login", body={"username": username, "password": password})
        self.assertEqual(status, 200); return body["data"]["token"]

    def test_optimize_submit_approve_and_read_permissions(self):
        body = {"well_id": "W-001", "constraints": constraints()}
        self.assertEqual(self._request("POST", "/api/strategies/optimize", self.reviewer, body)[0], 403)
        status, created = self._request("POST", "/api/strategies/optimize", self.production, body)
        self.assertEqual(status, 201)
        strategy = created["data"]
        self.assertEqual(self._request("POST", f"/api/strategies/{strategy['id']}/approve", self.production,
                                       {"version": strategy["version"], "comment": "越权"})[0], 403)
        status, submitted = self._request("POST", f"/api/strategies/{strategy['id']}/submit", self.production,
                                          {"version": strategy["version"]})
        self.assertEqual(status, 200)
        status, approved = self._request("POST", f"/api/strategies/{strategy['id']}/approve", self.admin,
                                         {"version": submitted["data"]["version"], "comment": "批准执行该节能周期"})
        self.assertEqual((status, approved["data"]["status"]), (200, "approved"))
        status, listing = self._request("GET", "/api/strategies?well_id=W-001&page_size=5", self.reviewer)
        self.assertEqual(status, 200)
        self.assertTrue(any(item["id"] == strategy["id"] for item in listing["data"]["items"]))
        self.assertEqual(self._request("POST", f"/api/strategies/{strategy['id']}/reject?bad=1", self.admin,
                                       {"version": approved["data"]["version"], "comment": "错误"})[0], 422)


if __name__ == "__main__":
    unittest.main()
