"""Computed operational reports, energy reconciliation and CSV HTTP tests."""

from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import quote

from backend.database import Database
from backend.http_server import create_server
from backend.seed import seed_database
from backend.services.reports import ReportNotFound, ReportService, ReportValidationError


SHANGHAI = timezone(timedelta(hours=8))
EFFECTIVE_AT = datetime(2026, 6, 3, tzinfo=SHANGHAI)
EFFECT_END = datetime(2026, 6, 5, tzinfo=SHANGHAI)


def replace_strategy_measurements(database):
    strategy = database.query_one(
        """SELECT strategy.id,strategy.well_id
           FROM energy_strategies AS strategy
           WHERE strategy.status='effective'
           ORDER BY strategy.id LIMIT 1"""
    )
    if strategy is None:
        strategy = database.query_one(
            "SELECT id,well_id FROM energy_strategies ORDER BY id LIMIT 1"
        )
    with database.transaction(
        None, "test.report_measurements", "energy_strategy", strategy["id"]
    ) as connection:
        connection.execute(
            "UPDATE energy_strategies SET status='effective',effective_at=? WHERE id=?",
            (EFFECTIVE_AT.isoformat(), strategy["id"]),
        )
        connection.execute(
            "UPDATE data_quality_events SET telemetry_id=NULL WHERE well_id=?",
            (strategy["well_id"],),
        )
        connection.execute("DELETE FROM telemetry WHERE well_id=?", (strategy["well_id"],))
        connection.execute("DELETE FROM production_daily WHERE well_id=?", (strategy["well_id"],))
        device = connection.execute(
            "SELECT id FROM devices WHERE well_id=? ORDER BY id LIMIT 1",
            (strategy["well_id"],),
        ).fetchone()["id"]
        samples = []
        point = datetime(2026, 6, 1, tzinfo=SHANGHAI)
        for hour in range(97):
            if hour < 48:
                power = 4.0
            elif hour < 96:
                power = 4.0 if (hour - 48) % 2 == 0 else 0.0
            else:
                power = 0.0
            measured_at = (point + timedelta(hours=hour)).isoformat()
            samples.append((strategy["well_id"], device, measured_at, power))
        connection.executemany(
            """INSERT INTO telemetry(
                   well_id,device_id,measured_at,metric_code,value,unit,status,source_type
               ) VALUES(?,?,?,'power_kw',?,'kW','valid','virtual_device')""",
            samples,
        )
        daily = []
        for offset in range(4):
            after = offset >= 2
            day = (point + timedelta(days=offset)).date().isoformat()
            daily.append((
                strategy["well_id"], day, 3.0 if after else 3.2,
                5.8 if after else 6.0, 48.0 if after else 96.0,
                0.61, 2.0, 32.0, 12.0 if after else 24.0,
            ))
        connection.executemany(
            """INSERT INTO production_daily(
                   well_id,day,daily_oil,daily_fluid,power_kwh,pump_efficiency,
                   stroke_m,frequency_hz,runtime_hours,status,source_type
               ) VALUES(?,?,?,?,?,?,?,?,?,'valid','virtual_device')""",
            daily,
        )
    return strategy["id"], strategy["well_id"]


class ReportServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "platform.db")
        self.database.migrate()
        seed_database(self.database)
        self.service = ReportService(
            self.database,
            clock=lambda: datetime(2026, 7, 1, 8, 0, tzinfo=SHANGHAI),
        )

    def test_energy_effect_reconciles_equal_windows_to_source_measurements(self):
        strategy_id, well_id = replace_strategy_measurements(self.database)
        effect = self.service.energy_effect(strategy_id, EFFECTIVE_AT, EFFECT_END)
        self.assertEqual(effect.strategy_id, strategy_id)
        self.assertEqual(effect.well_id, well_id)
        self.assertAlmostEqual(effect.baseline_energy_kwh, 192.0, places=6)
        self.assertAlmostEqual(effect.after_energy_kwh, 96.0, places=6)
        self.assertAlmostEqual(effect.energy_saved_kwh, 96.0, places=6)
        self.assertAlmostEqual(effect.saving_rate, 0.5, places=6)
        self.assertAlmostEqual(effect.baseline_runtime_hours, 48.0, places=6)
        self.assertAlmostEqual(effect.after_runtime_hours, 24.0, places=6)
        self.assertAlmostEqual(effect.baseline_liquid_m3, 12.0, places=6)
        self.assertAlmostEqual(effect.after_liquid_m3, 11.6, places=6)
        self.assertEqual(effect.after_start_count, 23)
        self.assertEqual(
            effect.source_tables,
            (
                "telemetry", "production_daily", "energy_strategies",
                "strategy_versions", "device_commands",
            ),
        )

    def test_energy_report_accepts_a_window_starting_after_effective_at(self):
        strategy_id, _ = replace_strategy_measurements(self.database)
        start = EFFECTIVE_AT + timedelta(days=1)
        report = self.service.report("energy", {
            "strategy_id": strategy_id,
            "from": start.isoformat(),
            "to": EFFECT_END.isoformat(),
        })
        self.assertEqual(report["summary"]["strategy_count"], 1)
        self.assertAlmostEqual(report["summary"]["after_energy_kwh"], 48.0, places=6)
        self.assertAlmostEqual(report["summary"]["saving_rate"], .5, places=6)

    def test_seeded_effect_history_shows_measured_savings_with_retained_liquid(self):
        report = self.service.report("energy", {})
        self.assertGreaterEqual(report["summary"]["strategy_count"], 2)
        self.assertGreater(report["summary"]["saving_rate"], .15)
        self.assertGreater(
            report["summary"]["after_liquid_m3"],
            report["summary"]["baseline_liquid_m3"] * .95,
        )
        self.assertTrue(all(
            item["baseline_coverage_rate"] == 1.0
            and item["after_coverage_rate"] == 1.0
            for item in report["items"]
        ))

    def test_production_report_uses_source_rows_and_preserves_filters(self):
        with self.database.transaction(
            None, "test.remove_stored_reports", "report", "all"
        ) as connection:
            connection.execute("DELETE FROM reports")
        filters = {"unit_id": "U-01", "from": "2026-06-01", "to": "2026-06-02"}
        report = self.service.report("production", filters)
        expected = self.database.scalar(
            """SELECT SUM(p.daily_fluid) FROM production_daily p
               JOIN wells w ON w.id=p.well_id
               JOIN well_groups g ON g.id=w.group_id
               WHERE g.unit_id='U-01' AND p.day BETWEEN '2026-06-01' AND '2026-06-02'"""
        )
        self.assertEqual(report["report_type"], "production")
        self.assertEqual(report["filters"]["unit_id"], "U-01")
        self.assertEqual(report["source_tables"], ["production_daily"])
        self.assertAlmostEqual(report["summary"]["daily_fluid_m3"], expected, places=6)
        self.assertEqual(report["summary"]["well_count"], 12)
        self.assertEqual(len(report["items"]), 24)
        self.assertTrue(all(item["unit_id"] == "U-01" for item in report["items"]))

    def test_all_six_reports_are_computed_and_trace_their_sources(self):
        strategy_id, _ = replace_strategy_measurements(self.database)
        filters_by_type = {
            "production": {"unit_id": "U-01"},
            "diagnosis": {"status": "reviewed"},
            "data-quality": {"status": "closed"},
            "strategy": {"status": "effective"},
            "energy": {
                "strategy_id": strategy_id,
                "from": EFFECTIVE_AT.isoformat(),
                "to": EFFECT_END.isoformat(),
            },
            "work-orders": {},
        }
        for report_type, filters in filters_by_type.items():
            with self.subTest(report_type=report_type):
                report = self.service.report(report_type, filters)
                self.assertEqual(report["report_type"], report_type)
                self.assertTrue(report["source_tables"])
                self.assertIn("summary", report)
                self.assertIn("items", report)
                self.assertTrue(report["generated_at"].endswith("+08:00"))
        energy = self.service.report("energy", filters_by_type["energy"])
        self.assertEqual(energy["items"][0]["strategy_id"], strategy_id)
        self.assertAlmostEqual(energy["summary"]["energy_saved_kwh"], 96.0)

    def test_csv_has_utf8_bom_filter_columns_and_escaped_values(self):
        content = self.service.csv(
            "production", {"unit_id": "U-01", "from": "2026-06-01", "to": "2026-06-01"}
        )
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        decoded = content.decode("utf-8-sig")
        self.assertIn("report_type,production", decoded)
        self.assertIn("filter.unit_id,U-01", decoded)
        self.assertIn("unit_id", decoded)
        self.assertIn("U-01", decoded)

    def test_incomplete_power_coverage_never_becomes_claimed_savings(self):
        strategy_id, well_id = replace_strategy_measurements(self.database)
        with self.database.transaction(
            None, "test.report_gap", "telemetry", well_id
        ) as connection:
            connection.execute(
                """DELETE FROM telemetry WHERE well_id=? AND metric_code='power_kw'
                   AND datetime(measured_at)>=datetime('2026-06-03T12:00:00+08:00')
                   AND datetime(measured_at)<=datetime('2026-06-04T00:00:00+08:00')""",
                (well_id,),
            )
        effect = self.service.energy_effect(strategy_id, EFFECTIVE_AT, EFFECT_END)
        self.assertFalse(effect.valid_for_comparison)
        self.assertIsNone(effect.saving_rate)
        self.assertIsNone(effect.energy_saved_kwh)
        self.assertIn("evaluation_power_coverage_below_95_percent", effect.validation_issues)
        report = self.service.report("energy", {
            "strategy_id": strategy_id,
            "from": EFFECTIVE_AT.isoformat(),
            "to": EFFECT_END.isoformat(),
        })
        self.assertEqual(report["summary"]["comparable_strategy_count"], 0)
        self.assertIsNone(report["summary"]["saving_rate"])

    def test_six_hour_gap_cannot_hide_behind_a_fixed_hold_limit(self):
        strategy_id, well_id = replace_strategy_measurements(self.database)
        with self.database.transaction(
            None, "test.short_report_gap", "telemetry", well_id
        ) as connection:
            connection.execute(
                """DELETE FROM telemetry WHERE well_id=? AND metric_code='power_kw'
                   AND datetime(measured_at)>=datetime('2026-06-03T01:00:00+08:00')
                   AND datetime(measured_at)<=datetime('2026-06-03T05:00:00+08:00')""",
                (well_id,),
            )
        effect = self.service.energy_effect(strategy_id, EFFECTIVE_AT, EFFECT_END)
        self.assertLess(effect.after_coverage_rate, .95)
        self.assertFalse(effect.valid_for_comparison)
        self.assertIsNone(effect.saving_rate)

    def test_incomplete_production_coverage_never_claims_a_liquid_gain(self):
        strategy_id, well_id = replace_strategy_measurements(self.database)
        with self.database.transaction(
            None, "test.production_gap", "production_daily", well_id
        ) as connection:
            connection.execute(
                "DELETE FROM production_daily WHERE well_id=? AND day='2026-06-01'",
                (well_id,),
            )
        effect = self.service.energy_effect(strategy_id, EFFECTIVE_AT, EFFECT_END)
        self.assertTrue(effect.valid_for_comparison)
        self.assertFalse(effect.liquid_valid_for_comparison)
        self.assertIsNone(effect.liquid_change_rate)
        self.assertEqual(effect.baseline_production_coverage_rate, .5)
        self.assertIn(
            "baseline_production_coverage_below_95_percent",
            effect.liquid_validation_issues,
        )
        report = self.service.report("energy", {
            "strategy_id": strategy_id,
            "from": EFFECTIVE_AT.isoformat(),
            "to": EFFECT_END.isoformat(),
        })
        self.assertEqual(report["summary"]["production_comparable_strategy_count"], 0)
        self.assertEqual(report["summary"]["baseline_liquid_m3"], 0.0)

    def test_same_well_strategy_periods_are_cut_at_next_activation_and_rollback(self):
        parent_id, well_id = replace_strategy_measurements(self.database)
        child_id = "STR-REPORT-CHILD"
        child_at = EFFECTIVE_AT + timedelta(days=1)
        rollback_at = child_at + timedelta(hours=12)
        device_id = self.database.scalar(
            "SELECT id FROM devices WHERE well_id=? ORDER BY id LIMIT 1", (well_id,)
        )
        with self.database.transaction(
            None, "test.report_periods", "energy_strategy", child_id
        ) as connection:
            connection.execute(
                """INSERT INTO energy_strategies(
                       id,well_id,parent_strategy_id,name,effective_at,status,source_type
                   ) VALUES(?,?,?,'复核策略',?,'rolled_back','manual')""",
                (child_id, well_id, parent_id, child_at.isoformat()),
            )
            connection.execute(
                """INSERT INTO device_commands(
                       id,device_id,strategy_id,command_type,payload,completed_at,status,source_type
                   ) VALUES('CMD-REPORT-ROLLBACK',?,?,'rollback_cycle','{}',?,'success','system')""",
                (device_id, child_id, rollback_at.isoformat()),
            )
        report = self.service.report("energy", {"well_id": well_id})
        self.assertEqual(report["summary"]["strategy_count"], 2)
        self.assertAlmostEqual(report["summary"]["after_energy_kwh"], 72.0, places=6)
        child = next(item for item in report["items"] if item["strategy_id"] == child_id)
        self.assertEqual(child["evaluation_to"], rollback_at.isoformat())

    def test_work_order_integer_bound_and_csv_control_prefixes_fail_safe(self):
        with self.assertRaises(ReportValidationError):
            self.service.report("work-orders", {"assignee_id": str(2**63)})
        with self.database.transaction(
            None, "test.csv_prefix", "data_quality_event", "first"
        ) as connection:
            connection.execute(
                "UPDATE data_quality_events SET detail=? WHERE id=(SELECT MIN(id) FROM data_quality_events)",
                ("\t=1+1",),
            )
        decoded = self.service.csv("data-quality", {}).decode("utf-8-sig")
        self.assertIn("'\t=1+1", decoded)
        self.assertEqual(ReportService._csv_value("\r=1+1"), "'\r=1+1")

    def test_legacy_completed_orders_remain_open_until_independent_review(self):
        report = self.service.report("work-orders", {})
        completed = report["summary"]["counts_by_status"].get("completed", 0)
        self.assertGreater(completed, 0)
        self.assertEqual(
            report["summary"]["closed_count"],
            report["summary"]["counts_by_status"].get("closed", 0),
        )
        self.assertEqual(
            report["summary"]["open_count"],
            report["summary"]["work_order_count"] - report["summary"]["closed_count"],
        )

    def test_invalid_type_scope_time_and_non_effective_strategy_fail_closed(self):
        with self.assertRaises(ReportNotFound):
            self.service.report("unknown", {})
        for filters in (
            {"unexpected": "x"},
            {"unit_id": "../U-01"},
            {"from": "2026-06-02", "to": "2026-06-01"},
            {"from": "not-a-time"},
        ):
            with self.subTest(filters=filters), self.assertRaises(ReportValidationError):
                self.service.report("production", filters)
        draft = self.database.scalar(
            "SELECT id FROM energy_strategies WHERE status='draft' ORDER BY id LIMIT 1"
        )
        with self.assertRaises(ReportValidationError):
            self.service.energy_effect(draft, EFFECTIVE_AT, EFFECT_END)

    def test_all_report_types_reject_unrepresentable_date_boundaries(self):
        invalid_filters = (
            {"to": "9999-12-31"},
            {"from": "0001-01-01T00:00:00+14:00"},
            {"to": "9999-12-31T23:59:59-14:00"},
        )
        for report_type in (
            "production", "diagnosis", "data-quality", "strategy", "energy", "work-orders"
        ):
            for filters in invalid_filters:
                with self.subTest(report_type=report_type, filters=filters):
                    with self.assertRaises(ReportValidationError):
                        self.service.report(report_type, filters)


class ReportHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = create_server(
            "127.0.0.1", 0, database_path=Path(self.temporary.name) / "platform.db"
        )
        self.strategy_id, _ = replace_strategy_measurements(self.server.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        self.reviewer = self._login("reviewer", "Review@2026")
        self.maintenance = self._login("maintenance_engineer", "Maintenance@2026")

    def _close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def _request(self, method, path, token=None, body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def _login(self, username, password):
        status, _, raw = self._request(
            "POST", "/api/auth/login", body={"username": username, "password": password}
        )
        self.assertEqual(status, 200)
        return json.loads(raw.decode("utf-8"))["data"]["token"]

    def test_json_and_csv_routes_enforce_permission_and_validate_queries(self):
        self.assertEqual(self._request("GET", "/api/reports/production")[0], 401)
        self.assertEqual(
            self._request("GET", "/api/reports/production", self.maintenance)[0], 403
        )
        for report_type in (
            "production", "diagnosis", "data-quality", "strategy", "work-orders"
        ):
            status, headers, raw = self._request(
                "GET", f"/api/reports/{report_type}", self.reviewer
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw.decode("utf-8"))["data"]["report_type"], report_type)
            self.assertIn("application/json", headers["Content-Type"])
        query = "from=" + quote(EFFECTIVE_AT.isoformat()) + "&to=" + quote(EFFECT_END.isoformat())
        status, _, raw = self._request(
            "GET", f"/api/reports/energy?strategy_id={self.strategy_id}&{query}", self.reviewer
        )
        self.assertEqual(status, 200)
        self.assertAlmostEqual(json.loads(raw.decode("utf-8"))["data"]["summary"]["saving_rate"], .5)
        status, headers, raw = self._request(
            "GET", "/api/reports/production.csv?unit_id=U-01", self.reviewer
        )
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(
            self._request("GET", "/api/reports/production?unit_id=U-01&unit_id=U-02", self.reviewer)[0],
            422,
        )
        self.assertEqual(
            self._request(
                "GET", "/api/reports/work-orders?assignee_id=9223372036854775808",
                self.reviewer,
            )[0],
            422,
        )
        self.assertEqual(self._request("GET", "/api/reports/unknown", self.reviewer)[0], 404)

    def test_unrepresentable_report_date_is_a_json_validation_error(self):
        status, headers, raw = self._request(
            "GET", "/api/reports/energy?to=9999-12-31", self.reviewer
        )
        self.assertEqual(status, 422)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(raw.decode("utf-8"))["error"]["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
