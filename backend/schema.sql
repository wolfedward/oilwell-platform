CREATE TABLE IF NOT EXISTS units (
    id TEXT PRIMARY KEY,
    code TEXT UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS well_groups (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(id),
    code TEXT UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wells (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES well_groups(id),
    code TEXT UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS device_channels (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    channel_code TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(device_id, channel_code)
);

CREATE TABLE IF NOT EXISTS device_heartbeats (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id),
    reported_at TEXT NOT NULL,
    status TEXT NOT NULL,
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'device',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY,
    device_id TEXT REFERENCES devices(id),
    received_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'device',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dynagraphs (
    id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(id),
    captured_at TEXT NOT NULL,
    sample_data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'device',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dynagraph_features (
    id INTEGER PRIMARY KEY,
    dynagraph_id TEXT NOT NULL REFERENCES dynagraphs(id),
    feature_name TEXT NOT NULL,
    feature_value REAL,
    status TEXT NOT NULL DEFAULT 'valid',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'algorithm',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recovery_tests (
    id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(id),
    tested_at TEXT NOT NULL,
    result_data TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS energy_strategies (
    id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL REFERENCES wells(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES energy_strategies(id),
    version_number INTEGER NOT NULL,
    configuration TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS command_attempts (
    id INTEGER PRIMARY KEY,
    command_id TEXT NOT NULL REFERENCES device_commands(id),
    attempted_at TEXT NOT NULL,
    result_detail TEXT,
    status TEXT NOT NULL,
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    role_id INTEGER REFERENCES roles(id),
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    decision TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS work_orders (
    id TEXT PRIMARY KEY,
    well_id TEXT REFERENCES wells(id),
    alarm_id TEXT REFERENCES alarms(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS work_order_logs (
    id INTEGER PRIMARY KEY,
    work_order_id TEXT NOT NULL REFERENCES work_orders(id),
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT,
    status TEXT NOT NULL DEFAULT 'recorded',
    source_type TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'recorded',
    actor_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
