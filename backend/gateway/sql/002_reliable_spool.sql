CREATE TABLE uplink_queue_v2 (
    message_id TEXT PRIMARY KEY NOT NULL,
    topic TEXT NOT NULL,
    payload BLOB NOT NULL CHECK(typeof(payload)='blob'),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
    qos INTEGER NOT NULL CHECK(qos IN (1,2)),
    confirmation_mode TEXT NOT NULL CHECK(confirmation_mode IN ('broker','application')),
    state TEXT NOT NULL CHECK(state IN ('pending','inflight','awaiting_receipt','stored','quarantined','broker_confirmed','legacy_broker_confirmed','dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    next_attempt_at REAL NOT NULL,
    created_at REAL NOT NULL,
    claimed_at REAL,
    lease_until REAL,
    lease_token TEXT,
    broker_acked_at REAL,
    receipt_id TEXT,
    last_error_code TEXT,
    legacy_state TEXT,
    legacy_error TEXT,
    legacy_claimed_at REAL,
    legacy_acked_at REAL,
    CHECK((lease_token IS NULL AND lease_until IS NULL) OR (lease_token IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK(confirmation_mode='application' OR state NOT IN ('awaiting_receipt','stored','quarantined'))
);
CREATE INDEX uplink_queue_v2_ready_idx ON uplink_queue_v2(state,next_attempt_at,created_at);
CREATE TRIGGER queue_v2_identity_immutable BEFORE UPDATE OF message_id,topic,payload,payload_sha256,qos,confirmation_mode ON uplink_queue_v2
BEGIN SELECT RAISE(ABORT,'immutable queue identity'); END;
CREATE TRIGGER queue_v2_no_delete BEFORE DELETE ON uplink_queue_v2
BEGIN SELECT RAISE(ABORT,'queue retention required'); END;
CREATE TABLE spool_receipts (
    receipt_id TEXT PRIMARY KEY NOT NULL,
    message_id TEXT NOT NULL UNIQUE REFERENCES uplink_queue_v2(message_id),
    topic TEXT NOT NULL,
    payload BLOB NOT NULL CHECK(typeof(payload)='blob' AND length(payload)<=4096),
    received_at REAL NOT NULL
);
CREATE TRIGGER spool_receipts_immutable BEFORE UPDATE ON spool_receipts
BEGIN SELECT RAISE(ABORT,'immutable application receipt'); END;
CREATE TRIGGER spool_receipts_no_delete BEFORE DELETE ON spool_receipts
BEGIN SELECT RAISE(ABORT,'receipt retention required'); END;
CREATE TABLE spool_security_events (
    error_code TEXT NOT NULL,
    occurrences INTEGER NOT NULL CHECK(occurrences>0),
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    topic_sha256 TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    PRIMARY KEY(error_code,topic_sha256,payload_sha256)
);
CREATE TABLE spool_capacity (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    max_messages INTEGER NOT NULL CHECK(max_messages>0),
    max_payload_bytes INTEGER NOT NULL CHECK(max_payload_bytes>0),
    min_free_bytes INTEGER NOT NULL CHECK(min_free_bytes>0),
    used_messages INTEGER NOT NULL CHECK(used_messages BETWEEN 0 AND max_messages),
    used_payload_bytes INTEGER NOT NULL CHECK(used_payload_bytes BETWEEN 0 AND max_payload_bytes),
    last_clock_seen REAL NOT NULL,
    last_clock_warning REAL,
    security_overflow_count INTEGER NOT NULL DEFAULT 0 CHECK(security_overflow_count>=0)
);
CREATE TABLE spool_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL CHECK(version=2),
    migration_sha256 TEXT NOT NULL,
    schema_sha256 TEXT NOT NULL,
    initialized_at REAL NOT NULL
);
CREATE VIEW uplink_spool AS SELECT message_id,topic,payload,qos,
    coalesce(legacy_state,CASE state WHEN 'broker_confirmed' THEN 'acked' ELSE state END) AS state,
    attempts,next_attempt_at,legacy_claimed_at AS claimed_at,created_at,legacy_acked_at AS acked_at,legacy_error AS last_error
    FROM uplink_queue_v2;
