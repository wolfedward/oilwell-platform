import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from backend.http_server import create_server
from backend.seed import _previous_content_hash


class WellApiTests(unittest.TestCase):
    """HTTP contracts for data-backed dashboard and well read models."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "platform.db"
        self.server = create_server("127.0.0.1", 0, database_path=self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, payload=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        try:
            body = None if payload is None else json.dumps(payload).encode("utf-8")
            headers = {} if body is None else {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def login(self, username="administrator", password="Admin@2026"):
        response, payload = self.request("POST", "/api/auth/login", {"username": username, "password": password})
        self.assertEqual(response.status, 200)
        return payload["data"]["token"]

    def test_dashboard_is_authenticated_and_matches_latest_daily_sql(self):
        response, payload = self.request("GET", "/api/dashboard")
        self.assertEqual((response.status, payload["error"]["code"]), (401, "unauthenticated"))
        token = self.login()
        response, payload = self.request("GET", "/api/dashboard", token=token)
        self.assertEqual(response.status, 200)
        data = payload["data"]
        latest_day = self.server.database.scalar("SELECT MAX(day) FROM production_daily")
        expected = self.server.database.query_one(
            "SELECT SUM(daily_fluid), SUM(daily_oil), SUM(power_kwh), AVG(pump_efficiency) "
            "FROM production_daily WHERE day = ?", (latest_day,)
        )
        self.assertEqual(data["data_day"], latest_day)
        self.assertEqual(data["total_wells"], 48)
        self.assertAlmostEqual(data["daily_fluid"], expected[0], places=4)
        self.assertAlmostEqual(data["daily_oil"], expected[1], places=4)
        self.assertAlmostEqual(data["power_kwh"], expected[2], places=4)
        self.assertAlmostEqual(data["pump_efficiency"], expected[3], places=4)

    def test_all_seeded_roles_have_the_declared_read_models_but_not_all_can_write(self):
        """Omitting a newly introduced grant must not make a seeded role unusable."""
        accounts = (
            ("administrator", "Admin@2026"), ("production_engineer", "Production@2026"),
            ("algorithm_engineer", "Algorithm@2026"), ("maintenance_engineer", "Maintenance@2026"),
            ("reviewer", "Review@2026"),
        )
        for username, password in accounts:
            with self.subTest(username=username):
                token = self.login(username, password)
                for path in ("/api/dashboard", "/api/wells?page_size=1", "/api/wells/W-001/telemetry?interval=1d"):
                    response, _ = self.request("GET", path, token=token)
                    self.assertEqual(response.status, 200, path)

    def test_well_list_filters_paginates_and_treats_wildcards_as_literals(self):
        token = self.login()
        response, payload = self.request("GET", "/api/wells?unit_id=U-01&status=running&page=1&page_size=2", token=token)
        self.assertEqual(response.status, 200)
        data = payload["data"]
        self.assertEqual((data["page"], data["page_size"], len(data["items"])), (1, 2, 2))
        self.assertGreater(data["total"], 2)
        self.assertEqual(data["pages"], (data["total"] + 1) // 2)
        self.assertTrue(all(item["unit"]["id"] == "U-01" and item["well"]["status"] == "running" for item in data["items"]))
        response, injection = self.request("GET", "/api/wells?keyword=%25&page=1&page_size=100", token=token)
        self.assertEqual(response.status, 200)
        self.assertEqual(injection["data"]["total"], 0)
        response, injection = self.request("GET", "/api/wells?keyword=%27%20OR%201%3D1--&page=1&page_size=100", token=token)
        self.assertEqual(response.status, 200)
        self.assertEqual(injection["data"]["total"], 0)
        response, invalid = self.request("GET", "/api/wells?page=0", token=token)
        self.assertEqual((response.status, invalid["error"]["code"]), (422, "validation_error"))

    def test_well_detail_links_only_its_business_objects_and_empty_chain_is_null(self):
        token = self.login()
        response, payload = self.request("GET", "/api/wells/W-001", token=token)
        self.assertEqual(response.status, 200)
        data = payload["data"]
        self.assertEqual(data["well"]["id"], "W-001")
        self.assertEqual(data["device"]["well_id"], "W-001")
        self.assertEqual(data["latest_telemetry"]["well_id"], "W-001")
        self.assertEqual(data["latest_diagnosis"]["well_id"], "W-001")
        self.assertIn("active_strategy", data)
        response, other = self.request("GET", "/api/wells/W-002", token=token)
        self.assertEqual(response.status, 200)
        self.assertIsNone(other["data"]["active_strategy"])
        self.assertEqual(other["data"]["active_alarms"], [])
        response, missing = self.request("GET", "/api/wells/nope", token=token)
        self.assertEqual((response.status, missing["error"]["code"]), (404, "not_found"))

    def test_telemetry_buckets_use_seed_history_timezone_and_validate_ranges(self):
        token = self.login()
        response, payload = self.request("GET", "/api/wells/W-001/telemetry", token=token)
        self.assertEqual(response.status, 200)
        self.assertGreater(len(payload["data"]["items"]), 0)
        self.assertTrue(payload["data"]["range"]["to"].endswith("+08:00"))
        for interval in ("raw", "15m", "1h", "1d"):
            response, sampled = self.request(
                "GET", "/api/wells/W-001/telemetry?from=2026-06-01T00:00:00%2B08:00&to=2026-06-02T00:00:00%2B08:00&interval=" + interval,
                token=token,
            )
            self.assertEqual(response.status, 200, interval)
            self.assertTrue(all("bucket" in item and "count" in item for item in sampled["data"]["items"]))
        for path in (
            "/api/wells/W-001/telemetry?from=not-a-time",
            "/api/wells/W-001/telemetry?from=2026-06-02T00:00:00%2B08:00&to=2026-06-01T00:00:00%2B08:00",
            "/api/wells/W-001/telemetry?interval=week",
        ):
            response, invalid = self.request("GET", path, token=token)
            self.assertEqual((response.status, invalid["error"]["code"]), (422, "validation_error"))

    def test_status_write_is_audited_versioned_permissioned_and_survives_restart(self):
        # Simulate the exact Task 4 manifest projection that included
        # wells.status, then force the one-time Task 5 hash migration.
        old_hash = _previous_content_hash(self.server.database, 202606, "")
        with self.server.database.transaction(None, "test.old_projection", "seed_manifest", "acceptance-history"):
            self.server.database.execute("UPDATE seed_manifests SET content_hash=? WHERE dataset_name=?", (old_hash, "acceptance-history"))
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        migrated = create_server("127.0.0.1", 0, database_path=self.path)
        migrated_thread = threading.Thread(target=migrated.serve_forever, daemon=True)
        migrated_thread.start()
        self.addCleanup(migrated.server_close)
        self.addCleanup(migrated_thread.join)
        self.addCleanup(migrated.shutdown)
        self.server, self.thread = migrated, migrated_thread
        token = self.login()
        response, detail = self.request("GET", "/api/wells/W-001", token=token)
        version = detail["data"]["well"]["version"]
        response, stale = self.request("PATCH", "/api/wells/W-001/status", {"status": "stopped", "reason": "检修", "version": -1}, token)
        self.assertEqual((response.status, stale["error"]["code"]), (409, "version_conflict"))
        response, updated = self.request("PATCH", "/api/wells/W-001/status", {"status": "stopped", "reason": "检修", "version": version}, token)
        self.assertEqual(response.status, 200)
        self.assertEqual((updated["data"]["status"], updated["data"]["version"]), ("stopped", version + 1))
        audit = self.server.database.query_one("SELECT details_json FROM audit_logs WHERE action = 'well.status.update' ORDER BY id DESC LIMIT 1")
        self.assertIn('"reason":"检修"', audit["details_json"])
        reviewer = self.login("reviewer", "Review@2026")
        response, forbidden = self.request("PATCH", "/api/wells/W-001/status", {"status": "maintenance", "reason": "检修", "version": version + 1}, reviewer)
        self.assertEqual((response.status, forbidden["error"]["code"]), (403, "forbidden"))
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        replacement = create_server("127.0.0.1", 0, database_path=self.path)
        self.addCleanup(replacement.server_close)
        replacement_thread = threading.Thread(target=replacement.serve_forever, daemon=True)
        replacement_thread.start()
        self.addCleanup(replacement_thread.join)
        self.addCleanup(replacement.shutdown)
        self.server = replacement
        self.thread = replacement_thread
        replacement_token = self.login()
        response, persisted = self.request("GET", "/api/wells/W-001", token=replacement_token)
        self.assertEqual((response.status, persisted["data"]["well"]["status"]), (200, "stopped"))

    def test_tampered_immutable_seed_data_fails_closed_after_mutable_status_change(self):
        token = self.login()
        version = self.server.database.query_one("SELECT version FROM wells WHERE id = 'W-001'")["version"]
        response, _ = self.request("PATCH", "/api/wells/W-001/status", {"status": "stopped", "reason": "检修", "version": version}, token)
        self.assertEqual(response.status, 200)
        with self.server.database.transaction(1, "test.tamper", "production_daily", "W-001"):
            self.server.database.execute("UPDATE production_daily SET power_kwh = power_kwh + 1 WHERE well_id = ? AND day = ?", ("W-001", "2026-04-02"))
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            create_server("127.0.0.1", 0, database_path=self.path)


if __name__ == "__main__":
    unittest.main()
