"""SQLite-backed store-and-forward queue for intermittent field networks."""

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
import time


@dataclass(frozen=True)
class SpoolMessage:
    message_id: str
    topic: str
    payload: bytes
    qos: int
    attempts: int


class DurableSpool:
    """Persist messages until a QoS acknowledgement is confirmed by the client."""

    def __init__(
        self,
        path,
        *,
        max_attempts: int = 12,
        base_backoff_seconds: float = 2.0,
        max_backoff_seconds: float = 300.0,
    ):
        if max_attempts < 1 or base_backoff_seconds <= 0 or max_backoff_seconds < base_backoff_seconds:
            raise ValueError("invalid retry policy")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.base_backoff_seconds = float(base_backoff_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS uplink_spool (
                message_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                payload BLOB NOT NULL,
                qos INTEGER NOT NULL CHECK(qos IN (1, 2)),
                state TEXT NOT NULL CHECK(state IN ('pending','inflight','acked','dead')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                claimed_at REAL,
                created_at REAL NOT NULL,
                acked_at REAL,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS uplink_spool_ready_idx
                ON uplink_spool(state, next_attempt_at, created_at);
            """
        )
        self._connection.commit()

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def enqueue(self, message_id: str, topic: str, payload: bytes, *, qos: int = 1, now: float | None = None) -> bool:
        timestamp = time.time() if now is None else float(now)
        if not message_id or not topic or qos not in (1, 2) or not isinstance(payload, (bytes, bytearray, memoryview)):
            raise ValueError("invalid spool message")
        with self._lock:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO uplink_spool
                   (message_id,topic,payload,qos,state,next_attempt_at,created_at)
                   VALUES (?,?,?,?,'pending',?,?)""",
                (message_id, topic, bytes(payload), qos, timestamp, timestamp),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    def claim(
        self,
        *,
        now: float | None = None,
        limit: int = 50,
        lease_seconds: float = 60.0,
    ) -> list[SpoolMessage]:
        timestamp = time.time() if now is None else float(now)
        if not 1 <= limit <= 1000 or lease_seconds <= 0:
            raise ValueError("invalid claim parameters")
        expired_before = timestamp - float(lease_seconds)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    """SELECT message_id FROM uplink_spool
                       WHERE (state='pending' AND next_attempt_at<=?)
                          OR (state='inflight' AND claimed_at<=?)
                       ORDER BY created_at,message_id LIMIT ?""",
                    (timestamp, expired_before, limit),
                ).fetchall()
                identifiers = [row["message_id"] for row in rows]
                if identifiers:
                    placeholders = ",".join("?" for _ in identifiers)
                    self._connection.execute(
                        f"""UPDATE uplink_spool SET state='inflight', claimed_at=?, attempts=attempts+1
                            WHERE message_id IN ({placeholders})""",
                        (timestamp, *identifiers),
                    )
                    claimed = self._connection.execute(
                        f"""SELECT message_id,topic,payload,qos,attempts FROM uplink_spool
                            WHERE message_id IN ({placeholders}) ORDER BY created_at,message_id""",
                        identifiers,
                    ).fetchall()
                else:
                    claimed = []
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return [
            SpoolMessage(row["message_id"], row["topic"], bytes(row["payload"]), row["qos"], row["attempts"])
            for row in claimed
        ]

    def ack(self, message_id: str, *, now: float | None = None) -> bool:
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE uplink_spool SET state='acked',acked_at=?,claimed_at=NULL,last_error=NULL
                   WHERE message_id=? AND state='inflight'""",
                (timestamp, message_id),
            )
            self._connection.commit()
            return cursor.rowcount == 1

    def fail(self, message_id: str, error: str, *, now: float | None = None) -> bool:
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            row = self._connection.execute(
                "SELECT attempts FROM uplink_spool WHERE message_id=? AND state='inflight'",
                (message_id,),
            ).fetchone()
            if row is None:
                return False
            attempts = row["attempts"]
            state = "dead" if attempts >= self.max_attempts else "pending"
            delay = min(self.max_backoff_seconds, self.base_backoff_seconds * (2 ** max(0, attempts - 1)))
            self._connection.execute(
                """UPDATE uplink_spool SET state=?,next_attempt_at=?,claimed_at=NULL,last_error=?
                   WHERE message_id=? AND state='inflight'""",
                (state, timestamp + delay, str(error)[:1000], message_id),
            )
            self._connection.commit()
            return True

    def counts(self) -> dict[str, int]:
        result = {state: 0 for state in ("pending", "inflight", "acked", "dead")}
        with self._lock:
            rows = self._connection.execute(
                "SELECT state,COUNT(*) AS count FROM uplink_spool GROUP BY state"
            ).fetchall()
        for row in rows:
            result[row["state"]] = row["count"]
        return result
