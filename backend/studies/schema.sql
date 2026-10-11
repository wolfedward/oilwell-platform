CREATE TABLE IF NOT EXISTS study_schema_version (
    version INTEGER PRIMARY KEY CHECK(version = 1)
);
INSERT OR IGNORE INTO study_schema_version(version) VALUES(1);

CREATE TABLE IF NOT EXISTS study_records (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
    algorithm_version TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('software_fixture','submitted_observation')),
    source_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    result_status TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_sha256 TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
    UNIQUE(actor_id,request_id)
);
CREATE INDEX IF NOT EXISTS study_records_kind_created ON study_records(kind,created_at DESC,id DESC);

CREATE TABLE IF NOT EXISTS study_events (
    id INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES study_records(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours'))
);
CREATE TABLE IF NOT EXISTS study_reviews (
    id INTEGER PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES study_records(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    version INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','revoked')),
    comment TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
    UNIQUE(record_id,version)
);
CREATE TABLE IF NOT EXISTS study_selections (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    record_id TEXT REFERENCES study_records(id),
    review_version INTEGER,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    comment TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+08:00','now','+8 hours')),
    UNIQUE(kind,version)
);
CREATE TABLE IF NOT EXISTS study_receipts (
    actor_id INTEGER NOT NULL REFERENCES users(id),
    request_id TEXT NOT NULL,
    action TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    PRIMARY KEY(actor_id,request_id)
);

CREATE TRIGGER IF NOT EXISTS study_records_no_update BEFORE UPDATE ON study_records
BEGIN SELECT RAISE(ABORT,'study records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS study_records_no_delete BEFORE DELETE ON study_records
BEGIN SELECT RAISE(ABORT,'study records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS study_events_no_update BEFORE UPDATE ON study_events
BEGIN SELECT RAISE(ABORT,'study events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS study_events_no_delete BEFORE DELETE ON study_events
BEGIN SELECT RAISE(ABORT,'study events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS study_reviews_no_update BEFORE UPDATE ON study_reviews
BEGIN SELECT RAISE(ABORT,'study reviews are immutable'); END;
CREATE TRIGGER IF NOT EXISTS study_reviews_no_delete BEFORE DELETE ON study_reviews
BEGIN SELECT RAISE(ABORT,'study reviews are immutable'); END;
CREATE TRIGGER IF NOT EXISTS study_selections_no_update BEFORE UPDATE ON study_selections
BEGIN SELECT RAISE(ABORT,'study selections are immutable'); END;
CREATE TRIGGER IF NOT EXISTS study_selections_no_delete BEFORE DELETE ON study_selections
BEGIN SELECT RAISE(ABORT,'study selections are immutable'); END;
CREATE TRIGGER IF NOT EXISTS study_receipts_no_update BEFORE UPDATE ON study_receipts
BEGIN SELECT RAISE(ABORT,'study receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS study_receipts_no_delete BEFORE DELETE ON study_receipts
BEGIN SELECT RAISE(ABORT,'study receipts are immutable'); END;
