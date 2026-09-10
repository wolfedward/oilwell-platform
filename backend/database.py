"""SQLite persistence boundary for the offline platform."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence


TIMESTAMP_TABLES = (
    "units", "well_groups", "wells", "devices", "device_channels", "device_heartbeats",
    "telemetry", "production_daily", "seed_manifests", "raw_messages", "data_quality_events", "dynagraphs", "dynagraph_features",
    "diagnoses", "model_runs", "diagnosis_templates", "production_estimates", "diagnosis_reviews",
    "recovery_tests", "energy_strategies", "strategy_versions",
    "device_commands", "command_attempts", "alarms", "approvals", "work_orders",
    "work_order_logs", "reports", "users", "roles", "permissions", "role_permissions", "sessions", "auth_bootstrap_state",
    "audit_logs", "system_events", "device_scenarios", "device_events",
    "acquisition_cursors",
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
            self._migrate_auth_columns(connection)
            self._migrate_device_columns(connection)
            self._migrate_acquisition_columns(connection)
            self._migrate_analysis_columns(connection)
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
        if "version" not in columns:
            connection.execute("ALTER TABLE wells ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        manifest_columns = {row[1] for row in connection.execute("PRAGMA table_info(seed_manifests)")}
        if "content_hash" not in manifest_columns:
            connection.execute("ALTER TABLE seed_manifests ADD COLUMN content_hash TEXT")

    @staticmethod
    def _migrate_auth_columns(connection: sqlite3.Connection) -> None:
        """Add authentication fields without rebuilding a populated Task 3 database."""
        required = {
            "users": (
                ("password_hash", "TEXT"), ("password_salt", "TEXT"),
                ("password_iterations", "INTEGER NOT NULL DEFAULT 300000"),
                ("last_login_at", "TEXT"), ("version", "INTEGER NOT NULL DEFAULT 1"),
            ),
            "roles": (("version", "INTEGER NOT NULL DEFAULT 1"),),
            "audit_logs": (("details_json", "TEXT NOT NULL DEFAULT '{}'"),),
        }
        for table_name, columns_to_add in required.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}
            for column_name, declaration in columns_to_add:
                if column_name not in columns:
                    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}")
    @staticmethod
    def _migrate_device_columns(connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(device_channels)")}
        for name, declaration in (("enabled", "INTEGER NOT NULL DEFAULT 1"), ("sample_interval_ms", "INTEGER NOT NULL DEFAULT 60000"), ("scale", "REAL NOT NULL DEFAULT 1.0"), ("offset", "REAL NOT NULL DEFAULT 0.0"), ("version", "INTEGER NOT NULL DEFAULT 1")):
            if name not in columns:
                connection.execute(f"ALTER TABLE device_channels ADD COLUMN {name} {declaration}")
        scenario_columns = {row[1] for row in connection.execute("PRAGMA table_info(device_scenarios)")}
        if "duration_minutes" not in scenario_columns:
            connection.execute("ALTER TABLE device_scenarios ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _migrate_acquisition_columns(connection: sqlite3.Connection) -> None:
        required = {
            "raw_messages": (
                ("claimed_device_id", "TEXT"), ("raw_frame", "BLOB"),
                ("frame_digest", "TEXT"), ("sequence_text", "TEXT"),
                ("sequence_number", "INTEGER"), ("reported_at", "TEXT"),
                ("frame_kind", "TEXT"), ("rejection_code", "TEXT"),
                ("rejection_detail", "TEXT"), ("replay_event_id", "INTEGER"),
            ),
            "device_heartbeats": (("raw_message_id", "INTEGER"),),
            "telemetry": (("raw_message_id", "INTEGER"),),
            "data_quality_events": (
                ("device_id", "TEXT"), ("raw_message_id", "INTEGER"),
                ("kind", "TEXT"), ("missing_from", "INTEGER"),
                ("missing_to", "INTEGER"), ("missing_sequences", "TEXT"),
                ("resolution_detail", "TEXT"), ("resolved_at", "TEXT"),
                ("version", "INTEGER NOT NULL DEFAULT 1"),
            ),
        }
        for table_name, columns_to_add in required.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}
            for column_name, declaration in columns_to_add:
                if column_name not in columns:
                    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}")
        # These indexes reference columns introduced above. Creating them only
        # after the ALTER statements keeps upgrades from pre-acquisition
        # databases safe while remaining idempotent for current databases.
        connection.execute("CREATE INDEX IF NOT EXISTS raw_messages_live_cursor_idx ON raw_messages(source_type, id)")
        connection.execute("CREATE INDEX IF NOT EXISTS raw_messages_device_sequence_idx ON raw_messages(device_id, sequence_text, status)")
        connection.execute("CREATE INDEX IF NOT EXISTS data_quality_live_status_idx ON data_quality_events(source_type, device_id, kind, status, id)")
        connection.execute("CREATE INDEX IF NOT EXISTS telemetry_raw_message_idx ON telemetry(raw_message_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS device_heartbeats_raw_message_idx ON device_heartbeats(raw_message_id)")

    @staticmethod
    def _migrate_analysis_columns(connection: sqlite3.Connection) -> None:
        """Upgrade populated databases and materialize accepted electric cards."""
        required = {
            "dynagraphs": (
                ("device_id", "TEXT"), ("raw_message_id", "INTEGER"),
                ("conversion_json", "TEXT"), ("features_json", "TEXT"),
                ("model_version", "TEXT"), ("latest_model_run_id", "TEXT"),
                ("version", "INTEGER NOT NULL DEFAULT 1"),
            ),
            "model_runs": (
                ("input_snapshot", "TEXT"), ("parameters_json", "TEXT"),
                ("output_snapshot", "TEXT"), ("warnings_json", "TEXT"),
                ("completed_at", "TEXT"),
            ),
            "diagnoses": (
                ("template_id", "TEXT"), ("template_version", "TEXT"),
                ("candidates_json", "TEXT"), ("rule_hits_json", "TEXT"),
                ("recommendation", "TEXT"),
                ("review_version", "INTEGER NOT NULL DEFAULT 1"),
            ),
        }
        for table_name, columns_to_add in required.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}
            for column_name, declaration in columns_to_add:
                if column_name not in columns:
                    connection.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
                    )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS dynagraphs_raw_message_unique "
            "ON dynagraphs(raw_message_id) WHERE raw_message_id IS NOT NULL"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS dynagraphs_live_list_idx "
            "ON dynagraphs(well_id,status,captured_at DESC,id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS model_runs_input_idx "
            "ON model_runs(input_object_type,input_object_id,created_at DESC,id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS diagnosis_reviews_diagnosis_idx "
            "ON diagnosis_reviews(diagnosis_id,created_at,id)"
        )
        connection.execute(
            """INSERT OR IGNORE INTO dynagraphs(
                   id,well_id,device_id,raw_message_id,captured_at,sample_data,status,source_type
               )
               SELECT 'DG-RAW-' || printf('%012d', raw.id), device.well_id,
                      raw.device_id, raw.id, COALESCE(raw.reported_at,raw.received_at),
                      raw.payload, 'unprocessed', raw.source_type
               FROM raw_messages AS raw
               JOIN devices AS device ON device.id=raw.device_id
               WHERE raw.frame_kind='electric_card'
                 AND raw.status IN ('accepted','replayed')
                 AND raw.source_type='virtual_device'"""
        )
        broken = connection.execute(
            """SELECT raw.id
               FROM raw_messages AS raw
               JOIN devices AS device ON device.id=raw.device_id
               LEFT JOIN dynagraphs AS card ON card.raw_message_id=raw.id
               WHERE raw.frame_kind='electric_card'
                 AND raw.status IN ('accepted','replayed')
                 AND raw.source_type='virtual_device'
                 AND (card.id IS NULL OR card.well_id<>device.well_id
                      OR card.device_id<>raw.device_id OR card.sample_data<>raw.payload
                      OR card.captured_at<>COALESCE(raw.reported_at,raw.received_at))
               LIMIT 1"""
        ).fetchone()
        if broken is not None:
            raise sqlite3.IntegrityError(
                f"electric-card lineage migration failed for raw message {broken['id']}"
            )

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
        *, source_type: str = "system", details_json: str = '{"after":{},"before":{}}', auto_audit: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        """Commit business changes and their audit entry as one transaction."""
        if source_type not in {"system", "manual", "simulator", "device", "virtual_device", "algorithm"}:
            raise ValueError("unsupported audit source_type")
        with self.connection() as connection:
            token = self._transaction_connection.set(connection)
            try:
                yield connection
                if auto_audit:
                    connection.execute(
                        """
                        INSERT INTO audit_logs(actor_id, action, object_type, object_id, details_json, source_type)
                        VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (actor_id, action, object_type, object_id, details_json, source_type),
                    )
            finally:
                self._transaction_connection.reset(token)
