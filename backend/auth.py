"""Local PBKDF2 authentication and permission enforcement."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets

from backend.database import Database


SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
SESSION_LIFETIME = timedelta(hours=8)
PBKDF2_ITERATIONS = 300_000


class AuthError(RuntimeError):
    """A request did not provide a usable authenticated session."""


class InvalidCredentials(AuthError):
    """A username/password pair cannot establish a session."""


class PermissionDenied(AuthError):
    """An authenticated user lacks a required permission."""


class SessionExpired(AuthError):
    """A previously-issued session is expired or revoked."""


@dataclass(frozen=True)
class Session:
    id: int
    token: str
    user_id: int
    expires_at: str


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role_id: int
    role_code: str
    permissions: frozenset[str]
    version: int


def password_material(password: str, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> tuple[str, str, int]:
    """Return hex-encoded PBKDF2-HMAC-SHA256 material for a local password."""
    if not isinstance(password, str) or not password:
        raise ValueError("password is required")
    salt = secrets.token_bytes(16) if salt is None else salt
    if len(salt) < 16:
        raise ValueError("password salt must contain at least 16 bytes")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return salt.hex(), digest.hex(), iterations


class AuthService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _token_hash(token: str) -> str:
        try:
            encoded = token.encode("ascii")
        except UnicodeEncodeError:
            raise AuthError("authentication required") from None
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(SHANGHAI_TIMEZONE)

    def login(self, username: str, password: str) -> Session:
        if not isinstance(username, str) or not isinstance(password, str):
            raise InvalidCredentials("invalid username or password")
        row = self.database.query_one(
            """
            SELECT u.id, u.password_salt, u.password_hash, u.password_iterations
            FROM users AS u JOIN roles AS r ON r.id = u.role_id
            WHERE u.username = ? AND u.status = 'active' AND r.status = 'active'
            """,
            (username.strip(),),
        )
        if row is None or not row["password_salt"] or not row["password_hash"]:
            raise InvalidCredentials("invalid username or password")
        calculated = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(row["password_salt"]), row["password_iterations"]
        ).hex()
        if not hmac.compare_digest(calculated, row["password_hash"]):
            raise InvalidCredentials("invalid username or password")
        token = secrets.token_bytes(32).hex()
        now = self._now()
        expires_at = (now + SESSION_LIFETIME).isoformat()
        with self.database.transaction(
            int(row["id"]), "auth.login", "user", str(row["id"]),
            details_json='{"after":{"session":"issued"},"before":{}}',
        ) as connection:
            connection.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
            cursor = connection.execute(
                "INSERT INTO sessions(user_id, token_hash, expires_at) VALUES(?, ?, ?)",
                (row["id"], self._token_hash(token), expires_at),
            )
            session_id = int(cursor.lastrowid)
        return Session(session_id, token, int(row["id"]), expires_at)

    def _session_user(self, token: str) -> User:
        if not isinstance(token, str) or not token:
            raise AuthError("authentication required")
        row = self.database.query_one(
            """
            SELECT s.id AS session_id, s.expires_at, s.revoked_at,
                   u.id, u.username, u.display_name, u.role_id, u.status AS user_status, u.version,
                   r.code AS role_code, r.status AS role_status
            FROM sessions AS s
            JOIN users AS u ON u.id = s.user_id
            JOIN roles AS r ON r.id = u.role_id
            WHERE s.token_hash = ?
            """,
            (self._token_hash(token),),
        )
        if row is None:
            raise AuthError("authentication required")
        if row["revoked_at"] is not None or datetime.fromisoformat(row["expires_at"]) <= self._now():
            raise SessionExpired("session expired")
        if row["user_status"] != "active" or row["role_status"] != "active":
            raise AuthError("authentication required")
        permissions = frozenset(self.database.column(
            """SELECT p.code FROM permissions AS p
               JOIN role_permissions AS rp ON rp.permission_id = p.id
               WHERE rp.role_id = ? ORDER BY p.code""",
            (row["role_id"],),
        ))
        return User(
            int(row["id"]), row["username"], row["display_name"], int(row["role_id"]),
            row["role_code"], permissions, int(row["version"]),
        )

    def require(self, token: str, permission: str) -> User:
        user = self._session_user(token)
        if permission not in user.permissions:
            raise PermissionDenied(f"missing permission: {permission}")
        return user

    def me(self, token: str) -> User:
        return self._session_user(token)

    def logout(self, token: str) -> None:
        user = self._session_user(token)
        with self.database.transaction(
            user.id, "auth.logout", "session", None,
            details_json='{"after":{"session":"revoked"},"before":{"session":"active"}}',
        ) as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (self._now().isoformat(), self._token_hash(token)),
            )
