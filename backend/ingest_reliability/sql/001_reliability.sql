CREATE SCHEMA ingest_reliability;
REVOKE ALL ON SCHEMA ingest_reliability FROM PUBLIC;

CREATE TABLE ingest_reliability.schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    structure_sha256 TEXT NOT NULL CHECK (structure_sha256 ~ '^[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE ingest_reliability.facts (
    fact_id UUID PRIMARY KEY,
    topic TEXT NOT NULL CHECK (octet_length(topic) <= 65535),
    topic_sha256 TEXT NOT NULL CHECK (topic_sha256 = encode(sha256(convert_to(topic,'UTF8')),'hex')),
    raw_payload BYTEA NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 = encode(sha256(raw_payload),'hex')),
    payload_bytes INTEGER NOT NULL CHECK (payload_bytes BETWEEN 0 AND 2097152),
    qos SMALLINT NOT NULL CHECK (qos BETWEEN 0 AND 2),
    retain BOOLEAN NOT NULL,
    dup BOOLEAN NOT NULL,
    received_at TIMESTAMPTZ NOT NULL CHECK (isfinite(received_at)),
    validator_version TEXT NOT NULL CHECK (length(validator_version) BETWEEN 1 AND 64),
    status TEXT NOT NULL CHECK (status IN ('stored','quarantined')),
    reason_code TEXT NOT NULL CHECK (reason_code IN (
        'accepted','topic_invalid','payload_too_large','utf8_invalid','json_invalid',
        'json_duplicate_key','contract_invalid','identity_mismatch','idempotency_conflict',
        'qos_not_supported','retained_publish')),
    accepted_message_id TEXT REFERENCES public.ingest_messages(message_id),
    gateway_id TEXT,
    device_id TEXT,
    message_id TEXT,
    UNIQUE(topic_sha256,payload_sha256),
    CHECK (octet_length(raw_payload)=payload_bytes),
    CHECK ((status='stored' AND accepted_message_id IS NOT NULL AND reason_code='accepted'
            AND message_id IS NOT NULL AND accepted_message_id=message_id)
        OR (status='quarantined' AND accepted_message_id IS NULL AND reason_code<>'accepted')),
    CHECK ((gateway_id IS NULL AND device_id IS NULL AND message_id IS NULL)
        OR (gateway_id IS NOT NULL AND device_id IS NOT NULL AND message_id IS NOT NULL
            AND gateway_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
            AND device_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
            AND message_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'))
);
CREATE INDEX facts_identity_time ON ingest_reliability.facts(gateway_id,device_id,received_at DESC);
CREATE INDEX facts_message_id ON ingest_reliability.facts(message_id);

CREATE TABLE ingest_reliability.fact_observations (
    fact_id UUID PRIMARY KEY REFERENCES ingest_reliability.facts(fact_id),
    first_seen TIMESTAMPTZ NOT NULL CHECK (isfinite(first_seen)),
    last_seen TIMESTAMPTZ NOT NULL CHECK (isfinite(last_seen) AND last_seen>=first_seen),
    duplicate_count BIGINT NOT NULL DEFAULT 0 CHECK (duplicate_count>=0),
    transport_anomaly_count BIGINT NOT NULL DEFAULT 0 CHECK (transport_anomaly_count>=0),
    counters_saturated BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE ingest_reliability.cases (
    case_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_id UUID NOT NULL UNIQUE REFERENCES ingest_reliability.facts(fact_id),
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','acknowledged','closed')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version>0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(updated_at))
);

CREATE TABLE ingest_reliability.receipts (
    receipt_id UUID PRIMARY KEY,
    fact_id UUID NOT NULL UNIQUE REFERENCES ingest_reliability.facts(fact_id),
    topic TEXT NOT NULL CHECK (octet_length(topic) BETWEEN 1 AND 400),
    payload BYTEA NOT NULL CHECK (octet_length(payload) BETWEEN 1 AND 4096),
    gateway_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('stored','quarantined')),
    reason_code TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL CHECK (isfinite(recorded_at))
);

CREATE TABLE ingest_reliability.deliveries (
    receipt_id UUID PRIMARY KEY REFERENCES ingest_reliability.receipts(receipt_id),
    requested_generation BIGINT NOT NULL DEFAULT 1 CHECK (requested_generation>0),
    sent_generation BIGINT NOT NULL DEFAULT 0 CHECK (sent_generation>=0 AND sent_generation<=requested_generation),
    lease_token UUID,
    lease_generation BIGINT,
    lease_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(next_attempt_at)),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(requested_at)),
    attempts BIGINT NOT NULL DEFAULT 0 CHECK (attempts>=0),
    last_error_code TEXT CHECK (last_error_code ~ '^[a-z][a-z0-9_.]{0,95}$'),
    broker_acked_at TIMESTAMPTZ CHECK (isfinite(broker_acked_at)),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version>0),
    CHECK ((lease_token IS NULL AND lease_generation IS NULL AND lease_until IS NULL)
        OR (lease_token IS NOT NULL AND lease_generation IS NOT NULL AND lease_until IS NOT NULL
            AND lease_generation>sent_generation AND lease_generation<=requested_generation AND isfinite(lease_until)))
);
CREATE INDEX deliveries_pending ON ingest_reliability.deliveries(next_attempt_at,receipt_id)
    WHERE requested_generation>sent_generation;

