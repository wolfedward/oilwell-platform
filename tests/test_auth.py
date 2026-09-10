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
from backend.seed import _legacy_content_hash, seed_database


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

    def test_seed_hash_upgrades_once_and_allows_mutable_auth_configuration(self):
        """Hashing role/user configuration makes a valid administrative edit brick the next startup."""
        legacy_hash = _legacy_content_hash(self.database, 202606, "")
        with self.database.transaction(None, "test.legacy_hash", "seed_manifest", "acceptance-history"):
            self.database.execute(
                "UPDATE seed_manifests SET content_hash = ? WHERE dataset_name = 'acceptance-history'",
                (legacy_hash,),
            )
        seed_database(self.database)
        manifest_before = self.database.query_one(
            "SELECT content_hash FROM seed_manifests WHERE dataset_name = 'acceptance-history'"
        )["content_hash"]
        reviewer = self.database.query_one("SELECT id, version FROM users WHERE username = 'reviewer'")
        with self.database.transaction(1, "test.mutable_auth", "user", str(reviewer["id"])):
            self.database.execute("UPDATE roles SET name = '现场评审', status = 'disabled' WHERE code = 'reviewer'")
            self.database.execute(
                "UPDATE users SET display_name = '现场评审员', status = 'disabled', version = version + 1 WHERE id = ? AND version = ?",
                (reviewer["id"], reviewer["version"]),
            )

        seed_database(self.database)

        upgraded = self.database.query_one(
            "SELECT content_hash FROM seed_manifests WHERE dataset_name = 'acceptance-history'"
        )["content_hash"]
        self.assertEqual(upgraded, manifest_before)
        self.assertEqual(self.database.scalar("SELECT name FROM roles WHERE code = 'reviewer'"), "现场评审")
        self.assertEqual(self.database.scalar("SELECT status FROM users WHERE username = 'reviewer'"), "disabled")
        audit_count = self.database.scalar("SELECT COUNT(*) FROM audit_logs WHERE action = 'seed.upgrade_manifest_hash'")
        self.assertEqual(audit_count, 1)
        seed_database(self.database)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM audit_logs WHERE action = 'seed.upgrade_manifest_hash'"), audit_count)
        with self.database.transaction(1, "test.damage_seed", "raw_message", None):
            self.database.execute("DELETE FROM raw_messages WHERE id = (SELECT MIN(id) FROM raw_messages)")
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            seed_database(self.database)

    def test_bootstrap_does_not_restore_admin_revoked_grant_on_restart(self):
        """Restarting must not silently undo an administrator's deliberate permission revocation."""
        reviewer_role = self.database.query_one("SELECT id FROM roles WHERE code = 'reviewer'")["id"]
        with self.database.transaction(1, "test.revoke", "role", str(reviewer_role)):
            self.database.execute(
                """DELETE FROM role_permissions WHERE role_id = ? AND permission_id =
                   (SELECT id FROM permissions WHERE code = 'audit.read')""",
                (reviewer_role,),
            )

        seed_database(self.database)

        self.assertEqual(
            self.database.scalar(
                """SELECT COUNT(*) FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id
                   WHERE rp.role_id = ? AND p.code = 'audit.read'""",
                (reviewer_role,),
            ),
            0,
        )
        self.assertEqual(self.database.scalar("SELECT bootstrap_version FROM auth_bootstrap_state WHERE id = 1"), 1)

    def test_audit_recursively_removes_common_secret_keys_but_keeps_public_key(self):
        """Redacting only password/token leaves API credentials in the persistent audit trail."""
        secret_values = ("api-value", "private-value", "secret-value", "client-value", "access-value", "refresh-value")
        audit_id = self.audit.record(
            1, "test.redaction", "secret", "1",
            {"before": {"api_key": "api-value", "nested": {"private_key": "private-value", "public_key": "public-value"}},
             "after": {"secret_key": "secret-value", "client_secret": "client-value", "access_token": "access-value", "refresh_token": "refresh-value"}},
        )
        details = self.database.query_one("SELECT details_json FROM audit_logs WHERE id = ?", (audit_id,))["details_json"]

        for value in secret_values:
            self.assertNotIn(value, details)
        self.assertIn("public-value", details)

    def test_all_demonstration_accounts_can_log_in(self):
        for username, password in (
            ("administrator", "Admin@2026"), ("production_engineer", "Production@2026"),
            ("algorithm_engineer", "Algorithm@2026"), ("maintenance_engineer", "Maintenance@2026"),
            ("reviewer", "Review@2026"),
        ):
            with self.subTest(username=username):
                self.assertEqual(self.auth.login(username, password).user_id, self.database.scalar(
                    "SELECT id FROM users WHERE username = ?", (username,)
                ))


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
        return self.request_to(self.server.server_port, method, path, payload, headers)

    def request_to(self, port, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
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

    @staticmethod
    def bearer(token):
        return {"Authorization": f"Bearer {token}"}

    def user_by_username(self, token, username):
        response, payload = self.request("GET", "/api/users", headers=self.bearer(token))
        self.assertEqual(response.status, 200)
        return next(user for user in payload["data"] if user["username"] == username)

    def create_second_administrator(self, token):
        role_id = self.server.database.query_one("SELECT id FROM roles WHERE code = 'administrator'")["id"]
        response, payload = self.request(
            "POST", "/api/users",
            {"username": "backup_admin", "display_name": "Backup Admin", "role_id": role_id, "password": "Backup@2026"},
            self.bearer(token),
        )
        self.assertEqual(response.status, 201)
        return payload["data"]

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

    def test_single_administrator_cannot_change_own_role(self):
        """Checking only disable status lets the final administrator self-demote out of recovery access."""
        _, login = self.login()
        token = login["data"]["token"]
        administrator = self.user_by_username(token, "administrator")
        reviewer_role = self.server.database.query_one("SELECT id FROM roles WHERE code = 'reviewer'")["id"]

        response, payload = self.request(
            "PATCH", f"/api/users/{administrator['id']}",
            {"version": administrator["version"], "role_id": reviewer_role}, self.bearer(token),
        )

        self.assertEqual((response.status, payload["error"]["code"]), (422, "validation_error"))

    def test_administrator_role_cannot_be_disabled_even_when_two_admins_exist(self):
        """A role-level disable strands all administrators despite there being multiple accounts."""
        _, login = self.login()
        token = login["data"]["token"]
        self.create_second_administrator(token)
        role = self.server.database.query_one("SELECT id, version FROM roles WHERE code = 'administrator'")

        response, payload = self.request(
            "PATCH", f"/api/roles/{role['id']}", {"version": role["version"], "status": "disabled"}, self.bearer(token)
        )

        self.assertEqual((response.status, payload["error"]["code"]), (422, "validation_error"))

    def test_two_administrators_allow_first_demote_but_reject_last(self):
        """The post-update invariant must allow a safe first demotion but stop the final one."""
        _, login = self.login()
        token = login["data"]["token"]
        backup = self.create_second_administrator(token)
        administrator = self.user_by_username(token, "administrator")
        reviewer_role = self.server.database.query_one("SELECT id FROM roles WHERE code = 'reviewer'")["id"]
        response, payload = self.request(
            "PATCH", f"/api/users/{administrator['id']}",
            {"version": administrator["version"], "role_id": reviewer_role}, self.bearer(token),
        )
        self.assertEqual(response.status, 200)
        _, backup_login = self.login("backup_admin", "Backup@2026")
        response, payload = self.request(
            "PATCH", f"/api/users/{backup['id']}",
            {"version": backup["version"], "role_id": reviewer_role}, self.bearer(backup_login["data"]["token"]),
        )
        self.assertEqual((response.status, payload["error"]["code"]), (422, "validation_error"))

    def test_malformed_resource_ids_and_non_ascii_bearers_are_json_unauthenticated(self):
        """Invalid route IDs and bearer bytes must be validation/auth errors, not dropped connections."""
        _, login = self.login()
        token = login["data"]["token"]
        for path in ("/api/users/not-an-id", "/api/roles/not-an-id"):
            response, payload = self.request("GET", path, headers=self.bearer(token))
            self.assertEqual((response.status, payload["error"]["code"]), (400, "validation_error"))
        response, payload = self.request("GET", "/api/auth/me", headers={"Authorization": "Bearer \xff"})
        self.assertEqual((response.status, payload["error"]["code"]), (401, "unauthenticated"))

    def test_mutable_http_edits_survive_real_server_restart(self):
        """A role/user display edit is valid configuration and must not prevent the next service start."""
        _, login = self.login()
        token = login["data"]["token"]
        role = self.server.database.query_one("SELECT id, version FROM roles WHERE code = 'reviewer'")
        response, role_payload = self.request("PATCH", f"/api/roles/{role['id']}", {"version": role["version"], "name": "现场评审"}, self.bearer(token))
        self.assertEqual(response.status, 200)
        reviewer = self.user_by_username(token, "reviewer")
        response, _ = self.request("PATCH", f"/api/users/{reviewer['id']}", {"version": reviewer["version"], "display_name": "现场评审员"}, self.bearer(token))
        self.assertEqual(response.status, 200)
        response, _ = self.request(
            "PATCH", f"/api/roles/{role['id']}",
            {"version": role_payload["data"]["version"], "permissions": ["wells.read", "reports.read"]}, self.bearer(token),
        )
        self.assertEqual(response.status, 200)

        self.server.shutdown(); self.thread.join(); self.server.server_close()
        replacement = create_server("127.0.0.1", 0, database_path=self.server.database.path)
        replacement_thread = threading.Thread(target=replacement.serve_forever, daemon=True)
        replacement_thread.start()
        self.addCleanup(replacement.server_close)
        self.addCleanup(replacement_thread.join)
        self.addCleanup(replacement.shutdown)
        response, payload = self.request_to(replacement.server_port, "GET", "/api/health")
        self.assertEqual((response.status, payload["data"]["database"]), (200, "ok"))
        reviewer_token = replacement.auth.login("reviewer", "Review@2026").token
        with self.assertRaises(PermissionDenied):
            replacement.auth.require(reviewer_token, "audit.read")


if __name__ == "__main__":
    unittest.main()
