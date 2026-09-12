CREATE TABLE IF NOT EXISTS ingest_messages (
    message_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    sequence_first BIGINT NOT NULL CHECK (sequence_first >= 0),
    sequence_last BIGINT NOT NULL CHECK (sequence_last >= sequence_first),
    created_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload_sha256 CHAR(64) NOT NULL,
    raw_payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ingest_messages_device_received_idx
    ON ingest_messages (device_id, received_at DESC);

CREATE TABLE IF NOT EXISTS telemetry_samples (
    message_id TEXT NOT NULL,
    sample_index INTEGER NOT NULL CHECK (sample_index >= 0),
    gateway_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    point_code TEXT NOT NULL,
    measured_at TIMESTAMPTZ NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence >= 0),
    value DOUBLE PRECISION NOT NULL,
    quality TEXT NOT NULL CHECK (quality IN ('good', 'uncertain', 'bad')),
    unit TEXT,
    source TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (measured_at, message_id, sample_index),
    FOREIGN KEY (message_id) REFERENCES ingest_messages(message_id) ON DELETE RESTRICT,
    CHECK (value NOT IN ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION, '-Infinity'::DOUBLE PRECISION))
) PARTITION BY RANGE (measured_at);

CREATE INDEX IF NOT EXISTS telemetry_samples_device_time_idx
    ON telemetry_samples (device_id, measured_at DESC, point_code);

CREATE INDEX IF NOT EXISTS telemetry_samples_time_brin_idx
    ON telemetry_samples USING BRIN (measured_at);

-- Partition DDL is a migrator responsibility, not an ingestor privilege.
-- Keep three historical years and two future years ready around deployment.
DO $$
DECLARE
    month_start DATE := (date_trunc('month', CURRENT_DATE) - INTERVAL '36 months')::DATE;
    final_month DATE := (date_trunc('month', CURRENT_DATE) + INTERVAL '25 months')::DATE;
    partition_name TEXT;
BEGIN
    WHILE month_start < final_month LOOP
        partition_name := format('telemetry_samples_y%sm%s',
            to_char(month_start, 'YYYY'), to_char(month_start, 'MM'));
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF telemetry_samples FOR VALUES FROM (%L) TO (%L)',
            partition_name,
            month_start,
            (month_start + INTERVAL '1 month')::DATE
        );
        month_start := (month_start + INTERVAL '1 month')::DATE;
    END LOOP;
END $$;