CREATE TABLE ingest_reliability.audit_events (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_id UUID NOT NULL REFERENCES ingest_reliability.facts(fact_id),
    case_id UUID REFERENCES ingest_reliability.cases(case_id),
    receipt_id UUID REFERENCES ingest_reliability.receipts(receipt_id),
    actor JSONB NOT NULL CHECK (jsonb_typeof(actor)='object'
        AND actor ?& ARRAY['domain','user_id','username','display_name','role_code']
        AND actor-ARRAY['domain','user_id','username','display_name','role_code']='{}'::jsonb
        AND jsonb_typeof(actor->'domain')='string' AND length(actor->>'domain') BETWEEN 1 AND 128
        AND jsonb_typeof(actor->'user_id')='string' AND length(actor->>'user_id') BETWEEN 1 AND 128
        AND jsonb_typeof(actor->'username')='string' AND length(actor->>'username') BETWEEN 1 AND 128
        AND jsonb_typeof(actor->'display_name')='string' AND length(actor->>'display_name') BETWEEN 1 AND 128
        AND jsonb_typeof(actor->'role_code')='string' AND length(actor->>'role_code') BETWEEN 1 AND 128),
    action TEXT NOT NULL CHECK (action IN ('case.ack','case.close','case.reopen','case.note',
        'case.repeat_reopen','receipt.resend','raw.preview','raw.download')),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 2 AND 500),
    before_data JSONB NOT NULL CHECK (jsonb_typeof(before_data)='object'),
    after_data JSONB NOT NULL CHECK (jsonb_typeof(after_data)='object'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(recorded_at))
);
CREATE INDEX audit_fact_time ON ingest_reliability.audit_events(fact_id,recorded_at DESC);

CREATE TABLE ingest_reliability.operation_receipts (
    actor_domain TEXT NOT NULL CHECK (length(actor_domain) BETWEEN 1 AND 128),
    actor_id TEXT NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 128),
    idempotency_key UUID NOT NULL,
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    result JSONB NOT NULL CHECK (jsonb_typeof(result)='object'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (isfinite(recorded_at)),
    PRIMARY KEY(actor_domain,actor_id,idempotency_key)
);

CREATE TABLE ingest_reliability.worker_health (
    worker_id UUID PRIMARY KEY,
    last_seen TIMESTAMPTZ NOT NULL CHECK (isfinite(last_seen)),
    last_success TIMESTAMPTZ CHECK (isfinite(last_success)),
    last_failure TIMESTAMPTZ CHECK (isfinite(last_failure)),
    error_code TEXT CHECK (error_code ~ '^[a-z][a-z0-9_.]{0,95}$')
);

