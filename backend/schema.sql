CREATE TABLE IF NOT EXISTS units (
    id TEXT PRIMARY KEY,
    code TEXT UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS well_groups (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(id),
    code TEXT UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS wells (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES well_groups(id),
    code TEXT UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    is_low_output INTEGER NOT NULL DEFAULT 0 CHECK(is_low_output IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    well_id TEXT REFERENCES wells(id),
    serial_number TEXT UNIQUE,
    device_type TEXT NOT NULL,
    firmware_version TEXT,
    status TEXT NOT NULL DEFAULT 'offline',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS device_channels (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    channel_code TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    UNIQUE(device_id, channel_code)
);

CREATE TABLE IF NOT EXISTS device_heartbeats (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    reported_at TEXT NOT NULL,
    status TEXT NOT NULL,
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'device',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(id),
    device_id TEXT REFERENCES devices(id),
    channel_id TEXT REFERENCES device_channels(id),
    measured_at TEXT NOT NULL,
    metric_code TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    status TEXT NOT NULL DEFAULT 'valid',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'device',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS production_daily (
    well_id TEXT NOT NULL REFERENCES wells(id),
    day TEXT NOT NULL,
    daily_oil REAL NOT NULL,
    daily_fluid REAL NOT NULL,
    power_kwh REAL NOT NULL,
    pump_efficiency REAL NOT NULL,
    stroke_m REAL NOT NULL,
    frequency_hz REAL NOT NULL,
    runtime_hours REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'valid',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    PRIMARY KEY(well_id, day)
);

CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY,
    device_id TEXT REFERENCES devices(id),
    received_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'device',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS data_quality_events (
    id INTEGER PRIMARY KEY,
    telemetry_id INTEGER REFERENCES telemetry(id),
    well_id TEXT REFERENCES wells(id),
    event_type TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);
CREATE TABLE IF NOT EXISTS dynagraphs (
    id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(id),
    captured_at TEXT NOT NULL,
    sample_data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'device',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS dynagraph_features (
    id INTEGER PRIMARY KEY,
    dynagraph_id TEXT NOT NULL REFERENCES dynagraphs(id),
    feature_name TEXT NOT NULL,
    feature_value REAL,
    status TEXT NOT NULL DEFAULT 'valid',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'algorithm',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    UNIQUE(dynagraph_id, feature_name)
);

CREATE TABLE IF NOT EXISTS model_runs (
    id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    model_version TEXT,
    input_object_type TEXT,
    input_object_id TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'algorithm',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS diagnoses (
    id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(id),
    dynagraph_id TEXT REFERENCES dynagraphs(id),
    model_run_id TEXT REFERENCES model_runs(id),
    diagnosis_type TEXT NOT NULL,
    confidence REAL,
    evidence TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'algorithm',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS recovery_tests (
    id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(id),
    tested_at TEXT NOT NULL,
    result_data TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS energy_strategies (
    id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES energy_strategies(id),
    version_number INTEGER NOT NULL,
    configuration TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    UNIQUE(strategy_id, version_number)
);

CREATE TABLE IF NOT EXISTS device_commands (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    strategy_version_id TEXT REFERENCES strategy_versions(id),
    command_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS command_attempts (
    id INTEGER PRIMARY KEY,
    command_id TEXT NOT NULL REFERENCES device_commands(id),
    attempted_at TEXT NOT NULL,
    result_detail TEXT,
    status TEXT NOT NULL,
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS alarms (
    id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(id),
    diagnosis_id TEXT REFERENCES diagnoses(id),
    alarm_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    role_id INTEGER REFERENCES roles(id),
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    decision TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS work_orders (
    id TEXT PRIMARY KEY,
    well_id TEXT REFERENCES wells(id),
    alarm_id TEXT REFERENCES alarms(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS work_order_logs (
    id INTEGER PRIMARY KEY,
    work_order_id TEXT NOT NULL REFERENCES work_orders(id),
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'generated',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT,
    status TEXT NOT NULL DEFAULT 'recorded',
    source_type TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'recorded',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours'))
);

CREATE TRIGGER IF NOT EXISTS units_set_updated_at
AFTER UPDATE ON units
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE units SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS well_groups_set_updated_at
AFTER UPDATE ON well_groups
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE well_groups SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS wells_set_updated_at
AFTER UPDATE ON wells
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE wells SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS devices_set_updated_at
AFTER UPDATE ON devices
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE devices SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS device_channels_set_updated_at
AFTER UPDATE ON device_channels
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE device_channels SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS device_heartbeats_set_updated_at
AFTER UPDATE ON device_heartbeats
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE device_heartbeats SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS telemetry_set_updated_at
AFTER UPDATE ON telemetry
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE telemetry SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS production_daily_set_updated_at
AFTER UPDATE ON production_daily
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE production_daily SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE well_id = NEW.well_id AND day = NEW.day;
END;

CREATE TRIGGER IF NOT EXISTS raw_messages_set_updated_at
AFTER UPDATE ON raw_messages
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE raw_messages SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS data_quality_events_set_updated_at
AFTER UPDATE ON data_quality_events
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE data_quality_events SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS dynagraphs_set_updated_at
AFTER UPDATE ON dynagraphs
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE dynagraphs SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS dynagraph_features_set_updated_at
AFTER UPDATE ON dynagraph_features
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE dynagraph_features SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS diagnoses_set_updated_at
AFTER UPDATE ON diagnoses
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE diagnoses SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS model_runs_set_updated_at
AFTER UPDATE ON model_runs
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE model_runs SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS recovery_tests_set_updated_at
AFTER UPDATE ON recovery_tests
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE recovery_tests SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS energy_strategies_set_updated_at
AFTER UPDATE ON energy_strategies
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE energy_strategies SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS strategy_versions_set_updated_at
AFTER UPDATE ON strategy_versions
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE strategy_versions SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS device_commands_set_updated_at
AFTER UPDATE ON device_commands
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE device_commands SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS command_attempts_set_updated_at
AFTER UPDATE ON command_attempts
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE command_attempts SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS alarms_set_updated_at
AFTER UPDATE ON alarms
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE alarms SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS approvals_set_updated_at
AFTER UPDATE ON approvals
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE approvals SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS work_orders_set_updated_at
AFTER UPDATE ON work_orders
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE work_orders SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS work_order_logs_set_updated_at
AFTER UPDATE ON work_order_logs
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE work_order_logs SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS reports_set_updated_at
AFTER UPDATE ON reports
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE reports SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS users_set_updated_at
AFTER UPDATE ON users
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE users SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS roles_set_updated_at
AFTER UPDATE ON roles
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE roles SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS audit_logs_set_updated_at
AFTER UPDATE ON audit_logs
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE audit_logs SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS system_events_set_updated_at
AFTER UPDATE ON system_events
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE system_events SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+08:00', 'now', '+8 hours') WHERE rowid = NEW.rowid;
END;

-- Legacy databases may retain CURRENT_TIMESTAMP column defaults after migration.
-- Normalize values supplied by those defaults without rebuilding the tables.
CREATE TRIGGER IF NOT EXISTS units_normalize_inserted_timestamps
AFTER INSERT ON units
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE units SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS well_groups_normalize_inserted_timestamps
AFTER INSERT ON well_groups
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE well_groups SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS wells_normalize_inserted_timestamps
AFTER INSERT ON wells
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE wells SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS devices_normalize_inserted_timestamps
AFTER INSERT ON devices
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE devices SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS device_channels_normalize_inserted_timestamps
AFTER INSERT ON device_channels
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE device_channels SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS device_heartbeats_normalize_inserted_timestamps
AFTER INSERT ON device_heartbeats
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE device_heartbeats SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS telemetry_normalize_inserted_timestamps
AFTER INSERT ON telemetry
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE telemetry SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS raw_messages_normalize_inserted_timestamps
AFTER INSERT ON raw_messages
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE raw_messages SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS data_quality_events_normalize_inserted_timestamps
AFTER INSERT ON data_quality_events
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE data_quality_events SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS dynagraphs_normalize_inserted_timestamps
AFTER INSERT ON dynagraphs
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE dynagraphs SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS dynagraph_features_normalize_inserted_timestamps
AFTER INSERT ON dynagraph_features
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE dynagraph_features SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS diagnoses_normalize_inserted_timestamps
AFTER INSERT ON diagnoses
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE diagnoses SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS model_runs_normalize_inserted_timestamps
AFTER INSERT ON model_runs
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE model_runs SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS recovery_tests_normalize_inserted_timestamps
AFTER INSERT ON recovery_tests
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE recovery_tests SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS energy_strategies_normalize_inserted_timestamps
AFTER INSERT ON energy_strategies
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE energy_strategies SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS strategy_versions_normalize_inserted_timestamps
AFTER INSERT ON strategy_versions
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE strategy_versions SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS device_commands_normalize_inserted_timestamps
AFTER INSERT ON device_commands
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE device_commands SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS command_attempts_normalize_inserted_timestamps
AFTER INSERT ON command_attempts
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE command_attempts SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS alarms_normalize_inserted_timestamps
AFTER INSERT ON alarms
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE alarms SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS approvals_normalize_inserted_timestamps
AFTER INSERT ON approvals
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE approvals SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS work_orders_normalize_inserted_timestamps
AFTER INSERT ON work_orders
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE work_orders SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS work_order_logs_normalize_inserted_timestamps
AFTER INSERT ON work_order_logs
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE work_order_logs SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS reports_normalize_inserted_timestamps
AFTER INSERT ON reports
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE reports SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS users_normalize_inserted_timestamps
AFTER INSERT ON users
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE users SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS roles_normalize_inserted_timestamps
AFTER INSERT ON roles
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE roles SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS audit_logs_normalize_inserted_timestamps
AFTER INSERT ON audit_logs
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE audit_logs SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER IF NOT EXISTS system_events_normalize_inserted_timestamps
AFTER INSERT ON system_events
FOR EACH ROW WHEN NEW.created_at NOT GLOB '*+08:00' OR NEW.updated_at NOT GLOB '*+08:00'
BEGIN
    UPDATE system_events SET
        created_at = CASE WHEN NEW.created_at GLOB '*+08:00' THEN NEW.created_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.created_at, '+8 hours') END,
        updated_at = CASE WHEN NEW.updated_at GLOB '*+08:00' THEN NEW.updated_at ELSE strftime('%Y-%m-%dT%H:%M:%f+08:00', NEW.updated_at, '+8 hours') END
    WHERE rowid = NEW.rowid;
END;
