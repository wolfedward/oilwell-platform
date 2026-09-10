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
            # Table rebuilds are required to give pre-analysis databases the
            # same native foreign keys as a fresh install.  Keep legacy rename
            # semantics so child FKs continue to target the replacement table.
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("PRAGMA legacy_alter_table = ON")
            try:
                connection.executescript(schema)
                self._migrate_seed_columns(connection)
                self._migrate_auth_columns(connection)
                self._migrate_device_columns(connection)
                self._migrate_acquisition_columns(connection)
                self._migrate_analysis_columns(connection)
                self._migrate_strategy_columns(connection)
                self._migrate_command_columns(connection)
                self._backfill_strategy_effective_at(connection)
                self._migrate_workflow_columns(connection)
                self._normalize_legacy_timestamps(connection)
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    first = violations[0]
                    raise sqlite3.IntegrityError(
                        f"migration produced a foreign-key violation in {first[0]} row {first[1]}"
                    )
                connection.commit()
            finally:
                connection.execute("PRAGMA legacy_alter_table = OFF")
                connection.execute("PRAGMA foreign_keys = ON")

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
        connection.execute(
            "CREATE INDEX IF NOT EXISTS telemetry_report_power_idx "
            "ON telemetry(well_id,metric_code,status,datetime(measured_at),id)"
        )
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
        template_unique_columns = {
            tuple(
                row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")
            )
            for index in connection.execute("PRAGMA index_list(diagnosis_templates)")
            if index[2]
        }
        if ("condition_code",) in template_unique_columns:
            Database._rebuild_diagnosis_templates(connection)
        dynagraph_foreign_keys = {
            row[3] for row in connection.execute("PRAGMA foreign_key_list(dynagraphs)")
        }
        if not {"well_id", "device_id", "raw_message_id", "latest_model_run_id"}.issubset(
            dynagraph_foreign_keys
        ):
            Database._rebuild_dynagraphs(connection)
        diagnosis_foreign_keys = {
            row[3] for row in connection.execute("PRAGMA foreign_key_list(diagnoses)")
        }
        if not {"well_id", "dynagraph_id", "model_run_id", "template_id"}.issubset(
            diagnosis_foreign_keys
        ):
            Database._rebuild_diagnoses(connection)
        Database._create_analysis_timestamp_triggers(connection)
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
    def _drop_table_timestamp_triggers(connection, table_name: str) -> None:
        connection.execute(f"DROP TRIGGER IF EXISTS {table_name}_set_updated_at")
        connection.execute(
            f"DROP TRIGGER IF EXISTS {table_name}_normalize_inserted_timestamps"
        )

    @staticmethod
    def _rebuild_diagnosis_templates(connection: sqlite3.Connection) -> None:
        Database._drop_table_timestamp_triggers(connection, "diagnosis_templates")
        connection.execute(
            "ALTER TABLE diagnosis_templates RENAME TO diagnosis_templates_analysis_legacy"
        )
        connection.execute(
            """CREATE TABLE diagnosis_templates (
                   id TEXT PRIMARY KEY,
                   condition_code TEXT NOT NULL,
                   version TEXT NOT NULL,
                   features_json TEXT NOT NULL,
                   scales_json TEXT NOT NULL,
                   weights_json TEXT NOT NULL,
                   sample_count INTEGER NOT NULL CHECK(sample_count > 0),
                   status TEXT NOT NULL DEFAULT 'active',
                   actor_id INTEGER,
                   source_type TEXT NOT NULL DEFAULT 'algorithm',
                   created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
                   updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
                   UNIQUE(condition_code,version)
               )"""
        )
        connection.execute(
            """INSERT INTO diagnosis_templates(
                   id,condition_code,version,features_json,scales_json,weights_json,
                   sample_count,status,actor_id,source_type,created_at,updated_at
               ) SELECT id,condition_code,version,features_json,scales_json,weights_json,
                        sample_count,status,actor_id,source_type,created_at,updated_at
                   FROM diagnosis_templates_analysis_legacy"""
        )
        connection.execute("DROP TABLE diagnosis_templates_analysis_legacy")

    @staticmethod
    def _rebuild_dynagraphs(connection: sqlite3.Connection) -> None:
        Database._drop_table_timestamp_triggers(connection, "dynagraphs")
        connection.execute("ALTER TABLE dynagraphs RENAME TO dynagraphs_analysis_legacy")
        connection.execute(
            """CREATE TABLE dynagraphs (
                   id TEXT PRIMARY KEY,
                   well_id TEXT NOT NULL REFERENCES wells(id),
                   device_id TEXT REFERENCES devices(id),
                   raw_message_id INTEGER REFERENCES raw_messages(id),
                   captured_at TEXT NOT NULL,
                   sample_data TEXT NOT NULL,
                   conversion_json TEXT,
                   features_json TEXT,
                   model_version TEXT,
                   latest_model_run_id TEXT REFERENCES model_runs(id),
                   version INTEGER NOT NULL DEFAULT 1,
                   status TEXT NOT NULL DEFAULT 'available',
                   actor_id INTEGER,
                   source_type TEXT NOT NULL DEFAULT 'device',
                   created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
                   updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'))
               )"""
        )
        connection.execute(
            """INSERT INTO dynagraphs(
                   id,well_id,device_id,raw_message_id,captured_at,sample_data,
                   conversion_json,features_json,model_version,latest_model_run_id,
                   version,status,actor_id,source_type,created_at,updated_at
               ) SELECT id,well_id,device_id,raw_message_id,captured_at,sample_data,
                        conversion_json,features_json,model_version,latest_model_run_id,
                        version,status,actor_id,source_type,created_at,updated_at
                   FROM dynagraphs_analysis_legacy"""
        )
        connection.execute("DROP TABLE dynagraphs_analysis_legacy")

    @staticmethod
    def _rebuild_diagnoses(connection: sqlite3.Connection) -> None:
        Database._drop_table_timestamp_triggers(connection, "diagnoses")
        connection.execute("ALTER TABLE diagnoses RENAME TO diagnoses_analysis_legacy")
        connection.execute(
            """CREATE TABLE diagnoses (
                   id TEXT PRIMARY KEY,
                   well_id TEXT NOT NULL REFERENCES wells(id),
                   dynagraph_id TEXT REFERENCES dynagraphs(id),
                   model_run_id TEXT REFERENCES model_runs(id),
                   template_id TEXT REFERENCES diagnosis_templates(id),
                   template_version TEXT,
                   diagnosis_type TEXT NOT NULL,
                   confidence REAL,
                   evidence TEXT,
                   candidates_json TEXT,
                   rule_hits_json TEXT,
                   recommendation TEXT,
                   review_version INTEGER NOT NULL DEFAULT 1,
                   status TEXT NOT NULL DEFAULT 'new',
                   actor_id INTEGER,
                   source_type TEXT NOT NULL DEFAULT 'algorithm',
                   created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
                   updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'))
               )"""
        )
        connection.execute(
            """INSERT INTO diagnoses(
                   id,well_id,dynagraph_id,model_run_id,template_id,template_version,
                   diagnosis_type,confidence,evidence,candidates_json,rule_hits_json,
                   recommendation,review_version,status,actor_id,source_type,
                   created_at,updated_at
               ) SELECT id,well_id,dynagraph_id,model_run_id,template_id,template_version,
                        diagnosis_type,confidence,evidence,candidates_json,rule_hits_json,
                        recommendation,review_version,status,actor_id,source_type,
                        created_at,updated_at FROM diagnoses_analysis_legacy"""
        )
        connection.execute("DROP TABLE diagnoses_analysis_legacy")

    @staticmethod
    def _create_analysis_timestamp_triggers(connection: sqlite3.Connection) -> None:
        for table_name in ("dynagraphs", "diagnoses", "diagnosis_templates"):
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table_name}_set_updated_at
                    AFTER UPDATE ON {table_name}
                    FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
                    BEGIN
                        UPDATE {table_name} SET updated_at =
                            strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')
                        WHERE rowid = NEW.rowid;
                    END"""
            )
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table_name}_normalize_inserted_timestamps
                    AFTER INSERT ON {table_name}
                    FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00'
                                      OR NEW.updated_at NOT GLOB '*+08:00'
                    BEGIN
                        UPDATE {table_name} SET
                            created_at = CASE WHEN NEW.created_at GLOB '*+08:00'
                                THEN NEW.created_at ELSE strftime(
                                    '%Y-%m-%dT%H:%M:%f+08:00',NEW.created_at,'+8 hours') END,
                            updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00'
                                THEN NEW.updated_at ELSE strftime(
                                    '%Y-%m-%dT%H:%M:%f+08:00',NEW.updated_at,'+8 hours') END
                        WHERE rowid = NEW.rowid;
                    END"""
            )

    @staticmethod
    def _migrate_strategy_columns(connection: sqlite3.Connection) -> None:
        required = {
            "energy_strategies": (
                ("parent_strategy_id", "TEXT"), ("current_version_id", "TEXT"),
                ("version", "INTEGER NOT NULL DEFAULT 1"), ("created_by", "INTEGER"),
                ("submitted_by", "INTEGER"), ("decision_by", "INTEGER"),
                ("submitted_at", "TEXT"), ("decision_at", "TEXT"),
                ("effective_at", "TEXT"), ("approval_comment", "TEXT"),
            ),
            "strategy_versions": (
                ("created_by", "INTEGER"), ("submitted_at", "TEXT"),
            ),
            "approvals": (
                ("object_version", "INTEGER"), ("comment", "TEXT"),
                ("decided_at", "TEXT"),
            ),
        }
        for table_name, declarations in required.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}
            for column_name, declaration in declarations:
                if column_name not in columns:
                    connection.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
                    )
        foreign_keys = {
            row[3] for row in connection.execute("PRAGMA foreign_key_list(energy_strategies)")
        }
        if not {
            "well_id", "parent_strategy_id", "current_version_id", "created_by",
            "submitted_by", "decision_by",
        }.issubset(foreign_keys):
            Database._rebuild_energy_strategies(connection)
        version_foreign_keys = {
            row[3] for row in connection.execute(
                "PRAGMA foreign_key_list(strategy_versions)"
            )
        }
        if not {"strategy_id", "created_by"}.issubset(version_foreign_keys):
            Database._rebuild_strategy_versions(connection)
        connection.execute(
            """UPDATE energy_strategies SET current_version_id=(
                   SELECT version.id FROM strategy_versions AS version
                   WHERE version.strategy_id=energy_strategies.id
                   ORDER BY version.version_number DESC,version.id DESC LIMIT 1
               ) WHERE current_version_id IS NULL AND EXISTS(
                   SELECT 1 FROM strategy_versions AS version
                   WHERE version.strategy_id=energy_strategies.id
               )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS energy_strategies_list_idx "
            "ON energy_strategies(well_id,status,updated_at DESC,id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS energy_strategies_effect_period_idx "
            "ON energy_strategies(well_id,datetime(effective_at),id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS approvals_object_idx "
            "ON approvals(object_type,object_id,created_at DESC,id DESC)"
        )
        Database._create_strategy_timestamp_trigger(connection)
        Database._create_strategy_version_triggers(connection)
        Database._create_strategy_integrity_triggers(connection)

    @staticmethod
    def _backfill_strategy_effective_at(connection: sqlite3.Connection) -> None:
        """Run only after command columns exist on every legacy database."""
        connection.execute(
            """UPDATE energy_strategies AS strategy SET effective_at=COALESCE(
                   (SELECT MIN(command.completed_at) FROM device_commands AS command
                    WHERE command.strategy_id=strategy.id AND command.status='success'
                      AND command.command_type<>'rollback_cycle'
                      AND command.completed_at IS NOT NULL),
                   CASE WHEN strategy.source_type='simulator' THEN (
                       SELECT strftime('%Y-%m-%dT%H:%M:%f+08:00',
                                       MIN(telemetry.measured_at),'+8 hours','+15 days')
                       FROM telemetry WHERE telemetry.well_id=strategy.well_id
                   ) ELSE strategy.updated_at END
               )
               WHERE effective_at IS NULL
                 AND status IN ('active','effective','paused','rollback','rolled_back')"""
        )

    @staticmethod
    def _rebuild_energy_strategies(connection: sqlite3.Connection) -> None:
        Database._drop_table_timestamp_triggers(connection, "energy_strategies")
        connection.execute(
            "ALTER TABLE energy_strategies RENAME TO energy_strategies_workflow_legacy"
        )
        connection.execute(
            """CREATE TABLE energy_strategies (
                   id TEXT PRIMARY KEY,
                   well_id TEXT NOT NULL REFERENCES wells(id),
                   parent_strategy_id TEXT REFERENCES energy_strategies(id),
                   current_version_id TEXT REFERENCES strategy_versions(id),
                   name TEXT NOT NULL,
                   version INTEGER NOT NULL DEFAULT 1,
                   created_by INTEGER REFERENCES users(id),
                   submitted_by INTEGER REFERENCES users(id),
                   decision_by INTEGER REFERENCES users(id),
                   submitted_at TEXT,
                   decision_at TEXT,
                   effective_at TEXT,
                   approval_comment TEXT,
                   status TEXT NOT NULL DEFAULT 'draft',
                   actor_id INTEGER,
                   source_type TEXT NOT NULL DEFAULT 'manual',
                   created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
                   updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'))
               )"""
        )
        connection.execute(
            """INSERT INTO energy_strategies(
                   id,well_id,parent_strategy_id,current_version_id,name,version,
                   created_by,submitted_by,decision_by,submitted_at,decision_at,
                   effective_at,approval_comment,status,actor_id,source_type,created_at,updated_at
               ) SELECT id,well_id,parent_strategy_id,current_version_id,name,version,
                        created_by,submitted_by,decision_by,submitted_at,decision_at,
                        effective_at,approval_comment,status,actor_id,source_type,created_at,updated_at
                   FROM energy_strategies_workflow_legacy"""
        )
        connection.execute("DROP TABLE energy_strategies_workflow_legacy")

    @staticmethod
    def _create_strategy_timestamp_trigger(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS energy_strategies_set_updated_at
               AFTER UPDATE ON energy_strategies
               FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
               BEGIN
                   UPDATE energy_strategies SET updated_at =
                       strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')
                   WHERE rowid = NEW.rowid;
               END"""
        )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS energy_strategies_normalize_inserted_timestamps
               AFTER INSERT ON energy_strategies
               FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00'
                                 OR NEW.updated_at NOT GLOB '*+08:00'
               BEGIN
                   UPDATE energy_strategies SET
                       created_at = CASE WHEN NEW.created_at GLOB '*+08:00'
                           THEN NEW.created_at ELSE strftime(
                               '%Y-%m-%dT%H:%M:%f+08:00',NEW.created_at,'+8 hours') END,
                       updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00'
                           THEN NEW.updated_at ELSE strftime(
                               '%Y-%m-%dT%H:%M:%f+08:00',NEW.updated_at,'+8 hours') END
                   WHERE rowid = NEW.rowid;
               END"""
        )

    @staticmethod
    def _rebuild_strategy_versions(connection: sqlite3.Connection) -> None:
        Database._drop_table_timestamp_triggers(connection, "strategy_versions")
        connection.execute("DROP TRIGGER IF EXISTS strategy_versions_immutable_update")
        connection.execute("DROP TRIGGER IF EXISTS strategy_versions_immutable_state")
        connection.execute("DROP TRIGGER IF EXISTS strategy_versions_immutable_delete")
        connection.execute(
            "ALTER TABLE strategy_versions RENAME TO strategy_versions_workflow_legacy"
        )
        connection.execute(
            """CREATE TABLE strategy_versions (
                   id TEXT PRIMARY KEY,
                   strategy_id TEXT NOT NULL REFERENCES energy_strategies(id),
                   version_number INTEGER NOT NULL,
                   configuration TEXT NOT NULL,
                   created_by INTEGER REFERENCES users(id),
                   submitted_at TEXT,
                   status TEXT NOT NULL DEFAULT 'draft',
                   actor_id INTEGER,
                   source_type TEXT NOT NULL DEFAULT 'manual',
                   created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
                   updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
                   UNIQUE(strategy_id,version_number)
               )"""
        )
        connection.execute(
            """INSERT INTO strategy_versions(
                   id,strategy_id,version_number,configuration,created_by,submitted_at,
                   status,actor_id,source_type,created_at,updated_at
               ) SELECT id,strategy_id,version_number,configuration,created_by,submitted_at,
                        status,actor_id,source_type,created_at,updated_at
                   FROM strategy_versions_workflow_legacy"""
        )
        connection.execute("DROP TABLE strategy_versions_workflow_legacy")

    @staticmethod
    def _create_strategy_version_triggers(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS strategy_versions_set_updated_at
               AFTER UPDATE ON strategy_versions
               FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
               BEGIN
                   UPDATE strategy_versions SET updated_at =
                       strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')
                   WHERE rowid = NEW.rowid;
               END"""
        )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS strategy_versions_normalize_inserted_timestamps
               AFTER INSERT ON strategy_versions
               FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00'
                                 OR NEW.updated_at NOT GLOB '*+08:00'
               BEGIN
                   UPDATE strategy_versions SET
                       created_at = CASE WHEN NEW.created_at GLOB '*+08:00'
                           THEN NEW.created_at ELSE strftime(
                               '%Y-%m-%dT%H:%M:%f+08:00',NEW.created_at,'+8 hours') END,
                       updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00'
                           THEN NEW.updated_at ELSE strftime(
                               '%Y-%m-%dT%H:%M:%f+08:00',NEW.updated_at,'+8 hours') END
                   WHERE rowid = NEW.rowid;
               END"""
        )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS strategy_versions_immutable_update
               BEFORE UPDATE OF strategy_id,version_number,configuration ON strategy_versions
               FOR EACH ROW WHEN OLD.status <> 'draft' OR OLD.submitted_at IS NOT NULL
               BEGIN
                   SELECT RAISE(ABORT,'submitted strategy version is immutable');
               END"""
        )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS strategy_versions_immutable_state
               BEFORE UPDATE OF status,submitted_at ON strategy_versions
               FOR EACH ROW WHEN OLD.status <> 'draft' OR OLD.submitted_at IS NOT NULL
               BEGIN
                   SELECT RAISE(ABORT,'submitted strategy version state is immutable');
               END"""
        )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS strategy_versions_immutable_delete
               BEFORE DELETE ON strategy_versions
               FOR EACH ROW WHEN OLD.status <> 'draft' OR OLD.submitted_at IS NOT NULL
               BEGIN
                   SELECT RAISE(ABORT,'submitted strategy version is immutable');
               END"""
        )

    @staticmethod
    def _create_strategy_integrity_triggers(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS energy_strategies_current_version_owner_insert
               BEFORE INSERT ON energy_strategies
               FOR EACH ROW WHEN NEW.current_version_id IS NOT NULL AND NOT EXISTS(
                   SELECT 1 FROM strategy_versions
                   WHERE id=NEW.current_version_id AND strategy_id=NEW.id
               )
               BEGIN
                   SELECT RAISE(ABORT,'current strategy version belongs to another strategy');
               END"""
        )
        connection.execute(
            """CREATE TRIGGER IF NOT EXISTS energy_strategies_current_version_owner_update
               BEFORE UPDATE OF id,current_version_id ON energy_strategies
               FOR EACH ROW WHEN NEW.current_version_id IS NOT NULL AND NOT EXISTS(
                   SELECT 1 FROM strategy_versions
                   WHERE id=NEW.current_version_id AND strategy_id=NEW.id
               )
               BEGIN
                   SELECT RAISE(ABORT,'current strategy version belongs to another strategy');
               END"""
        )

    @staticmethod
    def _migrate_command_columns(connection: sqlite3.Connection) -> None:
        required = {
            "device_commands": (
                ("strategy_id", "TEXT"), ("signature", "TEXT"),
                ("idempotency_key", "TEXT"), ("issued_at", "TEXT"),
                ("expires_at", "TEXT"), ("next_attempt_at", "TEXT"),
                ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
                ("max_attempts", "INTEGER NOT NULL DEFAULT 3"),
                ("completed_at", "TEXT"), ("failure_reason", "TEXT"),
            ),
            "command_attempts": (
                ("attempt_number", "INTEGER"), ("state", "TEXT"),
                ("receipt_at", "TEXT"),
            ),
            "alarms": (
                ("strategy_id", "TEXT"), ("command_id", "TEXT"),
                ("details_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("version", "INTEGER NOT NULL DEFAULT 1"),
            ),
        }
        for table_name, declarations in required.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}
            for column_name, declaration in declarations:
                if column_name not in columns:
                    connection.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
                    )
        connection.execute(
            """UPDATE device_commands SET strategy_id=(
                   SELECT version.strategy_id FROM strategy_versions AS version
                   WHERE version.id=device_commands.strategy_version_id
               ) WHERE strategy_id IS NULL AND strategy_version_id IS NOT NULL"""
        )
        connection.execute(
            """WITH ranked_legacy AS (
                   SELECT candidate.id,
                          ROW_NUMBER() OVER(
                              PARTITION BY candidate.command_id
                              ORDER BY candidate.attempted_at,candidate.id
                          ) + COALESCE((
                              SELECT MAX(existing.attempt_number)
                              FROM command_attempts AS existing
                              WHERE existing.command_id=candidate.command_id
                          ),0) AS derived_number
                   FROM command_attempts AS candidate
                   WHERE candidate.attempt_number IS NULL
               )
               UPDATE command_attempts SET
                   attempt_number=COALESCE(attempt_number,(
                       SELECT derived_number FROM ranked_legacy
                       WHERE ranked_legacy.id=command_attempts.id
                   )),
                   state=COALESCE(state,status),
                   receipt_at=COALESCE(receipt_at,attempted_at)
               WHERE attempt_number IS NULL OR state IS NULL OR receipt_at IS NULL"""
        )
        command_foreign_keys = {
            row[3] for row in connection.execute("PRAGMA foreign_key_list(device_commands)")
        }
        if not {"device_id", "strategy_id", "strategy_version_id"}.issubset(
            command_foreign_keys
        ):
            Database._rebuild_device_commands(connection)
        alarm_foreign_keys = {
            row[3] for row in connection.execute("PRAGMA foreign_key_list(alarms)")
        }
        if not {"well_id", "diagnosis_id", "strategy_id", "command_id"}.issubset(
            alarm_foreign_keys
        ):
            Database._rebuild_alarms(connection)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS device_commands_idempotency_unique "
            "ON device_commands(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS device_commands_process_idx "
            "ON device_commands(status,next_attempt_at,expires_at,created_at,id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS device_commands_strategy_period_idx "
            "ON device_commands(strategy_id,status,command_type,datetime(completed_at),id)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS command_attempts_stage_unique "
            "ON command_attempts(command_id,attempt_number,state) "
            "WHERE attempt_number IS NOT NULL AND state IS NOT NULL"
        )
        connection.execute("DROP INDEX IF EXISTS alarms_command_unique")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS alarms_command_idx ON alarms(command_id)"
        )
        Database._create_command_timestamp_triggers(connection)

    @staticmethod
    def _rebuild_device_commands(connection: sqlite3.Connection) -> None:
        Database._drop_table_timestamp_triggers(connection, "device_commands")
        connection.execute(
            "ALTER TABLE device_commands RENAME TO device_commands_control_legacy"
        )
        connection.execute(
            """CREATE TABLE device_commands (
                   id TEXT PRIMARY KEY,
                   device_id TEXT NOT NULL REFERENCES devices(id),
                   strategy_id TEXT REFERENCES energy_strategies(id),
                   strategy_version_id TEXT REFERENCES strategy_versions(id),
                   command_type TEXT NOT NULL,
                   payload TEXT NOT NULL,
                   signature TEXT,
                   idempotency_key TEXT UNIQUE,
                   issued_at TEXT,
                   expires_at TEXT,
                   next_attempt_at TEXT,
                   attempt_count INTEGER NOT NULL DEFAULT 0,
                   max_attempts INTEGER NOT NULL DEFAULT 3,
                   completed_at TEXT,
                   failure_reason TEXT,
                   status TEXT NOT NULL DEFAULT 'pending',
                   actor_id INTEGER,
                   source_type TEXT NOT NULL DEFAULT 'system',
                   created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
                   updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'))
               )"""
        )
        connection.execute(
            """INSERT INTO device_commands(
                   id,device_id,strategy_id,strategy_version_id,command_type,payload,
                   signature,idempotency_key,issued_at,expires_at,next_attempt_at,
                   attempt_count,max_attempts,completed_at,failure_reason,status,
                   actor_id,source_type,created_at,updated_at
               ) SELECT id,device_id,strategy_id,strategy_version_id,command_type,payload,
                        signature,idempotency_key,issued_at,expires_at,next_attempt_at,
                        attempt_count,max_attempts,completed_at,failure_reason,status,
                        actor_id,source_type,created_at,updated_at
                   FROM device_commands_control_legacy"""
        )
        connection.execute("DROP TABLE device_commands_control_legacy")

    @staticmethod
    def _rebuild_alarms(connection: sqlite3.Connection) -> None:
        Database._drop_table_timestamp_triggers(connection, "alarms")
        connection.execute("ALTER TABLE alarms RENAME TO alarms_control_legacy")
        connection.execute(
            """CREATE TABLE alarms (
                   id TEXT PRIMARY KEY,
                   well_id TEXT NOT NULL REFERENCES wells(id),
                   diagnosis_id TEXT REFERENCES diagnoses(id),
                   strategy_id TEXT REFERENCES energy_strategies(id),
                   command_id TEXT REFERENCES device_commands(id),
                   alarm_type TEXT NOT NULL,
                   severity TEXT NOT NULL,
                   details_json TEXT NOT NULL DEFAULT '{}',
                   version INTEGER NOT NULL DEFAULT 1,
                   status TEXT NOT NULL DEFAULT 'new',
                   actor_id INTEGER,
                   source_type TEXT NOT NULL DEFAULT 'system',
                   created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
                   updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'))
               )"""
        )
        connection.execute(
            """INSERT INTO alarms(
                   id,well_id,diagnosis_id,strategy_id,command_id,alarm_type,severity,
                   details_json,version,status,actor_id,source_type,created_at,updated_at
               ) SELECT id,well_id,diagnosis_id,strategy_id,command_id,alarm_type,severity,
                        details_json,version,status,actor_id,source_type,created_at,updated_at
                   FROM alarms_control_legacy"""
        )
        connection.execute("DROP TABLE alarms_control_legacy")

    @staticmethod
    def _create_command_timestamp_triggers(connection: sqlite3.Connection) -> None:
        for table_name in ("device_commands", "alarms"):
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table_name}_set_updated_at
                    AFTER UPDATE ON {table_name}
                    FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
                    BEGIN
                        UPDATE {table_name} SET updated_at =
                            strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')
                        WHERE rowid = NEW.rowid;
                    END"""
            )
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table_name}_normalize_inserted_timestamps
                    AFTER INSERT ON {table_name}
                    FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00'
                                      OR NEW.updated_at NOT GLOB '*+08:00'
                    BEGIN
                        UPDATE {table_name} SET
                            created_at = CASE WHEN NEW.created_at GLOB '*+08:00'
                                THEN NEW.created_at ELSE strftime(
                                    '%Y-%m-%dT%H:%M:%f+08:00',NEW.created_at,'+8 hours') END,
                            updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00'
                                THEN NEW.updated_at ELSE strftime(
                                    '%Y-%m-%dT%H:%M:%f+08:00',NEW.updated_at,'+8 hours') END
                        WHERE rowid = NEW.rowid;
                    END"""
            )

    @staticmethod
    def _migrate_workflow_columns(connection: sqlite3.Connection) -> None:
        """Upgrade alarm/work-order history without losing seeded evidence."""
        required = {
            "alarms": (
                ("source_object_type", "TEXT"), ("source_object_id", "TEXT"),
                ("confirmed_at", "TEXT"), ("closed_at", "TEXT"),
                ("resolution_comment", "TEXT"),
            ),
            "work_orders": (
                ("kind", "TEXT NOT NULL DEFAULT 'inspection'"),
                ("assignee_id", "INTEGER"), ("due_at", "TEXT"),
                ("accepted_at", "TEXT"), ("completed_at", "TEXT"),
                ("closed_at", "TEXT"), ("closed_by", "INTEGER"),
                ("overdue_from_status", "TEXT"),
                ("version", "INTEGER NOT NULL DEFAULT 1"),
            ),
            "work_order_logs": (
                ("from_status", "TEXT"), ("to_status", "TEXT"),
                ("comment", "TEXT"), ("changed_at", "TEXT"),
            ),
        }
        for table_name, declarations in required.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}
            for column_name, declaration in declarations:
                if column_name not in columns:
                    connection.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
                    )
        connection.execute(
            """UPDATE alarms SET
                   source_object_type=CASE
                       WHEN command_id IS NOT NULL THEN 'command'
                       WHEN diagnosis_id IS NOT NULL THEN 'diagnosis'
                       WHEN strategy_id IS NOT NULL THEN 'strategy'
                       ELSE source_object_type END,
                   source_object_id=CASE
                       WHEN command_id IS NOT NULL THEN command_id
                       WHEN diagnosis_id IS NOT NULL THEN diagnosis_id
                       WHEN strategy_id IS NOT NULL THEN strategy_id
                       ELSE source_object_id END
               WHERE source_object_type IS NULL OR source_object_id IS NULL"""
        )
        connection.execute(
            """UPDATE work_order_logs SET
                   to_status=COALESCE(to_status,status),
                   comment=COALESCE(comment,message),
                   changed_at=COALESCE(changed_at,created_at)
               WHERE to_status IS NULL OR comment IS NULL OR changed_at IS NULL"""
        )
        connection.execute(
            """UPDATE work_orders SET assignee_id=(
                   SELECT user.id FROM users AS user
                   JOIN role_permissions AS grant_row ON grant_row.role_id=user.role_id
                   JOIN permissions AS permission ON permission.id=grant_row.permission_id
                   WHERE user.status='active' AND grant_row.status='active'
                     AND permission.status='active' AND permission.code='maintenance.manage'
                   ORDER BY CASE WHEN user.username='maintenance_engineer' THEN 0 ELSE 1 END,
                            user.id LIMIT 1
               ) WHERE assignee_id IS NULL"""
        )
        connection.execute(
            """UPDATE work_orders SET
                   due_at=COALESCE(due_at,strftime(
                       '%Y-%m-%dT%H:%M:%f+08:00',substr(created_at,1,19),'+4 hours'
                   )),
                   accepted_at=CASE WHEN status IN ('processing','completed','closed')
                       THEN COALESCE(accepted_at,created_at) ELSE accepted_at END,
                   completed_at=CASE WHEN status IN ('completed','closed')
                       THEN COALESCE(completed_at,updated_at) ELSE completed_at END,
                   closed_at=CASE WHEN status='closed'
                       THEN COALESCE(closed_at,updated_at) ELSE closed_at END,
                   closed_by=CASE WHEN status='closed' THEN COALESCE(closed_by,(
                       SELECT user.id FROM users AS user
                       JOIN role_permissions AS grant_row ON grant_row.role_id=user.role_id
                       JOIN permissions AS permission ON permission.id=grant_row.permission_id
                       WHERE user.status='active' AND grant_row.status='active'
                         AND permission.status='active' AND permission.code='diagnosis.review'
                       ORDER BY CASE WHEN user.username='reviewer' THEN 0 ELSE 1 END,
                                user.id LIMIT 1
                   )) ELSE closed_by END
               WHERE 1=1"""
        )
        order_foreign_keys = {
            row[3] for row in connection.execute("PRAGMA foreign_key_list(work_orders)")
        }
        if not {"well_id", "alarm_id", "assignee_id", "closed_by"}.issubset(order_foreign_keys):
            Database._rebuild_work_orders(connection)
        connection.execute("DROP INDEX IF EXISTS alarms_source_object_unique")
        connection.execute("DROP INDEX IF EXISTS work_orders_alarm_unique")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS alarms_source_object_idx "
            "ON alarms(source_object_type,source_object_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS work_orders_alarm_idx ON work_orders(alarm_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS work_orders_due_idx "
            "ON work_orders(status,due_at,id)"
        )
        for table_name in ("work_orders", "work_order_logs"):
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table_name}_set_updated_at
                    AFTER UPDATE ON {table_name}
                    FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
                    BEGIN
                        UPDATE {table_name} SET updated_at =
                            strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')
                        WHERE rowid = NEW.rowid;
                    END"""
            )
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table_name}_normalize_inserted_timestamps
                    AFTER INSERT ON {table_name}
                    FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00'
                                      OR NEW.updated_at NOT GLOB '*+08:00'
                    BEGIN
                        UPDATE {table_name} SET
                            created_at = CASE WHEN NEW.created_at GLOB '*+08:00'
                                THEN NEW.created_at ELSE strftime(
                                    '%Y-%m-%dT%H:%M:%f+08:00',NEW.created_at,'+8 hours') END,
                            updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00'
                                THEN NEW.updated_at ELSE strftime(
                                    '%Y-%m-%dT%H:%M:%f+08:00',NEW.updated_at,'+8 hours') END
                        WHERE rowid = NEW.rowid;
                    END"""
            )

    @staticmethod
    def _rebuild_work_orders(connection: sqlite3.Connection) -> None:
        Database._drop_table_timestamp_triggers(connection, "work_orders")
        connection.execute("ALTER TABLE work_orders RENAME TO work_orders_workflow_legacy")
        connection.execute(
            """CREATE TABLE work_orders (
                   id TEXT PRIMARY KEY,
                   well_id TEXT REFERENCES wells(id),
                   alarm_id TEXT REFERENCES alarms(id),
                   title TEXT NOT NULL,
                   kind TEXT NOT NULL DEFAULT 'inspection',
                   assignee_id INTEGER REFERENCES users(id),
                   due_at TEXT,
                   accepted_at TEXT,
                   completed_at TEXT,
                   closed_at TEXT,
                   closed_by INTEGER REFERENCES users(id),
                   overdue_from_status TEXT,
                   version INTEGER NOT NULL DEFAULT 1,
                   status TEXT NOT NULL DEFAULT 'pending',
                   actor_id INTEGER,
                   source_type TEXT NOT NULL DEFAULT 'manual',
                   created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
                   updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'))
               )"""
        )
        connection.execute(
            """INSERT INTO work_orders(
                   id,well_id,alarm_id,title,kind,assignee_id,due_at,accepted_at,
                   completed_at,closed_at,closed_by,overdue_from_status,version,status,
                   actor_id,source_type,created_at,updated_at
               ) SELECT id,well_id,alarm_id,title,kind,assignee_id,due_at,accepted_at,
                        completed_at,closed_at,closed_by,overdue_from_status,version,status,
                        actor_id,source_type,created_at,updated_at
                   FROM work_orders_workflow_legacy"""
        )
        connection.execute("DROP TABLE work_orders_workflow_legacy")

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
        *, source_type: str = "system", details_json: str = '{"after":{},"before":{}}',
        auto_audit: bool = True, immediate: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        """Commit business changes and their audit entry as one transaction."""
        if source_type not in {"system", "manual", "simulator", "device", "virtual_device", "algorithm"}:
            raise ValueError("unsupported audit source_type")
        with self.connection() as connection:
            token = self._transaction_connection.set(connection)
            try:
                if immediate:
                    connection.execute("BEGIN IMMEDIATE")
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
