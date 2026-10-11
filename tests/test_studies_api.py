"""Real loopback HTTP checks without launching the production server."""
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from backend.auth import AuthService
from backend.database import Database
from backend.studies.api import StudyApi
from backend.studies.service import StudyService
from backend.studies.storage import migrate_studies


class StudyHttpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db = Database(Path(temporary.name) / "study.sqlite")
        self.db = db
        db.migrate()
        migrate_studies(db)
        token = uuid4().hex
        expires = (datetime.now(timezone(timedelta(hours=8))) + timedelta(hours=2)).isoformat()
        with db.connection() as conn:
            conn.execute("INSERT INTO roles(code,name) VALUES('study','Study')")
            role = conn.execute("SELECT id FROM roles WHERE code='study'").fetchone()[0]
            conn.execute("INSERT INTO users(username,display_name,role_id,status) VALUES('user','User',?,'active')", (role,))
            user = conn.execute("SELECT id FROM users WHERE username='user'").fetchone()[0]
            for code in ("studies.read", "studies.create", "studies.review", "studies.select", "studies.export"):
                conn.execute("INSERT INTO permissions(code,name) VALUES(?,?)", (code, code))
                conn.execute("INSERT INTO role_permissions(role_id,permission_id) SELECT ?,id FROM permissions WHERE code=?", (role, code))
            conn.execute("INSERT INTO sessions(user_id,token_hash,expires_at) VALUES(?,?,?)", (user, hashlib.sha256(token.encode()).hexdigest(), expires))
        self.token = token
        service = StudyService(db, AuthService(db), {"motor_spectrum": ("v1", lambda payload: {"status": "completed"})})

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self): StudyApi(self).dispatch()
            def do_POST(self): StudyApi(self).dispatch()
            def do_DELETE(self): StudyApi(self).dispatch()
            def log_message(self, *args): pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.studies = service
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        head = {"Cookie": f"platform_session={self.token}", **(headers or {})}
        conn.request(method, path, body=body, headers=head)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw)

    def test_http_create_get_replay_and_reject_duplicate_keys(self):
        body = {"request_id": str(uuid4()), "kind": "motor_spectrum", "title": "test",
                "source": {"kind": "software_fixture", "description": "fixture", "references": []}, "payload": {"signal": [1]}}
        encoded = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        status, created = self.request("POST", "/api/production-studies", encoded, headers)
        self.assertEqual(status, 201)
        record_id = created["data"]["id"]
        self.assertEqual(self.request("POST", "/api/production-studies", encoded, headers)[1]["data"]["id"], record_id)
        status, fetched = self.request("GET", f"/api/production-studies/{record_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["data"]["payload"], body["payload"])
        status, receipt = self.request("GET", f"/api/production-studies/requests/{body['request_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(receipt["data"]["id"], record_id)
        duplicate = encoded.decode().replace('"title": "test"', '"title": "test", "title": "other"').encode()
        self.assertEqual(self.request("POST", "/api/production-studies", duplicate, headers)[0], 400)

    def test_http_rejects_cross_origin_and_missing_cookie(self):
        self.assertEqual(self.request("GET", "/api/production-studies", headers={"Cookie": ""})[0], 401)
        self.assertEqual(self.request("POST", "/api/production-studies", headers={"Content-Type": "application/json", "Origin": "https://attacker.invalid"})[0], 403)

    def test_http_rejects_unknown_method_nonfinite_and_oversized_body(self):
        headers = {"Content-Type": "application/json"}
        self.assertEqual(self.request("DELETE", "/api/production-studies", headers=headers)[0], 405)
        self.assertEqual(self.request("POST", "/api/production-studies", b'{"value":NaN}', headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/production-studies", b"{}", {**headers, "Content-Length": str(8 * 1024 * 1024 + 1)})[0], 413)

    def test_storage_failure_returns_bounded_error_and_rolls_back(self):
        with self.db.connection() as conn:
            conn.execute("""CREATE TRIGGER block_study_audit BEFORE INSERT ON audit_logs
                WHEN NEW.action='studies.create' BEGIN SELECT RAISE(FAIL,'private storage detail'); END""")
        body = {"request_id": str(uuid4()), "kind": "motor_spectrum", "title": "test",
                "source": {"kind": "software_fixture", "description": "fixture", "references": []}, "payload": {"signal": [1]}}
        status, response = self.request("POST", "/api/production-studies", json.dumps(body).encode(), {"Content-Type": "application/json"})
        self.assertEqual(status, 503)
        self.assertNotIn("private storage detail", json.dumps(response))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM study_records"), 0)


if __name__ == "__main__":
    unittest.main()
