CREATE SCHEMA waveforms;
REVOKE ALL ON SCHEMA waveforms FROM PUBLIC;

CREATE TABLE waveforms.schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version>0),
    sha256 TEXT NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
    structure_sha256 TEXT NOT NULL CHECK(structure_sha256 ~ '^[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- '*' is the system bucket; named buckets are configured intake gateways.
CREATE TABLE waveforms.capacity (
    bucket TEXT PRIMARY KEY CHECK(bucket='*' OR bucket ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    raw_bytes BIGINT NOT NULL CHECK(raw_bytes>0),
    facts BIGINT NOT NULL CHECK(facts>0),
    attachment_bytes BIGINT NOT NULL CHECK(attachment_bytes>0),
    incomplete_captures BIGINT NOT NULL CHECK(incomplete_captures>0),
    orphan_bytes BIGINT NOT NULL CHECK(orphan_bytes>0 AND orphan_bytes<=raw_bytes),
    quarantine_bytes BIGINT NOT NULL CHECK(quarantine_bytes>0 AND quarantine_bytes<=raw_bytes),
    metadata_rows BIGINT NOT NULL CHECK(metadata_rows>0),
    orphan_bytes_per_capture BIGINT NOT NULL CHECK(orphan_bytes_per_capture>0 AND orphan_bytes_per_capture<=orphan_bytes),
    used_raw_bytes BIGINT NOT NULL DEFAULT 0 CHECK(used_raw_bytes BETWEEN 0 AND raw_bytes),
    reserved_raw_bytes BIGINT NOT NULL DEFAULT 0 CHECK(reserved_raw_bytes BETWEEN 0 AND raw_bytes AND reserved_raw_bytes<=raw_bytes-used_raw_bytes),
    used_facts BIGINT NOT NULL DEFAULT 0 CHECK(used_facts BETWEEN 0 AND facts),
    used_attachment_bytes BIGINT NOT NULL DEFAULT 0 CHECK(used_attachment_bytes BETWEEN 0 AND attachment_bytes),
    used_incomplete_captures BIGINT NOT NULL DEFAULT 0 CHECK(used_incomplete_captures BETWEEN 0 AND incomplete_captures),
    used_orphan_bytes BIGINT NOT NULL DEFAULT 0 CHECK(used_orphan_bytes BETWEEN 0 AND orphan_bytes AND used_orphan_bytes<=used_raw_bytes),
    used_quarantine_bytes BIGINT NOT NULL DEFAULT 0 CHECK(used_quarantine_bytes BETWEEN 0 AND quarantine_bytes AND used_quarantine_bytes<=used_raw_bytes),
    used_metadata_rows BIGINT NOT NULL DEFAULT 0 CHECK(used_metadata_rows BETWEEN 0 AND metadata_rows),
    last_clock_seen TIMESTAMPTZ CHECK(isfinite(last_clock_seen)),
    CHECK(bucket<>'*' OR (used_raw_bytes=0 AND reserved_raw_bytes=0 AND used_facts=0
        AND used_attachment_bytes=0 AND used_incomplete_captures=0 AND used_orphan_bytes=0
        AND used_quarantine_bytes=0 AND used_metadata_rows=0))
);

CREATE TABLE waveforms.facts (
    fact_id UUID PRIMARY KEY,
    intake_gateway_id TEXT NOT NULL REFERENCES waveforms.capacity(bucket) CHECK(intake_gateway_id<>'*'),
    raw_payload BYTEA NOT NULL CHECK(octet_length(raw_payload)<=262144),
    payload_sha256 TEXT NOT NULL CHECK(payload_sha256=encode(sha256(raw_payload),'hex')),
    payload_bytes INTEGER NOT NULL CHECK(payload_bytes=octet_length(raw_payload)),
    billed_bytes INTEGER GENERATED ALWAYS AS (greatest(1024,octet_length(raw_payload))) STORED,
    received_at TIMESTAMPTZ NOT NULL CHECK(isfinite(received_at)),
    UNIQUE(intake_gateway_id,payload_sha256),
    UNIQUE(fact_id,intake_gateway_id)
);

-- Raw identity is transport-independent. A quarantined observation cannot
-- poison a subsequent valid observation of the same exact bytes.
CREATE TABLE waveforms.message_outcomes (
    outcome_id UUID PRIMARY KEY,
    fact_id UUID NOT NULL,
    intake_gateway_id TEXT NOT NULL,
    validator_version TEXT NOT NULL CHECK(length(validator_version) BETWEEN 1 AND 64),
    status TEXT NOT NULL CHECK(status IN ('stored','quarantined')),
    reason_code TEXT NOT NULL CHECK(reason_code ~ '^[a-z][a-z0-9_]{0,95}$'),
    kind TEXT CHECK(kind IN ('manifest','chunk')),
    gateway_id TEXT CHECK(gateway_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    device_id TEXT CHECK(device_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    capture_id UUID,
    CHECK((status='stored' AND reason_code='accepted' AND kind IS NOT NULL AND gateway_id=intake_gateway_id
           AND gateway_id IS NOT NULL AND device_id IS NOT NULL AND capture_id IS NOT NULL)
          OR (status='quarantined' AND reason_code<>'accepted')),
    FOREIGN KEY(fact_id,intake_gateway_id) REFERENCES waveforms.facts(fact_id,intake_gateway_id),
    UNIQUE(fact_id,status,reason_code,validator_version),
    UNIQUE(outcome_id,fact_id,intake_gateway_id)
);

CREATE TABLE waveforms.observations (
    observation_id UUID PRIMARY KEY,
    fact_id UUID NOT NULL,
    outcome_id UUID NOT NULL,
    intake_gateway_id TEXT NOT NULL,
    transport TEXT NOT NULL CHECK(transport IN ('mqtt','file')),
    source_ref UUID NOT NULL,
    topic TEXT CHECK(octet_length(topic)<=65535),
    qos SMALLINT CHECK(qos BETWEEN 0 AND 2), retain BOOLEAN, dup BOOLEAN,
    first_seen TIMESTAMPTZ NOT NULL CHECK(isfinite(first_seen)),
    last_seen TIMESTAMPTZ NOT NULL CHECK(isfinite(last_seen) AND last_seen>=first_seen),
    duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK(duplicate_count>=0),
    anomaly_count INTEGER NOT NULL DEFAULT 0 CHECK(anomaly_count>=0),
    counters_saturated BOOLEAN NOT NULL DEFAULT FALSE,
    FOREIGN KEY(fact_id,intake_gateway_id) REFERENCES waveforms.facts(fact_id,intake_gateway_id),
    FOREIGN KEY(outcome_id,fact_id,intake_gateway_id) REFERENCES waveforms.message_outcomes(outcome_id,fact_id,intake_gateway_id),
    UNIQUE(outcome_id,source_ref),
    CHECK((transport='mqtt' AND topic IS NOT NULL AND qos IS NOT NULL AND retain IS NOT NULL AND dup IS NOT NULL)
          OR (transport='file' AND topic IS NULL AND qos IS NULL AND retain IS NULL AND dup IS NULL))
);

CREATE TABLE waveforms.captures (
    capture_pk UUID PRIMARY KEY,
    intake_gateway_id TEXT NOT NULL REFERENCES waveforms.capacity(bucket) CHECK(intake_gateway_id<>'*'),
    gateway_id TEXT NOT NULL CHECK(gateway_id=intake_gateway_id),
    device_id TEXT NOT NULL CHECK(device_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    capture_id UUID NOT NULL,
    manifest_fact_id UUID REFERENCES waveforms.facts(fact_id),
    manifest_outcome_id UUID,
    manifest_sha256 TEXT CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
    state TEXT NOT NULL DEFAULT 'collecting' CHECK(state IN ('collecting','complete','invalid')),
    first_seen TIMESTAMPTZ NOT NULL CHECK(isfinite(first_seen)),
    last_valid_at TIMESTAMPTZ NOT NULL CHECK(isfinite(last_valid_at) AND last_valid_at>=first_seen),
    reserved_raw_bytes BIGINT NOT NULL DEFAULT 0 CHECK(reserved_raw_bytes BETWEEN 0 AND 33554432),
    used_raw_bytes BIGINT NOT NULL DEFAULT 0 CHECK(used_raw_bytes BETWEEN 0 AND 33554432 AND reserved_raw_bytes<=33554432-used_raw_bytes),
    orphan_bytes BIGINT NOT NULL DEFAULT 0 CHECK(orphan_bytes BETWEEN 0 AND used_raw_bytes),
    version BIGINT NOT NULL DEFAULT 1 CHECK(version>0),
    CHECK((manifest_fact_id IS NULL)=(manifest_sha256 IS NULL)),
    CHECK((manifest_fact_id IS NULL)=(manifest_outcome_id IS NULL)),
    FOREIGN KEY(manifest_outcome_id,manifest_fact_id,intake_gateway_id) REFERENCES waveforms.message_outcomes(outcome_id,fact_id,intake_gateway_id),
    UNIQUE(gateway_id,device_id,capture_id), UNIQUE(capture_pk,intake_gateway_id)
);

CREATE TABLE waveforms.streams (
    capture_pk UUID NOT NULL,
    intake_gateway_id TEXT NOT NULL,
    stream_id TEXT NOT NULL CHECK(stream_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    manifest_fact_id UUID NOT NULL REFERENCES waveforms.facts(fact_id),
    specification JSONB NOT NULL CHECK(jsonb_typeof(specification)='object' AND octet_length(specification::text)<=65536),
    chunk_count INTEGER NOT NULL CHECK(chunk_count BETWEEN 1 AND 512),
    decoded_bytes INTEGER NOT NULL CHECK(decoded_bytes BETWEEN 1 AND 16777216),
    data_sha256 TEXT NOT NULL CHECK(data_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY(capture_pk,stream_id),
    FOREIGN KEY(capture_pk,intake_gateway_id) REFERENCES waveforms.captures(capture_pk,intake_gateway_id)
);

-- Candidate content is immutable. Only verified candidates occupy chunk_slots.
CREATE TABLE waveforms.chunks (
    fact_id UUID PRIMARY KEY,
    outcome_id UUID NOT NULL,
    capture_pk UUID NOT NULL,
    intake_gateway_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
    stream_id TEXT NOT NULL CHECK(stream_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    chunk_index INTEGER NOT NULL CHECK(chunk_index BETWEEN 0 AND 511),
    sample_start BIGINT NOT NULL CHECK(sample_start>=0),
    sample_count INTEGER NOT NULL CHECK(sample_count>0),
    decoded_bytes INTEGER NOT NULL CHECK(decoded_bytes BETWEEN 1 AND 131072),
    data_sha256 TEXT NOT NULL CHECK(data_sha256 ~ '^[0-9a-f]{64}$'),
    FOREIGN KEY(fact_id,intake_gateway_id) REFERENCES waveforms.facts(fact_id,intake_gateway_id),
    FOREIGN KEY(outcome_id,fact_id,intake_gateway_id) REFERENCES waveforms.message_outcomes(outcome_id,fact_id,intake_gateway_id),
    FOREIGN KEY(capture_pk,intake_gateway_id) REFERENCES waveforms.captures(capture_pk,intake_gateway_id),
    UNIQUE(fact_id,capture_pk,stream_id,chunk_index)
);
CREATE INDEX chunk_candidates ON waveforms.chunks(capture_pk,manifest_sha256,stream_id,chunk_index);
CREATE TABLE waveforms.chunk_slots (
    capture_pk UUID NOT NULL,
    intake_gateway_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    fact_id UUID NOT NULL UNIQUE,
    PRIMARY KEY(capture_pk,stream_id,chunk_index),
    FOREIGN KEY(capture_pk,stream_id) REFERENCES waveforms.streams(capture_pk,stream_id),
    FOREIGN KEY(fact_id,capture_pk,stream_id,chunk_index) REFERENCES waveforms.chunks(fact_id,capture_pk,stream_id,chunk_index),
    FOREIGN KEY(capture_pk,intake_gateway_id) REFERENCES waveforms.captures(capture_pk,intake_gateway_id)
);

CREATE TABLE waveforms.completion (
    capture_pk UUID PRIMARY KEY,
    intake_gateway_id TEXT NOT NULL,
    manifest_fact_id UUID NOT NULL REFERENCES waveforms.facts(fact_id),
    status TEXT NOT NULL CHECK(status IN ('complete','invalid')),
    reason_code TEXT NOT NULL CHECK(reason_code ~ '^[a-z][a-z0-9_]{0,95}$'),
    result JSONB NOT NULL CHECK(jsonb_typeof(result)='object' AND octet_length(result::text)<=65536),
    completed_at TIMESTAMPTZ NOT NULL CHECK(isfinite(completed_at)),
    FOREIGN KEY(capture_pk,intake_gateway_id) REFERENCES waveforms.captures(capture_pk,intake_gateway_id)
);

CREATE TABLE waveforms.capture_conflicts (
    conflict_id UUID PRIMARY KEY,
    capture_pk UUID NOT NULL,
    intake_gateway_id TEXT NOT NULL,
    fact_id UUID NOT NULL REFERENCES waveforms.facts(fact_id),
    first_fact_id UUID REFERENCES waveforms.facts(fact_id),
    reason_code TEXT NOT NULL CHECK(reason_code IN ('idempotency_conflict','reference_conflict','layout_invalid','digest_mismatch')),
    recorded_at TIMESTAMPTZ NOT NULL CHECK(isfinite(recorded_at)),
    UNIQUE(capture_pk,fact_id,reason_code),
    FOREIGN KEY(fact_id,intake_gateway_id) REFERENCES waveforms.facts(fact_id,intake_gateway_id),
    FOREIGN KEY(first_fact_id,intake_gateway_id) REFERENCES waveforms.facts(fact_id,intake_gateway_id),
    FOREIGN KEY(capture_pk,intake_gateway_id) REFERENCES waveforms.captures(capture_pk,intake_gateway_id)
);

CREATE TABLE waveforms.assembly_jobs (
    capture_pk UUID PRIMARY KEY,
    intake_gateway_id TEXT NOT NULL,
    requested_generation BIGINT NOT NULL DEFAULT 1 CHECK(requested_generation>0),
    finished_generation BIGINT NOT NULL DEFAULT 0 CHECK(finished_generation BETWEEN 0 AND requested_generation),
    lease_token UUID, lease_generation BIGINT, lease_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(next_attempt_at)),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    last_error_code TEXT CHECK(last_error_code ~ '^[a-z][a-z0-9_.]{0,95}$'),
    version BIGINT NOT NULL DEFAULT 1 CHECK(version>0),
    FOREIGN KEY(capture_pk,intake_gateway_id) REFERENCES waveforms.captures(capture_pk,intake_gateway_id),
    CHECK((lease_token IS NULL AND lease_generation IS NULL AND lease_until IS NULL)
        OR (lease_token IS NOT NULL AND lease_generation IS NOT NULL AND lease_until IS NOT NULL
            AND lease_generation>finished_generation AND lease_generation<=requested_generation AND isfinite(lease_until)))
);

CREATE TABLE waveforms.receipts (
    receipt_id UUID PRIMARY KEY,
    intake_gateway_id TEXT NOT NULL REFERENCES waveforms.capacity(bucket) CHECK(intake_gateway_id<>'*'),
    fact_id UUID REFERENCES waveforms.facts(fact_id),
    outcome_id UUID,
    capture_pk UUID REFERENCES waveforms.captures(capture_pk),
    scope TEXT NOT NULL CHECK(scope IN ('message','capture')),
    topic TEXT NOT NULL CHECK(octet_length(topic) BETWEEN 1 AND 400),
    payload BYTEA NOT NULL CHECK(octet_length(payload) BETWEEN 1 AND 4096),
    recorded_at TIMESTAMPTZ NOT NULL CHECK(isfinite(recorded_at)),
    CHECK((scope='message' AND fact_id IS NOT NULL AND outcome_id IS NOT NULL AND capture_pk IS NULL)
       OR (scope='capture' AND fact_id IS NULL AND outcome_id IS NULL AND capture_pk IS NOT NULL)),
    FOREIGN KEY(outcome_id,fact_id,intake_gateway_id) REFERENCES waveforms.message_outcomes(outcome_id,fact_id,intake_gateway_id),
    FOREIGN KEY(capture_pk,intake_gateway_id) REFERENCES waveforms.captures(capture_pk,intake_gateway_id),
    UNIQUE(outcome_id), UNIQUE(capture_pk), UNIQUE(receipt_id,intake_gateway_id)
);
CREATE TABLE waveforms.deliveries (
    receipt_id UUID PRIMARY KEY,
    intake_gateway_id TEXT NOT NULL,
    requested_generation BIGINT NOT NULL DEFAULT 1 CHECK(requested_generation>0),
    sent_generation BIGINT NOT NULL DEFAULT 0 CHECK(sent_generation BETWEEN 0 AND requested_generation),
    lease_token UUID, lease_generation BIGINT, lease_until TIMESTAMPTZ,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(requested_at)),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(next_attempt_at)),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    last_error_code TEXT CHECK(last_error_code ~ '^[a-z][a-z0-9_.]{0,95}$'),
    broker_acked_at TIMESTAMPTZ CHECK(isfinite(broker_acked_at)),
    version BIGINT NOT NULL DEFAULT 1 CHECK(version>0),
    FOREIGN KEY(receipt_id,intake_gateway_id) REFERENCES waveforms.receipts(receipt_id,intake_gateway_id),
    CHECK((lease_token IS NULL AND lease_generation IS NULL AND lease_until IS NULL)
        OR (lease_token IS NOT NULL AND lease_generation IS NOT NULL AND lease_until IS NOT NULL
            AND lease_generation>sent_generation AND lease_generation<=requested_generation AND isfinite(lease_until)))
);
CREATE INDEX pending_deliveries ON waveforms.deliveries(next_attempt_at,receipt_id) WHERE requested_generation>sent_generation;

CREATE FUNCTION waveforms.valid_actor(actor JSONB) RETURNS BOOLEAN
LANGUAGE SQL IMMUTABLE PARALLEL SAFE SET search_path=pg_catalog,pg_temp AS $$
    SELECT coalesce(jsonb_typeof(actor)='object'
        AND actor ?& ARRAY['domain','user_id','username','role_code']
        AND actor-ARRAY['domain','user_id','username','role_code']='{}'::jsonb
        AND NOT EXISTS (SELECT 1 FROM jsonb_each(CASE WHEN jsonb_typeof(actor)='object' THEN actor ELSE '{}'::jsonb END) e WHERE jsonb_typeof(e.value)<>'string'
            OR length(e.value#>>'{}') NOT BETWEEN 1 AND 128
            OR btrim(e.value#>>'{}')='' OR (e.value#>>'{}') ~ '[[:cntrl:]]'),false)
$$;

CREATE TABLE waveforms.imports (
    import_id UUID PRIMARY KEY,
    intake_gateway_id TEXT NOT NULL REFERENCES waveforms.capacity(bucket) CHECK(intake_gateway_id<>'*'),
    actor JSONB NOT NULL CHECK(waveforms.valid_actor(actor)),
    file_sha256 TEXT NOT NULL CHECK(file_sha256 ~ '^[0-9a-f]{64}$'),
    file_bytes BIGINT NOT NULL CHECK(file_bytes BETWEEN 1 AND 33554945),
    state TEXT NOT NULL CHECK(state IN ('recorded','complete','failed')),
    line_count INTEGER NOT NULL CHECK(line_count BETWEEN 1 AND 513),
    recorded_at TIMESTAMPTZ NOT NULL CHECK(isfinite(recorded_at)),
    UNIQUE(import_id,intake_gateway_id)
);
CREATE TABLE waveforms.import_lines (
    import_id UUID NOT NULL,
    intake_gateway_id TEXT NOT NULL,
    line_number INTEGER NOT NULL CHECK(line_number BETWEEN 1 AND 513),
    fact_id UUID NOT NULL,
    outcome_id UUID NOT NULL,
    PRIMARY KEY(import_id,line_number),
    FOREIGN KEY(import_id,intake_gateway_id) REFERENCES waveforms.imports(import_id,intake_gateway_id),
    FOREIGN KEY(fact_id,intake_gateway_id) REFERENCES waveforms.facts(fact_id,intake_gateway_id),
    FOREIGN KEY(outcome_id,fact_id,intake_gateway_id) REFERENCES waveforms.message_outcomes(outcome_id,fact_id,intake_gateway_id)
);
CREATE TABLE waveforms.worker_health (
    worker_id UUID PRIMARY KEY,
    component TEXT NOT NULL CHECK(component IN ('receiver','assembler','receipt_publisher')),
    gateway_id TEXT NOT NULL REFERENCES waveforms.capacity(bucket) CHECK(gateway_id<>'*'),
    last_seen TIMESTAMPTZ NOT NULL CHECK(isfinite(last_seen)),
    last_success TIMESTAMPTZ CHECK(isfinite(last_success)),
    last_failure TIMESTAMPTZ CHECK(isfinite(last_failure)),
    error_code TEXT CHECK(error_code ~ '^[a-z][a-z0-9_.]{0,95}$'),
    UNIQUE(component,gateway_id)
);
CREATE TABLE waveforms.audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intake_gateway_id TEXT NOT NULL REFERENCES waveforms.capacity(bucket) CHECK(intake_gateway_id<>'*'),
    fact_id UUID REFERENCES waveforms.facts(fact_id),
    actor JSONB NOT NULL CHECK(waveforms.valid_actor(actor)),
    action TEXT NOT NULL CHECK(action ~ '^[a-z][a-z0-9_.]{0,95}$'),
    details JSONB NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(details)='object' AND octet_length(details::text)<=8192),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at)),
    FOREIGN KEY(fact_id,intake_gateway_id) REFERENCES waveforms.facts(fact_id,intake_gateway_id)
);
CREATE TABLE waveforms.operation_receipts (
    intake_gateway_id TEXT NOT NULL REFERENCES waveforms.capacity(bucket) CHECK(intake_gateway_id<>'*'),
    actor_domain TEXT NOT NULL CHECK(length(actor_domain) BETWEEN 1 AND 128 AND btrim(actor_domain)<>'' AND actor_domain !~ '[[:cntrl:]]'),
    actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 128 AND btrim(actor_id)<>'' AND actor_id !~ '[[:cntrl:]]'),
    idempotency_key UUID NOT NULL,
    request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    result JSONB NOT NULL CHECK(jsonb_typeof(result)='object' AND octet_length(result::text)<=8192),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at)),
    PRIMARY KEY(actor_domain,actor_id,idempotency_key)
);
CREATE TABLE waveforms.state_head (
    singleton SMALLINT PRIMARY KEY CHECK(singleton=1),
    version BIGINT NOT NULL DEFAULT 1 CHECK(version>0)
);
INSERT INTO waveforms.state_head(singleton) VALUES(1);

CREATE FUNCTION waveforms.immutable_row() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN RAISE EXCEPTION 'immutable waveform content' USING ERRCODE='55000'; END $$;

CREATE FUNCTION waveforms.charge_metadata() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE gateway TEXT; affected INTEGER;
BEGIN
    gateway := NEW.intake_gateway_id;
    -- Gateway first; global usage is derived from bounded gateway rows.
    UPDATE waveforms.capacity SET used_metadata_rows=used_metadata_rows+1 WHERE bucket=gateway AND bucket<>'*';
    GET DIAGNOSTICS affected=ROW_COUNT;
    IF affected<>1 THEN RAISE EXCEPTION 'unknown intake gateway' USING ERRCODE='23503'; END IF;
    RETURN NEW;
END $$;

DO $$
DECLARE name TEXT;
BEGIN
    FOREACH name IN ARRAY ARRAY['facts','message_outcomes','streams','chunks','chunk_slots','completion','capture_conflicts','receipts','import_lines','audit','operation_receipts','schema_migrations'] LOOP
        EXECUTE format('CREATE TRIGGER immutable_content BEFORE UPDATE OR DELETE ON waveforms.%I FOR EACH ROW EXECUTE FUNCTION waveforms.immutable_row()',name);
    END LOOP;
    FOREACH name IN ARRAY ARRAY['message_outcomes','observations','captures','streams','chunks','chunk_slots','completion','capture_conflicts','assembly_jobs','receipts','deliveries','imports','import_lines','audit','operation_receipts'] LOOP
        EXECUTE format('CREATE TRIGGER bounded_metadata AFTER INSERT ON waveforms.%I FOR EACH ROW EXECUTE FUNCTION waveforms.charge_metadata()',name);
    END LOOP;
END $$;

CREATE FUNCTION waveforms.capture_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF TG_OP='UPDATE' AND ((NEW.capture_pk,NEW.intake_gateway_id,NEW.gateway_id,NEW.device_id,NEW.capture_id,NEW.first_seen)
        IS DISTINCT FROM (OLD.capture_pk,OLD.intake_gateway_id,OLD.gateway_id,OLD.device_id,OLD.capture_id,OLD.first_seen)
        OR (OLD.manifest_fact_id IS NOT NULL AND (NEW.manifest_fact_id,NEW.manifest_outcome_id,NEW.manifest_sha256) IS DISTINCT FROM (OLD.manifest_fact_id,OLD.manifest_outcome_id,OLD.manifest_sha256))
        OR (OLD.state<>'collecting' AND NEW.state<>OLD.state)) THEN
        RAISE EXCEPTION 'capture identity or terminal state is immutable' USING ERRCODE='55000';
    END IF;
    IF NEW.manifest_fact_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM waveforms.facts f JOIN waveforms.message_outcomes o USING(fact_id)
        WHERE f.fact_id=NEW.manifest_fact_id AND o.outcome_id=NEW.manifest_outcome_id AND f.payload_bytes<=65536
        AND (o.intake_gateway_id,o.gateway_id,o.device_id,o.capture_id,f.payload_sha256,o.kind,o.status)=
            (NEW.intake_gateway_id,NEW.gateway_id,NEW.device_id,NEW.capture_id,NEW.manifest_sha256,'manifest','stored')) THEN
        RAISE EXCEPTION 'manifest identity mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER capture_identity BEFORE INSERT OR UPDATE ON waveforms.captures FOR EACH ROW EXECUTE FUNCTION waveforms.capture_identity();

CREATE FUNCTION waveforms.index_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE capture waveforms.captures%ROWTYPE;
BEGIN
    SELECT * INTO capture FROM waveforms.captures WHERE capture_pk=NEW.capture_pk;
    IF NOT FOUND THEN RAISE EXCEPTION 'missing capture' USING ERRCODE='23503'; END IF;
    IF TG_TABLE_NAME='chunks' THEN
        IF NOT EXISTS(SELECT 1 FROM waveforms.message_outcomes o WHERE o.fact_id=NEW.fact_id AND o.outcome_id=NEW.outcome_id AND
            (o.intake_gateway_id,o.gateway_id,o.device_id,o.capture_id,o.kind,o.status)=
            (capture.intake_gateway_id,capture.gateway_id,capture.device_id,capture.capture_id,'chunk','stored')) THEN
            RAISE EXCEPTION 'chunk fact identity mismatch' USING ERRCODE='23514';
        END IF;
    ELSIF TG_TABLE_NAME='chunk_slots' THEN
        IF NOT EXISTS(SELECT 1 FROM waveforms.chunks c JOIN waveforms.streams s
            ON s.capture_pk=c.capture_pk AND s.stream_id=c.stream_id
            WHERE c.fact_id=NEW.fact_id AND c.manifest_sha256=capture.manifest_sha256
                AND c.chunk_index<s.chunk_count) THEN
            RAISE EXCEPTION 'slot must reference accepted manifest and declared range' USING ERRCODE='23514';
        END IF;
    ELSE
        IF NEW.manifest_fact_id IS DISTINCT FROM capture.manifest_fact_id THEN
            RAISE EXCEPTION 'index must reference accepted manifest' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER index_identity BEFORE INSERT ON waveforms.streams FOR EACH ROW EXECUTE FUNCTION waveforms.index_identity();
CREATE TRIGGER index_identity BEFORE INSERT ON waveforms.chunks FOR EACH ROW EXECUTE FUNCTION waveforms.index_identity();
CREATE TRIGGER index_identity BEFORE INSERT ON waveforms.chunk_slots FOR EACH ROW EXECUTE FUNCTION waveforms.index_identity();
CREATE TRIGGER index_identity BEFORE INSERT ON waveforms.completion FOR EACH ROW EXECUTE FUNCTION waveforms.index_identity();

CREATE FUNCTION waveforms.audited_raw(p_fact UUID,p_actor JSONB) RETURNS BYTEA
LANGUAGE plpgsql SECURITY DEFINER VOLATILE PARALLEL UNSAFE SET search_path=pg_catalog,pg_temp AS $$
DECLARE payload BYTEA; gateway TEXT;
BEGIN
    IF p_fact IS NULL OR NOT waveforms.valid_actor(p_actor) THEN
        RAISE EXCEPTION 'invalid raw access arguments' USING ERRCODE='23514';
    END IF;
    SELECT raw_payload,intake_gateway_id INTO payload,gateway FROM waveforms.facts WHERE fact_id=p_fact;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown waveform fact' USING ERRCODE='P0002'; END IF;
    IF octet_length(payload)>262144 THEN RAISE EXCEPTION 'raw fact exceeds bound' USING ERRCODE='23514'; END IF;
    INSERT INTO waveforms.audit(intake_gateway_id,fact_id,actor,action) VALUES(gateway,p_fact,p_actor,'raw.read');
    RETURN payload;
END $$;

CREATE VIEW waveforms.fact_metadata AS SELECT fact_id,intake_gateway_id,payload_sha256,payload_bytes,billed_bytes,received_at FROM waveforms.facts;
CREATE VIEW waveforms.outcome_metadata AS SELECT * FROM waveforms.message_outcomes;
CREATE VIEW waveforms.capture_metadata AS SELECT c.*,
    (c.state='collecting' AND c.last_valid_at<CURRENT_TIMESTAMP-INTERVAL '24 hours') AS stalled,
    EXISTS(SELECT 1 FROM waveforms.capture_conflicts e WHERE e.capture_pk=c.capture_pk) AS has_conflicts FROM waveforms.captures c;
CREATE VIEW waveforms.receipt_metadata AS SELECT receipt_id,intake_gateway_id,fact_id,outcome_id,capture_pk,scope,topic,recorded_at FROM waveforms.receipts;
CREATE VIEW waveforms.audit_metadata AS SELECT * FROM waveforms.audit;
CREATE VIEW waveforms.capacity_metadata AS SELECT c.bucket,c.raw_bytes,c.facts,c.attachment_bytes,
    c.incomplete_captures,c.orphan_bytes,c.quarantine_bytes,c.metadata_rows,c.orphan_bytes_per_capture,
    CASE WHEN c.bucket='*' THEN sums.used_raw_bytes ELSE c.used_raw_bytes END AS used_raw_bytes,
    CASE WHEN c.bucket='*' THEN sums.reserved_raw_bytes ELSE c.reserved_raw_bytes END AS reserved_raw_bytes,
    CASE WHEN c.bucket='*' THEN sums.used_facts ELSE c.used_facts END AS used_facts,
    CASE WHEN c.bucket='*' THEN sums.used_attachment_bytes ELSE c.used_attachment_bytes END AS used_attachment_bytes,
    CASE WHEN c.bucket='*' THEN sums.used_incomplete_captures ELSE c.used_incomplete_captures END AS used_incomplete_captures,
    CASE WHEN c.bucket='*' THEN sums.used_orphan_bytes ELSE c.used_orphan_bytes END AS used_orphan_bytes,
    CASE WHEN c.bucket='*' THEN sums.used_quarantine_bytes ELSE c.used_quarantine_bytes END AS used_quarantine_bytes,
    CASE WHEN c.bucket='*' THEN sums.used_metadata_rows ELSE c.used_metadata_rows END AS used_metadata_rows,
    c.last_clock_seen
    FROM waveforms.capacity c CROSS JOIN (SELECT coalesce(sum(used_raw_bytes),0)::bigint AS used_raw_bytes,
        coalesce(sum(reserved_raw_bytes),0)::bigint AS reserved_raw_bytes,coalesce(sum(used_facts),0)::bigint AS used_facts,
        coalesce(sum(used_attachment_bytes),0)::bigint AS used_attachment_bytes,
        coalesce(sum(used_incomplete_captures),0)::bigint AS used_incomplete_captures,
        coalesce(sum(used_orphan_bytes),0)::bigint AS used_orphan_bytes,
        coalesce(sum(used_quarantine_bytes),0)::bigint AS used_quarantine_bytes,
        coalesce(sum(used_metadata_rows),0)::bigint AS used_metadata_rows FROM waveforms.capacity WHERE bucket<>'*') sums;
CREATE VIEW waveforms.worker_metadata AS SELECT * FROM waveforms.worker_health;

REVOKE ALL ON ALL TABLES IN SCHEMA waveforms FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA waveforms FROM PUBLIC;