CREATE TABLE ingest_reliability.capacity (
    singleton SMALLINT PRIMARY KEY CHECK (singleton=1),
    total_bytes BIGINT NOT NULL CHECK (total_bytes>0),
    quarantine_bytes BIGINT NOT NULL CHECK (quarantine_bytes>0 AND quarantine_bytes<=total_bytes),
    used_total_bytes BIGINT NOT NULL DEFAULT 0 CHECK (used_total_bytes BETWEEN 0 AND total_bytes),
    used_quarantine_bytes BIGINT NOT NULL DEFAULT 0 CHECK (used_quarantine_bytes BETWEEN 0 AND quarantine_bytes
                                                        AND used_quarantine_bytes<=used_total_bytes),
    last_clock_seen TIMESTAMPTZ CHECK (isfinite(last_clock_seen))
);

CREATE FUNCTION ingest_reliability.immutable_row() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'immutable ingest evidence' USING ERRCODE='55000';
END $$;

CREATE TRIGGER facts_immutable BEFORE UPDATE OR DELETE ON ingest_reliability.facts
    FOR EACH ROW EXECUTE FUNCTION ingest_reliability.immutable_row();
CREATE TRIGGER receipts_immutable BEFORE UPDATE OR DELETE ON ingest_reliability.receipts
    FOR EACH ROW EXECUTE FUNCTION ingest_reliability.immutable_row();
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON ingest_reliability.audit_events
    FOR EACH ROW EXECUTE FUNCTION ingest_reliability.immutable_row();
CREATE TRIGGER operation_receipts_immutable BEFORE UPDATE OR DELETE ON ingest_reliability.operation_receipts
    FOR EACH ROW EXECUTE FUNCTION ingest_reliability.immutable_row();
CREATE TRIGGER migration_immutable BEFORE UPDATE OR DELETE ON ingest_reliability.schema_migrations
    FOR EACH ROW EXECUTE FUNCTION ingest_reliability.immutable_row();

CREATE FUNCTION ingest_reliability.check_fact_parent() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE parent public.ingest_messages%ROWTYPE;
BEGIN
    IF NEW.status='stored' THEN
        SELECT * INTO parent FROM public.ingest_messages WHERE message_id=NEW.accepted_message_id;
        IF NOT FOUND OR (parent.topic,parent.gateway_id,parent.device_id,parent.message_id,parent.payload_sha256::text)
            IS DISTINCT FROM (NEW.topic,NEW.gateway_id,NEW.device_id,NEW.message_id,NEW.payload_sha256) THEN
            RAISE EXCEPTION 'accepted evidence does not match telemetry parent' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER fact_parent BEFORE INSERT ON ingest_reliability.facts
    FOR EACH ROW EXECUTE FUNCTION ingest_reliability.check_fact_parent();

CREATE FUNCTION ingest_reliability.check_receipt() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE parent ingest_reliability.facts%ROWTYPE; body jsonb; raw_text text; expected jsonb;
BEGIN
    SELECT * INTO parent FROM ingest_reliability.facts WHERE fact_id=NEW.fact_id;
    IF NOT FOUND OR parent.gateway_id IS NULL OR
        (parent.gateway_id,parent.device_id,parent.message_id,parent.payload_sha256,parent.status,parent.reason_code)
        IS DISTINCT FROM (NEW.gateway_id,NEW.device_id,NEW.message_id,NEW.payload_sha256,NEW.status,NEW.reason_code) THEN
        RAISE EXCEPTION 'receipt does not match persisted fact' USING ERRCODE='23514';
    END IF;
    IF NEW.topic IS DISTINCT FROM split_part(parent.topic,'/',1)||'/v2/'||NEW.gateway_id||'/'||NEW.device_id||'/ingest-ack'
        OR octet_length(NEW.payload)>4096 THEN
        RAISE EXCEPTION 'receipt target or size invalid' USING ERRCODE='23514';
    END IF;
    raw_text:=convert_from(NEW.payload,'UTF8');
    IF NOT (raw_text IS JSON OBJECT WITH UNIQUE KEYS) THEN
        RAISE EXCEPTION 'receipt JSON invalid' USING ERRCODE='23514';
    END IF;
    body:=raw_text::jsonb;
    IF EXISTS(SELECT 1 FROM jsonb_each(body) AS items WHERE jsonb_typeof(items.value)<>'string') THEN
        RAISE EXCEPTION 'receipt fields must be strings' USING ERRCODE='23514';
    END IF;
    expected:=jsonb_build_object('schema','OWP-INGEST-ACK/1.0','receipt_id',NEW.receipt_id::text,
        'gateway_id',NEW.gateway_id,'device_id',NEW.device_id,'message_id',NEW.message_id,
        'payload_sha256',NEW.payload_sha256,'status',NEW.status,'reason_code',NEW.reason_code,
        'recorded_at',body->>'recorded_at');
    IF body IS DISTINCT FROM expected OR body->>'recorded_at' IS NULL
        OR (body->>'recorded_at') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}(Z|\+00:00)$'
        OR (body->>'recorded_at')::timestamptz IS DISTINCT FROM NEW.recorded_at THEN
        RAISE EXCEPTION 'receipt body does not match metadata' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER receipt_consistent BEFORE INSERT ON ingest_reliability.receipts
    FOR EACH ROW EXECUTE FUNCTION ingest_reliability.check_receipt();

