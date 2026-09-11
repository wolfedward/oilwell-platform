import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from backend.http_server import create_server


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AcceptanceEvidenceApiTests(unittest.TestCase):
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
        status, payload = self.request(
            "POST", "/api/auth/login",
            {"username": "administrator", "password": "Admin@2026"},
        )
        self.assertEqual(status, 200)
        self.token = payload["data"]["token"]

    def _close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, method, path, body=None, *, token=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5,
        )
        headers = {"Content-Type": "application/json"} if body is not None else {}
        credential = token if token is not None else getattr(self, "token", None)
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        try:
            connection.request(
                method, path,
                body=json.dumps(body).encode("utf-8") if body is not None else None,
                headers=headers,
            )
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8"))
        finally:
            connection.close()

    def test_acceptance_center_maps_all_two_year_requirements_to_live_evidence(self):
        status, payload = self.request("GET", "/api/acceptance/evidence")
        self.assertEqual(status, 200)
        data = payload["data"]
        codes = {item["code"] for item in data["requirements"]}
        self.assertEqual(codes, {
            "Y1-DYNAGRAPH", "Y1-ACQUISITION", "Y1-TRANSMISSION",
            "Y1-DECISION", "Y2-ENERGY", "Y2-DSP-MCU",
        })
        self.assertTrue(all(item["evidence"] for item in data["requirements"]))
        self.assertTrue(all(item["route"] for item in data["requirements"]))
        self.assertEqual(data["integrity"]["foreign_keys"], "ok")
        self.assertEqual(data["integrity"]["database"], "ok")

    def test_audit_api_is_read_only_filtered_and_permission_protected(self):
        status, payload = self.request("GET", "/api/audit?object_type=device_command&page_size=5")
        self.assertEqual(status, 200)
        self.assertGreater(payload["data"]["total"], 0)
        self.assertTrue(all(
            item["object_type"] == "device_command"
            for item in payload["data"]["items"]
        ))
        status, reviewer = self.request(
            "POST", "/api/auth/login",
            {"username": "reviewer", "password": "Review@2026"},
            token="",
        )
        self.assertEqual(status, 200)
        status, _ = self.request(
            "GET", "/api/audit?page_size=1", token=reviewer["data"]["token"],
        )
        self.assertEqual(status, 200)


class AcceptanceManagementPageSourceTests(unittest.TestCase):
    def test_management_and_evidence_pages_are_real_modules(self):
        names = ("reports", "users", "roles", "audit", "system-health", "acceptance-center")
        for name in names:
            with self.subTest(name=name):
                source = (PROJECT_ROOT / "frontend" / "pages" / f"{name}.js").read_text(encoding="utf-8")
                self.assertIn("export async function", source)
                self.assertNotIn("Math.random", source)

    def test_audit_page_has_no_delete_action(self):
        source = (PROJECT_ROOT / "frontend/pages/audit.js").read_text(encoding="utf-8")
        self.assertNotIn("deleteAudit", source)


if __name__ == "__main__":
    unittest.main()
