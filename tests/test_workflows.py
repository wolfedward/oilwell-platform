"""Alarm and work-order lifecycle acceptance tests."""

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from backend.audit import AuditService
from backend.database import Database
from backend.http_server import create_server
from backend.seed import seed_database
from backend.services.workflows import (
    WorkflowNotFound,
    WorkflowService,
    WorkflowValidationError,
    WorkflowWorker,
)
from backend.state_machine import InvalidState


SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 6, 22, 10, 0, tzinfo=SHANGHAI)


class WorkflowServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "platform.db")
        self.database.migrate()
        seed_database(self.database)
        self.audit = AuditService(self.database)
        self.service = WorkflowService(self.database, self.audit, clock=lambda: NOW)
        self.production = self._actor("production_engineer")
        self.maintenance = self._actor("maintenance_engineer")
        self.reviewer = self._actor("reviewer")
        self.diagnosis_id = "DX-WORKFLOW-TEST"
        with self.database.transaction(
            self.production, "test.diagnosis", "diagnosis", self.diagnosis_id,
        ) as connection:
            connection.execute(
                """INSERT INTO diagnoses(
                       id,well_id,diagnosis_type,confidence,evidence,recommendation,
                       status,actor_id,source_type
                   ) VALUES(?, 'W-001', 'supply_shortage', .93, '[]',
                            '核实供液并调整开关井制度','new',?,'algorithm')""",
                (self.diagnosis_id, self.production),
            )

    def _actor(self, username):
        return self.database.scalar("SELECT id FROM users WHERE username=?", (username,))

    def _alarm(self):
        return self.service.create_alarm(
            "diagnosis", self.diagnosis_id, "warning",
            {"condition": "supply_shortage", "confidence": .93},
            actor_id=self.production,
        )

    def test_schema_has_native_workflow_relationships_and_migration_is_idempotent(self):
        alarm_columns = {row[1] for row in self.database.query_all("PRAGMA table_info(alarms)")}
        order_columns = {row[1] for row in self.database.query_all("PRAGMA table_info(work_orders)")}
        log_columns = {row[1] for row in self.database.query_all("PRAGMA table_info(work_order_logs)")}
        self.assertTrue({
            "source_object_type", "source_object_id", "confirmed_at", "closed_at",
            "resolution_comment", "version",
        }.issubset(alarm_columns))
        self.assertTrue({
            "kind", "assignee_id", "due_at", "accepted_at", "completed_at",
            "closed_at", "closed_by", "version", "overdue_from_status",
        }.issubset(order_columns))
        self.assertTrue({"from_status", "to_status", "comment", "changed_at"}.issubset(log_columns))
        order_fks = {row[3]: (row[2], row[4]) for row in self.database.query_all(
            "PRAGMA foreign_key_list(work_orders)"
        )}
        self.assertEqual(order_fks["alarm_id"], ("alarms", "id"))
        self.assertEqual(order_fks["assignee_id"], ("users", "id"))
        self.assertEqual(order_fks["closed_by"], ("users", "id"))
        self.database.migrate()
        self.assertIsNone(self.database.query_one("PRAGMA foreign_key_check"))
        self.assertEqual(self.database.scalar(
            """SELECT COUNT(*) FROM work_orders
               WHERE source_type='simulator' AND (assignee_id IS NULL OR due_at IS NULL)"""
        ), 0)
        historical = self.database.query_one(
            "SELECT id FROM work_orders WHERE source_type='simulator' AND status='completed' LIMIT 1"
        )
        self.assertIsNotNone(historical)
        self.assertEqual(self.service.transition_work_order(
            historical["id"], "closed", self.reviewer, "历史处置复核合格",
        )["status"], "closed")

    def test_alarm_creation_is_source_linked_idempotent_and_audited(self):
        first = self._alarm()
        second = self._alarm()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["well_id"], "W-001")
        self.assertEqual(first["diagnosis_id"], self.diagnosis_id)
        self.assertEqual(first["source_object_type"], "diagnosis")
        self.assertEqual(first["details"]["confidence"], .93)
        self.assertEqual(first["source_type"], "manual")
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM alarms WHERE source_object_type='diagnosis' AND source_object_id=?",
            (self.diagnosis_id,),
        ), 1)
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM audit_logs WHERE action='alarm.create' AND object_id=?",
            (first["id"],),
        ), 1)

    def test_alarm_and_work_order_complete_a_traceable_lifecycle(self):
        alarm = self._alarm()
        confirmed = self.service.transition_alarm(
            alarm["id"], "confirmed", self.production, "确认示功图供液不足",
        )
        self.assertEqual(confirmed["status"], "confirmed")
        order = self.service.create_work_order(
            alarm["id"], "production_adjustment", self.maintenance, self.production,
            due_at=(NOW + timedelta(hours=2)).isoformat(),
        )
        self.assertEqual(order["status"], "pending")
        self.assertEqual(self.service.get_alarm(alarm["id"])["status"], "assigned")
        for target, actor, comment in (
            ("accepted", self.maintenance, "接单并核对现场参数"),
            ("processing", self.maintenance, "已按建议调整间开周期"),
            ("review", self.maintenance, "处置完成，提交复核"),
            ("closed", self.reviewer, "复核液量恢复，同意关闭"),
        ):
            order = self.service.transition_work_order(order["id"], target, actor, comment)
        self.assertEqual(order["status"], "closed")
        self.assertEqual(order["closed_by"], self.reviewer)
        self.assertEqual(self.service.get_alarm(alarm["id"])["status"], "closed")
        logs = self.database.query_all(
            "SELECT from_status,to_status,comment FROM work_order_logs WHERE work_order_id=? ORDER BY id",
            (order["id"],),
        )
        self.assertEqual(
            [(row["from_status"], row["to_status"]) for row in logs],
            [(None, "pending"), ("pending", "accepted"), ("accepted", "processing"),
             ("processing", "review"), ("review", "closed")],
        )
        trace = self.service.trace(order["id"])
        self.assertEqual(trace["work_order_id"], order["id"])
        self.assertEqual(trace["alarm_id"], alarm["id"])
        self.assertEqual(trace["diagnosis_id"], self.diagnosis_id)
        self.assertGreaterEqual(len(trace["audit_ids"]), 8)

    def test_invalid_edges_comments_and_actor_rules_do_not_mutate(self):
        alarm = self._alarm()
        with self.assertRaises(WorkflowValidationError):
            self.service.transition_alarm(alarm["id"], "closed", self.production, "跳过正常流程")
        confirmed = self.service.transition_alarm(
            alarm["id"], "confirmed", self.production, "确认异常工况有效",
        )
        with self.assertRaises(WorkflowValidationError):
            self.service.transition_alarm(
                confirmed["id"], "assigned", self.production, "试图绕过工单直接派单",
            )
        with self.assertRaises(WorkflowValidationError):
            self.service.transition_alarm(confirmed["id"], "false_positive", self.production, "")
        with self.assertRaises(WorkflowValidationError):
            self.service.create_work_order(
                confirmed["id"], "production_adjustment", self.production, self.production,
            )
        order = self.service.create_work_order(
            confirmed["id"], "production_adjustment", self.maintenance, self.production,
        )
        with self.assertRaises(WorkflowValidationError):
            self.service.transition_work_order(order["id"], "accepted", self.production, "非指派人员接单")
        with self.assertRaises(InvalidState):
            self.service.transition_work_order(order["id"], "closed", self.reviewer, "跳过处置和复核")
        self.assertEqual(self.service.get_work_order(order["id"])["status"], "pending")
        with self.assertRaises(WorkflowNotFound):
            self.service.get_work_order("WO-NOT-FOUND")

    def test_overdue_evaluation_is_repeat_safe_logged_and_audited(self):
        alarm = self._alarm()
        self.service.transition_alarm(alarm["id"], "confirmed", self.production, "确认需要工单处置")
        order = self.service.create_work_order(
            alarm["id"], "production_adjustment", self.maintenance, self.production,
            due_at=(NOW - timedelta(minutes=1)).isoformat(),
        )
        first = self.service.evaluate_overdue(NOW)
        second = self.service.evaluate_overdue(NOW)
        self.assertEqual(first, {"evaluated": 1, "overdue": 1})
        self.assertEqual(second, {"evaluated": 0, "overdue": 0})
        current = self.service.get_work_order(order["id"])
        self.assertEqual(current["status"], "overdue")
        self.assertEqual(current["overdue_from_status"], "pending")
        with self.assertRaises(InvalidState):
            self.service.transition_work_order(
                order["id"], "closed", self.reviewer, "未处置的超时工单不能直接关闭",
            )
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM work_order_logs WHERE work_order_id=? AND to_status='overdue'",
            (order["id"],),
        ), 1)
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM audit_logs WHERE action='work_order.overdue' AND object_id=?",
            (order["id"],),
        ), 1)

    def test_manual_overdue_and_unknown_list_state_are_rejected(self):
        alarm = self._alarm()
        self.service.transition_alarm(alarm["id"], "confirmed", self.production, "确认需要工单处置")
        order = self.service.create_work_order(
            alarm["id"], "inspection", self.maintenance, self.production,
        )
        with self.assertRaises(WorkflowValidationError):
            self.service.transition_work_order(
                order["id"], "overdue", self.maintenance, "人工改为超时",
            )
        with self.assertRaises(WorkflowValidationError):
            self.service.list_work_orders({"status": "invented"})

    def test_active_order_prevents_cross_workflow_alarm_terminal_state(self):
        alarm = self._alarm()
        self.service.transition_alarm(alarm["id"], "confirmed", self.production, "确认异常有效")
        order = self.service.create_work_order(
            alarm["id"], "inspection", self.maintenance, self.production,
        )
        escalated = self.service.transition_alarm(
            alarm["id"], "escalated", self.production, "风险升级需加快处理",
        )
        self.assertEqual(escalated["status"], "escalated")
        for target in ("confirmed", "false_positive"):
            with self.assertRaises(WorkflowValidationError):
                self.service.transition_alarm(
                    alarm["id"], target, self.production, "不得脱离已创建的工单",
                )
        self.assertEqual(self.service.get_work_order(order["id"])["status"], "pending")

    def test_concurrent_creation_converges_without_raw_database_errors(self):
        def create_alarm(_):
            return WorkflowService(
                self.database, AuditService(self.database), clock=lambda: NOW,
            ).create_alarm(
                "diagnosis", self.diagnosis_id, "warning", {"condition": "supply_shortage"},
                actor_id=self.production,
            )["id"]

        with ThreadPoolExecutor(max_workers=6) as executor:
            alarm_ids = list(executor.map(create_alarm, range(12)))
        self.assertEqual(len(set(alarm_ids)), 1)
        alarm_id = alarm_ids[0]
        self.service.transition_alarm(alarm_id, "confirmed", self.production, "确认异常有效")

        def create_order(_):
            service = WorkflowService(
                self.database, AuditService(self.database), clock=lambda: NOW,
            )
            try:
                return "ok", service.create_work_order(
                    alarm_id, "inspection", self.maintenance, self.production,
                )["id"]
            except WorkflowValidationError as error:
                return "rejected", str(error)

        with ThreadPoolExecutor(max_workers=6) as executor:
            outcomes = list(executor.map(create_order, range(12)))
        created = [value for status, value in outcomes if status == "ok"]
        self.assertEqual(len(created), 1)
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM work_orders WHERE alarm_id=?", (alarm_id,),
        ), 1)

    def test_legacy_duplicate_links_migrate_without_data_loss(self):
        with self.database.transaction(
            self.production, "test.legacy_duplicates", "database", "workflow",
        ) as connection:
            for suffix in ("A", "B"):
                connection.execute(
                    """INSERT INTO alarms(
                           id,well_id,diagnosis_id,alarm_type,severity,details_json,
                           source_object_type,source_object_id,status,source_type
                       ) VALUES(?, 'W-001', ?, 'supply_shortage','warning','{}',
                                'diagnosis',?,'new','manual')""",
                    (f"AL-LEGACY-{suffix}", self.diagnosis_id, self.diagnosis_id),
                )
            for suffix in ("A", "B"):
                connection.execute(
                    """INSERT INTO work_orders(
                           id,well_id,alarm_id,title,status,source_type
                       ) VALUES(?,'W-001','AL-LEGACY-A','历史工单','pending','manual')""",
                    (f"WO-LEGACY-{suffix}",),
                )
        before = (
            self.database.scalar("SELECT COUNT(*) FROM alarms"),
            self.database.scalar("SELECT COUNT(*) FROM work_orders"),
        )
        self.database.migrate()
        self.assertEqual(before, (
            self.database.scalar("SELECT COUNT(*) FROM alarms"),
            self.database.scalar("SELECT COUNT(*) FROM work_orders"),
        ))
        self.assertIsNone(self.database.query_one("PRAGMA foreign_key_check"))
        migrated = self.database.query_all(
            """SELECT id,assignee_id,due_at FROM work_orders
               WHERE id IN ('WO-LEGACY-A','WO-LEGACY-B') ORDER BY id"""
        )
        self.assertTrue(all(
            row["assignee_id"] == self.maintenance and row["due_at"]
            for row in migrated
        ))
        for order in migrated:
            for target, actor, comment in (
                ("accepted", self.maintenance, "历史工单补录接单"),
                ("processing", self.maintenance, "历史工单现场处理"),
                ("review", self.maintenance, "历史工单提交复核"),
                ("closed", self.reviewer, "历史工单复核通过"),
            ):
                self.service.transition_work_order(order["id"], target, actor, comment)
        self.assertEqual(self.service.get_alarm("AL-LEGACY-A")["status"], "closed")

    def test_worker_marks_due_orders_overdue_without_api_call(self):
        alarm = self._alarm()
        self.service.transition_alarm(alarm["id"], "confirmed", self.production, "确认需要工单处置")
        order = self.service.create_work_order(
            alarm["id"], "inspection", self.maintenance, self.production,
            due_at=(NOW - timedelta(seconds=1)).isoformat(),
        )
        worker = WorkflowWorker(self.service, poll_seconds=.01)
        worker.start()
        self.addCleanup(worker.stop)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.service.get_work_order(order["id"])["status"] == "overdue":
                break
            time.sleep(.01)
        worker.stop()
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.service.get_work_order(order["id"])["status"], "overdue")

    def test_audit_failure_rolls_back_business_and_log_changes(self):
        alarm = self._alarm()
        self.service.transition_alarm(alarm["id"], "confirmed", self.production, "确认异常工况有效")
        order = self.service.create_work_order(
            alarm["id"], "production_adjustment", self.maintenance, self.production,
        )
        service = WorkflowService(self.database, Mock(record=Mock(side_effect=RuntimeError("audit unavailable"))))
        before_logs = self.database.scalar(
            "SELECT COUNT(*) FROM work_order_logs WHERE work_order_id=?", (order["id"],),
        )
        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            service.transition_work_order(order["id"], "accepted", self.maintenance, "接单处置")
        self.assertEqual(self.service.get_work_order(order["id"])["status"], "pending")
        self.assertEqual(self.database.scalar(
            "SELECT COUNT(*) FROM work_order_logs WHERE work_order_id=?", (order["id"],),
        ), before_logs)


class WorkflowHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = create_server(
            "127.0.0.1", 0, database_path=Path(self.temporary.name) / "platform.db",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        self.production = self._login("production_engineer", "Production@2026")
        self.maintenance = self._login("maintenance_engineer", "Maintenance@2026")
        self.reviewer = self._login("reviewer", "Review@2026")
        actor = self.server.database.scalar(
            "SELECT id FROM users WHERE username='algorithm_engineer'"
        )
        with self.server.database.transaction(
            actor, "test.diagnosis", "diagnosis", "DX-HTTP-WORKFLOW",
        ) as connection:
            connection.execute(
                """INSERT INTO diagnoses(
                       id,well_id,diagnosis_type,confidence,evidence,recommendation,
                       status,actor_id,source_type
                   ) VALUES('DX-HTTP-WORKFLOW','W-001','pump_leak',.96,'[]',
                            '检查阀组密封','new',?,'algorithm')""",
                (actor,),
            )

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
        status, payload = self._request(
            "POST", "/api/auth/login", body={"username": username, "password": password},
        )
        self.assertEqual(status, 200)
        return payload["data"]["token"]

    def test_workflow_routes_enforce_roles_validation_and_return_trace(self):
        create_body = {
            "source_type": "diagnosis", "source_id": "DX-HTTP-WORKFLOW",
            "severity": "critical", "details": {"condition": "pump_leak"},
        }
        self.assertEqual(self._request("POST", "/api/alarms", self.reviewer, create_body)[0], 403)
        status, created = self._request("POST", "/api/alarms", self.production, create_body)
        self.assertEqual(status, 201)
        alarm = created["data"]
        self.assertEqual(self._request(
            "POST", f"/api/alarms/{alarm['id']}/transition?bad=1", self.production,
            {"target": "confirmed", "comment": "确认阀漏"},
        )[0], 422)
        status, confirmed = self._request(
            "POST", f"/api/alarms/{alarm['id']}/transition", self.production,
            {"target": "confirmed", "comment": "确认阀漏异常"},
        )
        self.assertEqual((status, confirmed["data"]["status"]), (200, "confirmed"))
        status, created_order = self._request(
            "POST", "/api/work-orders", self.production,
            {"alarm_id": alarm["id"], "kind": "equipment_repair",
             "assignee_id": self.server.database.scalar(
                 "SELECT id FROM users WHERE username='maintenance_engineer'"
             )},
        )
        self.assertEqual(status, 201)
        order = created_order["data"]
        status, assigned = self._request(
            "GET", f"/api/work-orders?assignee_id={order['assignee_id']}&page_size=5",
            self.maintenance,
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(item["id"] == order["id"] for item in assigned["data"]["items"]))
        self.assertEqual(self._request(
            "GET", f"/api/work-orders/{order['id']}", self.maintenance,
        )[0], 200)
        self.assertEqual(self._request(
            "GET", f"/api/alarms/{alarm['id']}", self.maintenance,
        )[0], 200)
        for target, token, comment in (
            ("accepted", self.maintenance, "已接单准备检修"),
            ("processing", self.maintenance, "正在检查泵阀"),
            ("review", self.maintenance, "阀组更换完成待复核"),
        ):
            status, changed = self._request(
                "POST", f"/api/work-orders/{order['id']}/transition", token,
                {"target": target, "comment": comment},
            )
            self.assertEqual((status, changed["data"]["status"]), (200, target))
        self.assertEqual(self._request(
            "POST", f"/api/work-orders/{order['id']}/transition", self.maintenance,
            {"target": "closed", "comment": "本人关闭"},
        )[0], 403)
        status, closed = self._request(
            "POST", f"/api/work-orders/{order['id']}/transition", self.reviewer,
            {"target": "closed", "comment": "复核合格，运行参数已恢复"},
        )
        self.assertEqual((status, closed["data"]["status"]), (200, "closed"))
        status, trace = self._request(
            "GET", f"/api/work-orders/{order['id']}/trace", self.reviewer,
        )
        self.assertEqual(status, 200)
        self.assertEqual(trace["data"]["diagnosis_id"], "DX-HTTP-WORKFLOW")
        self.assertGreaterEqual(len(trace["data"]["audit_ids"]), 8)
        status, listing = self._request(
            "GET", "/api/alarms?well_id=W-001&severity=critical&page_size=5", self.reviewer,
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(item["id"] == alarm["id"] for item in listing["data"]["items"]))


if __name__ == "__main__":
    unittest.main()
