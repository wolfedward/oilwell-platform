"""Traceable dynagraph analysis persistence and HTTP workflow tests."""

from datetime import datetime, timedelta, timezone
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
from backend.protocol import encode_frame
from backend.seed import seed_database
from backend.services.acquisition import AcquisitionService
from backend.services.analysis import (
    AnalysisNotFound,
    AnalysisService,
    AnalysisValidationError,
    AnalysisVersionConflict,
)
from backend.simulator import SimulatorRegistry
from tests.test_dynagraph import fixture


SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 6, 22, 10, 0, tzinfo=SHANGHAI)


def electric_payload(name="normal", *, legacy_phase=False):
    samples, params = fixture(name)
    return {
        "kind": "electric_card",
        "status": "online",
        "electric_samples": [
            {
                "phase": float(index) if legacy_phase else sample.phase,
                "current": sample.current_a,
                "voltage": sample.voltage_v,
                "power": sample.active_power_kw,
            }
            for index, sample in enumerate(samples)
        ],
        "mechanism": {
            "stroke_m": params.stroke_m,
            "strokes_per_minute": params.strokes_per_minute,
        },
        "connection_mode": "virtual",
        "hardware_connected": False,
    }


class AnalysisServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Database(Path(self.temporary.name) / "platform.db")
        self.database.migrate()
        seed_database(self.database)
        self.registry = SimulatorRegistry(self.database, clock=lambda: NOW)
        self.acquisition = AcquisitionService(self.database, self.registry, clock=lambda: NOW)
        self.service = AnalysisService(self.database, AuditService(self.database))
        self.actor_id = self.database.scalar(
            "SELECT id FROM users WHERE username='algorithm_engineer'"
        )

    def ingest_card(self, name="normal", *, sequence=100):
        result = self.acquisition.ingest(
            encode_frame("D-001", sequence, electric_payload(name), timestamp=NOW)
        )
        self.assertTrue(result.accepted)
        row = self.database.query_one(
            "SELECT * FROM dynagraphs WHERE raw_message_id=?", (result.raw_message_id,)
        )
        self.assertIsNotNone(row)
        return row

    def test_analysis_schema_and_legacy_raw_backfill_are_idempotent(self):
        for table in ("diagnosis_templates", "production_estimates", "diagnosis_reviews"):
            self.assertIn(table, self.database.table_names())
        dynagraph_columns = {
            row[1] for row in self.database.query_all("PRAGMA table_info(dynagraphs)")
        }
        self.assertTrue({
            "raw_message_id", "device_id", "conversion_json", "features_json",
            "latest_model_run_id", "version",
        }.issubset(dynagraph_columns))

        with self.database.transaction(None, "test.legacy", "raw_message", None) as conn:
            raw_id = conn.execute(
                """INSERT INTO raw_messages(
                       device_id, claimed_device_id, received_at, reported_at, payload,
                       status, source_type, frame_kind, sequence_text, sequence_number
                   ) VALUES('D-001','D-001',?,?,?,?, 'virtual_device','electric_card','900',900)""",
                (NOW.isoformat(), NOW.isoformat(), json.dumps(electric_payload(legacy_phase=True)), "accepted"),
            ).lastrowid
        self.database.migrate()
        self.database.migrate()
        rows = self.database.query_all(
            "SELECT id,raw_message_id,sample_data FROM dynagraphs WHERE raw_message_id=?", (raw_id,)
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["sample_data"])["electric_samples"][1]["phase"], 1.0)

    def test_ingest_materializes_exact_traceable_dynagraph(self):
        row = self.ingest_card()
        self.assertEqual((row["well_id"], row["device_id"], row["status"]), ("W-001", "D-001", "unprocessed"))
        raw = self.database.query_one("SELECT * FROM raw_messages WHERE id=?", (row["raw_message_id"],))
        self.assertEqual(row["sample_data"], raw["payload"])
        self.assertEqual(row["captured_at"], raw["reported_at"])

    def test_analyze_persists_one_traceable_bundle_and_audit(self):
        dynagraph = self.ingest_card("pump_leak")
        data = self.service.run(dynagraph["id"], self.actor_id)

        self.assertEqual(len(data["converted_points"]), 120)
        self.assertIn("energy", data)
        self.assertEqual(data["model_run"]["model_version"], "dynagraph-2.0")
        self.assertEqual(data["diagnosis"]["condition"], "pump_leak")
        self.assertGreaterEqual(data["diagnosis"]["confidence"], .75)
        self.assertTrue(data["diagnosis"]["all_rule_hits"])
        self.assertGreater(data["production"]["estimated_m3_day"], 0.0)
        self.assertEqual(
            data["production"]["fillage_reference_load_span_kn"],
            data["parameters"]["full_load_span_kn"],
        )
        self.assertEqual(
            self.database.scalar("SELECT COUNT(*) FROM dynagraph_features WHERE dynagraph_id=?", (dynagraph["id"],)),
            11,
        )
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM model_runs WHERE id=?", (data["model_run"]["id"],)), 1)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM diagnoses WHERE id=?", (data["diagnosis"]["id"],)), 1)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM production_estimates WHERE model_run_id=?", (data["model_run"]["id"],)), 1)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM audit_logs WHERE action='analysis.run' AND object_id=?", (dynagraph["id"],)), 1)
        model = self.database.query_one("SELECT * FROM model_runs WHERE id=?", (data["model_run"]["id"],))
        self.assertEqual(json.loads(model["input_snapshot"])["raw_message_id"], dynagraph["raw_message_id"])
        self.assertEqual(json.loads(model["parameters_json"])["full_load_span_kn"], 25.0)
        self.assertEqual(json.loads(model["output_snapshot"])["diagnosis"]["condition"], "pump_leak")
        self.assertTrue(json.loads(model["warnings_json"]))
        template = self.database.query_one(
            "SELECT * FROM diagnosis_templates WHERE id=?", (self.database.scalar(
                "SELECT template_id FROM diagnoses WHERE id=?", (data["diagnosis"]["id"],)
            ),)
        )
        self.assertEqual(template["version"], data["diagnosis"]["selected_template_version"])
        self.assertEqual(set(json.loads(template["scales_json"])), set(data["features"]))

    def test_reanalysis_is_append_only_and_updates_only_latest_pointer(self):
        dynagraph = self.ingest_card()
        first = self.service.run(dynagraph["id"], self.actor_id)
        second = self.service.run(dynagraph["id"], self.actor_id)
        self.assertNotEqual(first["model_run"]["id"], second["model_run"]["id"])
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM model_runs WHERE input_object_id=?", (dynagraph["id"],)), 2)
        self.assertEqual(self.database.scalar("SELECT latest_model_run_id FROM dynagraphs WHERE id=?", (dynagraph["id"],)), second["model_run"]["id"])
        self.assertEqual(self.database.scalar("SELECT payload FROM raw_messages WHERE id=?", (dynagraph["raw_message_id"],)), dynagraph["sample_data"])

    def test_analysis_audit_failure_rolls_back_every_output(self):
        dynagraph = self.ingest_card()
        before = {
            table: self.database.scalar(f"SELECT COUNT(*) FROM {table}")
            for table in ("model_runs", "diagnoses", "production_estimates", "dynagraph_features")
        }
        self.service.audit.record = Mock(side_effect=RuntimeError("audit unavailable"))
        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            self.service.run(dynagraph["id"], self.actor_id)
        self.assertEqual(before, {
            table: self.database.scalar(f"SELECT COUNT(*) FROM {table}")
            for table in before
        })
        self.assertEqual(self.database.scalar("SELECT status FROM dynagraphs WHERE id=?", (dynagraph["id"],)), "unprocessed")

    def test_manual_review_is_versioned_and_never_overwrites_machine_result(self):
        dynagraph = self.ingest_card("supply_shortage")
        analyzed = self.service.run(dynagraph["id"], self.actor_id)
        diagnosis_id = analyzed["diagnosis"]["id"]
        machine_before = self.database.query_one(
            "SELECT diagnosis_type,confidence,review_version FROM diagnoses WHERE id=?", (diagnosis_id,)
        )
        reviewed = self.service.review(
            diagnosis_id, self.actor_id, "pump_leak", "复核曲线后认为阀漏特征更明显", machine_before["review_version"],
        )
        machine_after = self.database.query_one(
            "SELECT diagnosis_type,confidence,review_version FROM diagnoses WHERE id=?", (diagnosis_id,)
        )
        self.assertEqual((machine_after["diagnosis_type"], machine_after["confidence"]), (machine_before["diagnosis_type"], machine_before["confidence"]))
        self.assertEqual(machine_after["review_version"], machine_before["review_version"] + 1)
        self.assertEqual(reviewed["reviewer_condition"], "pump_leak")
        self.assertEqual(len(self.service.detail(dynagraph["id"])["reviews"]), 1)
        with self.assertRaises(AnalysisVersionConflict):
            self.service.review(diagnosis_id, self.actor_id, "normal", "过期结论", machine_before["review_version"])

    def test_list_detail_and_validation_boundaries(self):
        dynagraph = self.ingest_card()
        listing = self.service.list_dynagraphs({
            "well_id": "W-001", "status": "unprocessed", "page": 1, "page_size": 5,
        })
        self.assertGreaterEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["well_id"], "W-001")
        self.assertEqual(self.service.detail(dynagraph["id"])["raw_message_id"], dynagraph["raw_message_id"])
        with self.assertRaises(AnalysisNotFound):
            self.service.detail("DG-NOT-FOUND")
        with self.assertRaises(AnalysisValidationError):
            self.service.list_dynagraphs({"well_id": "bad id", "page": 1, "page_size": 5})
        with self.assertRaises(AnalysisValidationError):
            self.service.model_runs({"page": 1_000_000, "page_size": 100_000_000_000_000})


class AnalysisHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = create_server(
            "127.0.0.1", 0, database_path=Path(self.temporary.name) / "platform.db"
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        self.algorithm = self._login("algorithm_engineer", "Algorithm@2026")
        self.reviewer = self._login("reviewer", "Review@2026")

    def _close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def _request(self, method, path, token=None, body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=4
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
            "POST", "/api/auth/login", body={"username": username, "password": password}
        )
        self.assertEqual(status, 200)
        return payload["data"]["token"]

    def _live_dynagraph(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = self.server.database.query_one(
                "SELECT id FROM dynagraphs WHERE raw_message_id IS NOT NULL ORDER BY captured_at DESC,id DESC LIMIT 1"
            )
            if row:
                return row["id"]
            time.sleep(.02)
        self.fail("live dynagraph was not materialized")

    def test_authenticated_analysis_and_review_workflow(self):
        dynagraph_id = self._live_dynagraph()
        status, listing = self._request("GET", "/api/dynagraphs?page_size=2", self.reviewer)
        self.assertEqual(status, 200)
        self.assertTrue(listing["data"]["items"])
        self.assertEqual(self._request("POST", f"/api/dynagraphs/{dynagraph_id}/analyze", self.reviewer, {})[0], 403)
        self.assertEqual(self._request("POST", f"/api/dynagraphs/{dynagraph_id}/analyze", self.algorithm, {"extra": True})[0], 422)
        status, response = self._request("POST", f"/api/dynagraphs/{dynagraph_id}/analyze", self.algorithm, {})
        self.assertEqual(status, 200)
        data = response["data"]
        status, detail = self._request("GET", f"/api/dynagraphs/{dynagraph_id}", self.reviewer)
        self.assertEqual(status, 200)
        self.assertEqual(detail["data"]["latest_diagnosis"]["id"], data["diagnosis"]["id"])
        self.assertEqual(self._request("GET", f"/api/dynagraphs/{dynagraph_id}?extra=1", self.reviewer)[0], 422)
        status, runs = self._request("GET", "/api/model-runs?page_size=5", self.reviewer)
        self.assertEqual(status, 200)
        self.assertTrue(any(item["id"] == data["model_run"]["id"] for item in runs["data"]["items"]))
        review_body = {"condition": "normal", "comment": "人工复核记录，不覆盖机器结论", "version": data["diagnosis"]["review_version"]}
        status, reviewed = self._request("POST", f"/api/diagnoses/{data['diagnosis']['id']}/review", self.algorithm, review_body)
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["data"]["reviewer_condition"], "normal")
        self.assertEqual(self.server.database.scalar("SELECT diagnosis_type FROM diagnoses WHERE id=?", (data["diagnosis"]["id"],)), data["diagnosis"]["condition"])


if __name__ == "__main__":
    unittest.main()
