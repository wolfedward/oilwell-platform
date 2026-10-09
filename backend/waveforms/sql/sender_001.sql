CREATE TABLE sender_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL CHECK(version=1),
    source_sha256 TEXT NOT NULL,
    structure_sha256 TEXT NOT NULL,
    owner_token TEXT NOT NULL,
    max_bytes INTEGER NOT NULL CHECK(max_bytes>0),
    gateway_id TEXT,
    topic_prefix TEXT,
    CHECK((gateway_id IS NULL) = (topic_prefix IS NULL))
);
CREATE TABLE sender_capture (
    queue_id TEXT PRIMARY KEY,
    gateway_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    file_sha256 TEXT NOT NULL UNIQUE,
    manifest_sha256 TEXT NOT NULL,
    content BLOB NOT NULL,
    charge INTEGER NOT NULL CHECK(charge>0),
    state TEXT NOT NULL CHECK(state IN ('awaiting_capture_receipt','archived','failed','local_corrupt')),
    receipt BLOB,
    UNIQUE(gateway_id,device_id,capture_id)
);
CREATE TABLE sender_message (
    queue_id TEXT NOT NULL REFERENCES sender_capture(queue_id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    offset INTEGER NOT NULL CHECK(offset>=0),
    length INTEGER NOT NULL CHECK(length>0),
    payload_sha256 TEXT NOT NULL,
    stream_id TEXT,
    chunk_index INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 2147483647),
    broker_accepted INTEGER NOT NULL DEFAULT 0 CHECK(broker_accepted IN (0,1)),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','stored','quarantined')),
    receipt BLOB,
    PRIMARY KEY(queue_id,ordinal)
);
CREATE TABLE sender_event (
    slot INTEGER PRIMARY KEY CHECK(slot BETWEEN 0 AND 127),
    code TEXT NOT NULL CHECK(length(code)<=48)
);
CREATE TABLE sender_event_cursor (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    next_slot INTEGER NOT NULL CHECK(next_slot BETWEEN 0 AND 127)
);
