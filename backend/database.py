"""SQLite persistence boundary for the offline platform."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence


TIMESTAMP_TABLES = (
    "units", "well_groups", "wells", "devices", "device_channels", "device_heartbeats",
    "telemetry", "production_daily", "seed_manifests", "raw_messages", "data_quality_events", "dynagraphs", "dynagraph_features",
    "diagnoses", "model_runs", "recovery_tests", "energy_strategies", "strategy_versions",
    "device_commands", "command_attempts", "alarms", "approvals", "work_orders",
    "work_order_logs", "reports", "users", "roles", "audit_logs", "system_events",
)
SHANGHAI_TIMESTAMP_SQL = "strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')"


class Database:
    """Open short-lived SQLite connections with consistent platform settings."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._transaction_connection: ContextVar[sqlite3.Connection | None] = ContextVar(
            "transaction_connection", default=None
        )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield one configured database connection and finish its transaction."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def migrate(self) -> None:
        """Create the base schema; statements are safe to apply repeatedly."""
        schema = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        with self.connection() as connection:
            connection.executescript(schema)
            self._migrate_seed_columns(connection)
            self._normalize_legacy_timestamps(connection)

    @staticmethod
    def _migrate_seed_columns(connection: sqlite3.Connection) -> None:
        """Add seed-era well metadata to databases created by earlier platform releases."""
        columns = {row[1] for row in connection.execute("PRAGMA table_info(wells)")}
        if "is_low_output" not in columns:
            connection.execute(
                "ALTER TABLE wells ADD COLUMN is_low_output INTEGER NOT NULL DEFAULT 0 "
                "CHECK(is_low_output IN (0, 1))"
            )
        manifest_columns = {row[1] for row in connection.execute("PRAGMA table_info(seed_manifests)")}
        if "content_hash" not in manifest_columns:
            connection.execute("ALTER TABLE seed_manifests ADD COLUMN content_hash TEXT")

    @staticmethod
    def _normalize_legacy_timestamps(connection: sqlite3.Connection) -> None:
        for table_name in TIMESTAMP_TABLES:
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            if not {"created_at", "updated_at"}.issubset(columns):
                continue
            connection.execute(
                f"""
                UPDATE {table_name}
                SET created_at = CASE
                        WHEN created_at GLOB '*+08:00' THEN created_at
                        ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', created_at, '+8 hours')
                    END,
                    updated_at = CASE
                        WHEN updated_at GLOB '*+08:00' THEN updated_at
                        ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', updated_at, '+8 hours')
                    END
                WHERE created_at NOT GLOB '*+08:00' OR updated_at NOT GLOB '*+08:00'
                """
            )

    def table_names(self) -> set[str]:
        return set(
            self.column("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        )

    def query_one(self, statement: str, parameters: Sequence = ()) -> sqlite3.Row | None:
        active_connection = self._transaction_connection.get()
        if active_connection is not None:
            return active_connection.execute(statement, parameters).fetchone()
        with self._guarded_connection() as connection:
            return connection.execute(statement, parameters).fetchone()

    def query_all(self, statement: str, parameters: Sequence = ()) -> list[sqlite3.Row]:
        active_connection = self._transaction_connection.get()
        if active_connection is not None:
            return active_connection.execute(statement, parameters).fetchall()
        with self._guarded_connection() as connection:
            return connection.execute(statement, parameters).fetchall()

    def scalar(self, statement: str, parameters: Sequence = ()):
        row = self.query_one(statement, parameters)
        return None if row is None else row[0]

    def column(self, statement: str, parameters: Sequence = ()) -> list:
        return [row[0] for row in self.query_all(statement, parameters)]

    def execute(self, statement: str, parameters: Sequence = ()) -> int:
        active_connection = self._transaction_connection.get()
        if active_connection is not None:
            return active_connection.execute(statement, parameters).rowcount
        with self._guarded_connection() as connection:
            return connection.execute(statement, parameters).rowcount

    def executemany(self, statement: str, parameter_sets: Sequence[Sequence]) -> int:
        active_connection = self._transaction_connection.get()
        if active_connection is not None:
            return active_connection.executemany(statement, parameter_sets).rowcount
        with self._guarded_connection() as connection:
            return connection.executemany(statement, parameter_sets).rowcount

    @contextmanager
    def _guarded_connection(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.set_authorizer(self._deny_mutations)
            try:
                yield connection
            except sqlite3.DatabaseError as error:
                if "not authorized" in str(error).lower():
                    raise RuntimeError("DML requires Database.transaction") from None
                raise
            finally:
                connection.set_authorizer(None)

    @staticmethod
    def _deny_mutations(
        action_code: int, table_name: str | None, _column_name: str | None,
        _database_name: str | None, _source: str | None,
    ) -> int:
        if table_name != "sqlite_master" and action_code in {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
        }:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def transaction(
        self, actor_id: int | None, action: str, object_type: str, object_id: str | None,
        *, source_type: str = "system",
    ) -> Iterator[sqlite3.Connection]:
        """Commit business changes and their audit entry as one transaction."""
        if source_type not in {"system", "manual", "simulator", "device", "algorithm"}:
            raise ValueError("unsupported audit source_type")
        with self.connection() as connection:
            token = self._transaction_connection.set(connection)
            try:
                yield connection
                connection.execute(
                    """
                    INSERT INTO audit_logs(actor_id, action, object_type, object_id, source_type)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (actor_id, action, object_type, object_id, source_type),
                )
            finally:
                self._transaction_connection.reset(token)