CREATE FUNCTION ingest_reliability.case_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NOT EXISTS(SELECT 1 FROM ingest_reliability.facts WHERE fact_id=NEW.fact_id AND status='quarantined') THEN
            RAISE EXCEPTION 'case requires quarantine fact' USING ERRCODE='23514';
        END IF;
    ELSIF TG_OP='DELETE' OR (NEW.case_id,NEW.fact_id) IS DISTINCT FROM (OLD.case_id,OLD.fact_id) THEN
        RAISE EXCEPTION 'case identity is immutable' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER case_identity_fixed BEFORE INSERT OR UPDATE OR DELETE ON ingest_reliability.cases
    FOR EACH ROW EXECUTE FUNCTION ingest_reliability.case_identity();

CREATE VIEW ingest_reliability.case_metadata AS
    SELECT c.case_id AS id,c.fact_id,c.state,c.version,c.updated_at,
        f.topic,f.payload_sha256,f.payload_bytes,f.gateway_id,f.device_id,f.message_id,
        f.reason_code,f.validator_version,f.received_at,f.qos,f.retain,f.dup,
        o.first_seen,o.last_seen,o.duplicate_count,o.transport_anomaly_count,o.counters_saturated
    FROM ingest_reliability.cases c JOIN ingest_reliability.facts f USING(fact_id)
    LEFT JOIN ingest_reliability.fact_observations o USING(fact_id);

CREATE VIEW ingest_reliability.receipt_metadata AS
    SELECT r.receipt_id AS id,r.fact_id,r.topic,r.gateway_id,r.device_id,r.message_id,
        r.payload_sha256,r.status,r.reason_code,r.recorded_at,
        d.requested_generation,d.sent_generation,d.lease_until,d.next_attempt_at,d.requested_at,
        d.attempts,d.last_error_code,d.broker_acked_at,d.version
    FROM ingest_reliability.receipts r LEFT JOIN ingest_reliability.deliveries d USING(receipt_id);

CREATE VIEW ingest_reliability.health_metadata AS
    SELECT c.total_bytes,c.quarantine_bytes,c.used_total_bytes,c.used_quarantine_bytes,c.last_clock_seen,
        (SELECT count(*) FROM ingest_reliability.cases WHERE state<>'closed') AS open_cases,
        (SELECT min(f.received_at) FROM ingest_reliability.cases q JOIN ingest_reliability.facts f USING(fact_id)
            WHERE q.state<>'closed') AS oldest_open_at,
        (SELECT count(*) FROM ingest_reliability.deliveries WHERE requested_generation>sent_generation) AS pending_receipts,
        (SELECT min(requested_at) FROM ingest_reliability.deliveries WHERE requested_generation>sent_generation) AS oldest_pending_at,
        (SELECT max(last_seen) FROM ingest_reliability.worker_health) AS publisher_last_seen
    FROM ingest_reliability.capacity c WHERE singleton=1;

CREATE VIEW ingest_reliability.audit_metadata AS
    SELECT audit_id AS id,fact_id,case_id,receipt_id,actor,action,reason,before_data,after_data,recorded_at
    FROM ingest_reliability.audit_events;

REVOKE ALL ON ALL TABLES IN SCHEMA ingest_reliability FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ingest_reliability FROM PUBLIC;
