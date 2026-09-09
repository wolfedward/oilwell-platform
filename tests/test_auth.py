import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from backend.audit import AuditService
from backend.auth import (
    AuthError,
    AuthService,
    InvalidCredentials,
    PermissionDenied,
    SessionExpired,
)
from backend.database import Database
from backend.http_server import create_server
from backend.seed import seed_database


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "platform.db")
        self.database.migrate()
        seed_database(self.database)
        self.auth = AuthService(self.database)
        self.audit = AuditService(self.database)

    def test_reviewer_can_log_in_but_cannot_approve_strategy(self):
        """Removing the role-permission check would allow a read-only reviewer to approve."""
        token = self.auth.login("reviewer", "Review@2026").token

        with self.assertRaises(PermissionDenied):
            self.auth.require(token, "strategy.approve")

    def test_login_persists_only_a_hash_of_a_random_token(self):
        """Storing the client token itself would expose a reusable session credential."""
        session = self.auth.login("reviewer", "Review@2026")
        row = self.database.query_one("SELECT token_hash FROM sessions WHERE id = ?", (session.id,))

        self.assertEqual(len(session.token), 64)
        self.assertNotEqual(row["token_hash"], session.token)
        self.assertEqual(len(row["token_hash"]), 64)

    def test_invalid_password_and_disabled_user_are_rejected(self):
        """Dropping credential or account-status verification would create an unauthorized session."""
        with self.assertRaises(InvalidCredentials):
            self.auth.login("reviewer", "incorrect")
        reviewer = self.database.query_one("SELECT id, version FROM users WHERE username = 'reviewer'")
        with self.database.transaction(1, "user.disable", "user", str(reviewer["id"])):
            self.database.execute(
                "UPDATE users SET status = 'disabled', version = version + 1 WHERE id = ? AND version = ?",
                (reviewer["id"], reviewer["version"]),
            )
        with self.assertRaises(InvalidCredentials):
            self.auth.login("reviewer", "Review@2026")

    def test_logout_and_expired_session_cannot_be_required(self):
        """Ignoring revoked_at or expires_at would leave expired credentials usable."""
        session = self.auth.login("reviewer", "Review@2026")
        self.auth.logout(session.token)
        with self.assertRaises(SessionExpired):
            self.auth.require(session.token, "reports.read")

    def test_mutation_audit_contains_canonical_before_and_after(self):
        """Not serializing audit detail would make state changes impossible to trace."""
        audit_id = self.audit.record(
            1, "update", "well", "W-001",
            {"after": {"status": "stopped"}, "before": {"status": "running"}},
        )
        row = self.database.query_one("SELECT details_json FROM audit_logs WHERE id = ?", (audit_id,))

        self.assertEqual(
            row["details_json"],
            '{"after":{"status":"stopped"},"before":{"status":"running"}}',
        )

    def test_audit_and_business_write_roll_back_together(self):
        """Committing audit separately would preserve an audit event after a failed business write."""
        before = self.database.scalar("SELECT COUNT(*) FROM audit_logs")
        with self.assertRaises(RuntimeError):
            with self.database.transaction(1, "test.change", "role", "1", auto_audit=False) as connection:
                connection.execute("UPDATE roles SET name = 'temporary' WHERE code = 'reviewer'")
                self.audit.record(1, "test.change", "role", "1", {"before": {}, "after": {}}, connection)
                raise RuntimeError("rollback")

        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM audit_logs"), before)
        self.assertEqual(self.database.scalar("SELECT name FROM roles WHERE code = 'reviewer'"), "只读评审")

    def test_password_hash_uses_distinct_salt_and_safe_iterations(self):
        """A fixed salt or weak PBKDF2 cost makes a stolen credential database easier to crack."""
        row = self.database.query_one(
            "SELECT password_salt, password_hash, password_iterations FROM users WHERE username = 'reviewer'"
        )

        self.assertGreaterEqual(len(row["password_salt"]), 32)
        self.assertEqual(len(row["password_hash"]), 64)
        self.assertGreaterEqual(row["password_iterations"], 200_000)


class AuthenticationApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        path = Path(self.temporary_directory.name) / "platform.db"
        self.server = create_server("127.0.0.1", 0, database_path=path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            body = None if payload is None else json.dumps(payload).encode("utf-8")
            request_headers = dict(headers or {})
            if body is not None:
                request_headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def login(self, username="administrator", password="Admin@2026"):
        response, payload = self.request("POST", "/api/auth/login", {"username": username, "password": password})
        self.assertEqual(response.status, 200)
        return response, payload

    def test_login_sets_secure_cookie_and_me_accepts_bearer(self):
        """Omitting cookie flags or token extraction would break protected browser and local requests."""
        response, payload = self.login()
        cookie = response.getheader("Set-Cookie")
        self.assertIn("platform_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Max-Age=28800", cookie)

        response, me = self.request(
            "GET", "/api/auth/me", headers={"Authorization": f"Bearer {payload['data']['token']}"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(me["data"]["username"], "administrator")
        self.assertNotIn("password_hash", me["data"])

    def test_invalid_login_unauthenticated_and_forbidden_are_json_errors(self):
        """Wrong status mapping would blur bad credentials, missing sessions, and missing permissions."""
        response, payload = self.request("POST", "/api/auth/login", {"username": "reviewer", "password": "no"})
        self.assertEqual((response.status, payload["error"]["code"]), (401, "invalid_credentials"))
        response, payload = self.request("GET", "/api/users")
        self.assertEqual((response.status, payload["error"]["code"]), (401, "unauthenticated"))
        _, login = self.login("reviewer", "Review@2026")
        response, payload = self.request(
            "POST", "/api/users", {"username": "blocked", "display_name": "Blocked", "role_id": 1, "password": "Blocked@2026"},
            {"Authorization": f"Bearer {login['data']['token']}"},
        )
        self.assertEqual((response.status, payload["error"]["code"]), (403, "forbidden"))

    def test_user_patch_uses_version_and_writes_one_redacted_audit_record(self):
        """Ignoring the version predicate permits stale writes and unaudited password resets leak secrets."""
        _, login = self.login()
        token = login["data"]["token"]
        response, users = self.request("GET", "/api/users", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status, 200)
        reviewer = next(user for user in users["data"] if user["username"] == "reviewer")
        response, payload = self.request(
            "PATCH", f"/api/users/{reviewer['id']}",
            {"version": reviewer["version"], "password": "Changed@2026"},
            {"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["data"]["version"], reviewer["version"] + 1)
        audit = self.server.database.query_one("SELECT details_json FROM audit_logs ORDER BY id DESC LIMIT 1")
        self.assertNotIn("Changed@2026", audit["details_json"])
        self.assertNotIn("password_hash", audit["details_json"])
        response, payload = self.request(
            "PATCH", f"/api/users/{reviewer['id']}", {"version": reviewer["version"], "status": "active"},
            {"Authorization": f"Bearer {token}"},
        )
        self.assertEqual((response.status, payload["error"]["code"]), (409, "version_conflict"))


if __name__ == "__main__":
    unittest.main()
