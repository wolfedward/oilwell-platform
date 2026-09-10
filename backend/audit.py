"""Audit records with canonical, secret-free detail payloads."""

import json
import sqlite3
from typing import Any

from backend.database import Database


_SECRET_MARKERS = ("password", "salt", "hash", "token", "secret", "credential")
_SECRET_KEY_NAMES = {
    "api_key", "private_key", "secret_key", "client_secret", "access_token", "refresh_token",
}


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized == "public_key":
        return False
    return normalized in _SECRET_KEY_NAMES or any(marker in normalized for marker in _SECRET_MARKERS)


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _safe(item)
            for key, item in value.items()
            if not _is_secret_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


class AuditService:
    """Write immutable audit details, optionally into a caller-owned transaction."""

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def canonical_details(details: dict) -> str:
        if not isinstance(details, dict):
            raise ValueError("audit details must be an object")
        cleaned = _safe(details)
        cleaned.setdefault("before", {})
        cleaned.setdefault("after", {})
        return json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def record(
        self, actor_id: int | None, action: str, object_type: str, object_id: str | None,
        details: dict, connection: sqlite3.Connection | None = None,
        *, source_type: str = "manual",
    ) -> int:
        """Store an audit record and return its id.

        Supplying ``connection`` lets a business mutation and its audit record share
        one transaction.  Callers that own that connection must use
        ``Database.transaction(..., auto_audit=False)`` to avoid a second summary row.
        """
        payload = self.canonical_details(details)
        if connection is not None:
            cursor = connection.execute(
                """
                INSERT INTO audit_logs(actor_id, action, object_type, object_id, details_json, source_type)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (actor_id, action, object_type, object_id, payload, source_type),
            )
            return int(cursor.lastrowid)
        with self.database.transaction(
            actor_id, action, object_type, object_id, source_type=source_type, auto_audit=False,
        ) as transaction:
            cursor = transaction.execute(
                """
                INSERT INTO audit_logs(actor_id, action, object_type, object_id, details_json, source_type)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (actor_id, action, object_type, object_id, payload, source_type),
            )
            return int(cursor.lastrowid)
