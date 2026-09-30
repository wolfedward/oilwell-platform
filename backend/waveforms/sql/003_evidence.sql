-- Explicit offline migration. Historical base grants must already exist.
CREATE TABLE waveforms.business_settings (
    singleton SMALLINT PRIMARY KEY CHECK(singleton=1),
    accounting_bucket TEXT NOT NULL UNIQUE REFERENCES waveforms.capacity(bucket) CHECK(accounting_bucket<>'*')
);
CREATE TABLE waveforms.attachments (
    attachment_id UUID PRIMARY KEY,
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    media_type TEXT NOT NULL CHECK(media_type IN ('application/pdf','image/png','image/jpeg')),
    payload BYTEA NOT NULL CHECK(octet_length(payload) BETWEEN 1 AND 4194304),
    payload_bytes INTEGER NOT NULL CHECK(payload_bytes=octet_length(payload)),
    payload_sha256 TEXT NOT NULL CHECK(payload_sha256=encode(sha256(payload),'hex')),
    creator JSONB NOT NULL CHECK(waveforms.valid_actor(creator)),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at))
);
CREATE TABLE waveforms.evidence_versions (
    evidence_id UUID PRIMARY KEY,
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    kind TEXT NOT NULL CHECK(kind IN ('sampling_config','calibration','sync','origin','profile')),
    canonical_content BYTEA NOT NULL CHECK(octet_length(canonical_content) BETWEEN 1 AND 65536),
    content_sha256 TEXT NOT NULL CHECK(content_sha256=encode(sha256(canonical_content),'hex')),
    body JSONB NOT NULL CHECK(jsonb_typeof(body)='object' AND octet_length(body::text)<=131072),
    creator JSONB NOT NULL CHECK(waveforms.valid_actor(creator)),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at)),
    CHECK(convert_from(canonical_content,'UTF8')::jsonb=jsonb_build_object('protocol','waveform.evidence-content/1','kind',kind,'body',body)),
    UNIQUE(evidence_id,content_sha256)
);
CREATE TABLE waveforms.evidence_events (
    event_id UUID NOT NULL UNIQUE,
    evidence_id UUID NOT NULL REFERENCES waveforms.evidence_versions(evidence_id),
    version BIGINT NOT NULL CHECK(version>0),
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    state TEXT NOT NULL CHECK(state IN ('draft','approved','rejected','revoked')),
    actor JSONB NOT NULL CHECK(waveforms.valid_actor(actor)),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 512 AND btrim(reason)<>'' AND reason !~ '[[:cntrl:]]'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at)),
    PRIMARY KEY(evidence_id,version)
);
CREATE TABLE waveforms.restrictions (
    restriction_id UUID PRIMARY KEY,
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    evidence_id UUID NOT NULL REFERENCES waveforms.evidence_versions(evidence_id),
    source_event_id UUID NOT NULL UNIQUE REFERENCES waveforms.evidence_events(event_id),
    reason_code TEXT NOT NULL CHECK(reason_code='evidence_revoked'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at))
);
CREATE TABLE waveforms.business_audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    actor JSONB NOT NULL CHECK(waveforms.valid_actor(actor)),
    action TEXT NOT NULL CHECK(action ~ '^[a-z][a-z0-9_.]{0,95}$'),
    subject_id UUID NOT NULL,
    details JSONB NOT NULL CHECK(jsonb_typeof(details)='object' AND octet_length(details::text)<=8192),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at))
);
CREATE TABLE waveforms.business_receipts (
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    actor_domain TEXT NOT NULL CHECK(length(actor_domain) BETWEEN 1 AND 128 AND btrim(actor_domain)<>'' AND actor_domain !~ '[[:cntrl:]]'),
    actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 128 AND btrim(actor_id)<>'' AND actor_id !~ '[[:cntrl:]]'),
    idempotency_key UUID NOT NULL,
    request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    result JSONB NOT NULL CHECK(jsonb_typeof(result)='object' AND octet_length(result::text)<=8192),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at)),
    PRIMARY KEY(actor_domain,actor_id,idempotency_key)
);
CREATE TABLE waveforms.quality_runs (
    quality_run_id UUID PRIMARY KEY,
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    capture_pk UUID REFERENCES waveforms.captures(capture_pk),
    source_key JSONB NOT NULL CHECK(jsonb_typeof(source_key)='object' AND source_key ?& ARRAY['gateway_id','device_id','capture_id'] AND source_key-ARRAY['gateway_id','device_id','capture_id']='{}'::jsonb AND octet_length(source_key::text)<=1024),
    manifest_sha256 TEXT CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
    validator_version TEXT NOT NULL CHECK(length(validator_version) BETWEEN 1 AND 128),
    profile_id UUID, profile_sha256 TEXT CHECK(profile_sha256 ~ '^[0-9a-f]{64}$'),
    purpose TEXT NOT NULL CHECK(purpose IN ('raw_archive','software_pair_test','field_pair_input')),
    status TEXT NOT NULL CHECK(status IN ('eligible','ineligible','pending')),
    parameters JSONB NOT NULL CHECK(jsonb_typeof(parameters)='object' AND octet_length(parameters::text)<=65536),
    evidence JSONB NOT NULL CHECK(jsonb_typeof(evidence)='array' AND octet_length(evidence::text)<=65536),
    registry_snapshot JSONB CHECK(jsonb_typeof(registry_snapshot)='object' AND octet_length(registry_snapshot::text)<=65536),
    result JSONB NOT NULL CHECK(jsonb_typeof(result)='object' AND octet_length(result::text)<=65536),
    creator JSONB NOT NULL CHECK(waveforms.valid_actor(creator)),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at)),
    CHECK((profile_id IS NULL)=(profile_sha256 IS NULL)),
    CHECK(status<>'eligible' OR (capture_pk IS NOT NULL AND manifest_sha256 IS NOT NULL AND profile_id IS NOT NULL)),
    FOREIGN KEY(profile_id,profile_sha256) REFERENCES waveforms.evidence_versions(evidence_id,content_sha256)
);
CREATE TABLE waveforms.pair_versions (
    pair_version_id UUID PRIMARY KEY,
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    left_quality_run_id UUID NOT NULL REFERENCES waveforms.quality_runs(quality_run_id),
    right_quality_run_id UUID NOT NULL REFERENCES waveforms.quality_runs(quality_run_id),
    method_version TEXT NOT NULL CHECK(length(method_version) BETWEEN 1 AND 128),
    body JSONB NOT NULL CHECK(jsonb_typeof(body)='object' AND octet_length(body::text)<=65536),
    content_sha256 TEXT NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
    creator JSONB NOT NULL CHECK(waveforms.valid_actor(creator)),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at))
);
CREATE TABLE waveforms.pair_reviews (
    pair_version_id UUID NOT NULL REFERENCES waveforms.pair_versions(pair_version_id),
    version BIGINT NOT NULL CHECK(version>=2),
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    decision TEXT NOT NULL CHECK(decision IN ('approve','reject','revoke')),
    actor JSONB NOT NULL CHECK(waveforms.valid_actor(actor)),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 512 AND btrim(reason)<>'' AND reason !~ '[[:cntrl:]]'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at)),
    PRIMARY KEY(pair_version_id,version)
);

CREATE FUNCTION waveforms.charge_business() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF NEW.accounting_bucket IS DISTINCT FROM (SELECT accounting_bucket FROM waveforms.business_settings WHERE singleton=1) THEN
        RAISE EXCEPTION 'business accounting mismatch' USING ERRCODE='23514';
    END IF;
    IF TG_TABLE_NAME='attachments' THEN
        UPDATE waveforms.capacity SET used_metadata_rows=used_metadata_rows+1,
            used_attachment_bytes=used_attachment_bytes+octet_length(NEW.payload) WHERE bucket=NEW.accounting_bucket;
    ELSE
        UPDATE waveforms.capacity SET used_metadata_rows=used_metadata_rows+1 WHERE bucket=NEW.accounting_bucket;
    END IF;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown business payer' USING ERRCODE='23503'; END IF;
    RETURN NEW;
END $$;
CREATE FUNCTION waveforms.evidence_event_identity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE previous waveforms.evidence_events; creator JSONB;
BEGIN
    SELECT e.creator INTO creator FROM waveforms.evidence_versions e WHERE e.evidence_id=NEW.evidence_id FOR UPDATE;
    SELECT * INTO previous FROM waveforms.evidence_events e WHERE e.evidence_id=NEW.evidence_id ORDER BY version DESC LIMIT 1;
    IF previous.event_id IS NULL THEN
        IF NEW.version<>1 OR NEW.state<>'draft' OR NEW.actor<>creator THEN RAISE EXCEPTION 'invalid evidence creation' USING ERRCODE='23514'; END IF;
    ELSE
        IF NEW.version<>previous.version+1 OR previous.state='revoked' OR NEW.state='draft'
            OR (NEW.state IN ('approved','rejected') AND (previous.state<>'draft' OR (NEW.actor->>'domain',NEW.actor->>'user_id')=(creator->>'domain',creator->>'user_id'))) THEN
            RAISE EXCEPTION 'invalid evidence transition or self review' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER evidence_event_identity BEFORE INSERT ON waveforms.evidence_events FOR EACH ROW EXECUTE FUNCTION waveforms.evidence_event_identity();

CREATE FUNCTION waveforms.evidence_identity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE item TEXT; configuration waveforms.evidence_versions;
BEGIN
    IF jsonb_typeof(NEW.body->'attachment_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(NEW.body->'attachment_ids')>8 THEN
        RAISE EXCEPTION 'invalid attachments' USING ERRCODE='23514';
    END IF;
    FOR item IN SELECT jsonb_array_elements_text(NEW.body->'attachment_ids') LOOP
        IF NOT EXISTS(SELECT 1 FROM waveforms.attachments a WHERE a.attachment_id=item::uuid AND a.accounting_bucket=NEW.accounting_bucket) THEN
            RAISE EXCEPTION 'unknown controlled attachment' USING ERRCODE='23503';
        END IF;
    END LOOP;
    IF NEW.kind='calibration' THEN
        SELECT * INTO configuration FROM waveforms.evidence_versions WHERE evidence_id=(NEW.body->>'sampling_config_version_id')::uuid;
        IF configuration.kind IS DISTINCT FROM 'sampling_config' OR configuration.content_sha256 IS DISTINCT FROM NEW.body->>'sampling_config_sha256'
            OR configuration.body->'endpoint' IS DISTINCT FROM NEW.body->'endpoint' THEN
            RAISE EXCEPTION 'calibration config identity mismatch' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER evidence_identity BEFORE INSERT ON waveforms.evidence_versions FOR EACH ROW EXECUTE FUNCTION waveforms.evidence_identity();

CREATE FUNCTION waveforms.quality_identity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE capture waveforms.captures;
BEGIN
    IF NEW.capture_pk IS NOT NULL THEN
        SELECT * INTO capture FROM waveforms.captures WHERE capture_pk=NEW.capture_pk;
        IF NEW.source_key IS DISTINCT FROM jsonb_build_object('gateway_id',capture.gateway_id,'device_id',capture.device_id,'capture_id',capture.capture_id::text)
            OR NEW.manifest_sha256 IS DISTINCT FROM capture.manifest_sha256 THEN
            RAISE EXCEPTION 'quality source mismatch' USING ERRCODE='23514';
        END IF;
    END IF;
    IF NEW.profile_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM waveforms.evidence_versions e WHERE e.evidence_id=NEW.profile_id AND e.kind='profile' AND e.body->>'purpose'=NEW.purpose) THEN
        RAISE EXCEPTION 'quality profile mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER quality_identity BEFORE INSERT ON waveforms.quality_runs FOR EACH ROW EXECUTE FUNCTION waveforms.quality_identity();

CREATE FUNCTION waveforms.lock_business_capacity(p_bucket TEXT) RETURNS SETOF waveforms.capacity
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF p_bucket IS DISTINCT FROM (SELECT accounting_bucket FROM waveforms.business_settings WHERE singleton=1) THEN
        RAISE EXCEPTION 'business payer mismatch' USING ERRCODE='23514';
    END IF;
    RETURN QUERY SELECT * FROM waveforms.capacity WHERE bucket=p_bucket FOR UPDATE;
END $$;
CREATE FUNCTION waveforms.lock_business_captures(p_ids UUID[]) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF p_ids IS NULL OR cardinality(p_ids)>32 OR array_position(p_ids,NULL) IS NOT NULL THEN
        RAISE EXCEPTION 'invalid capture selection' USING ERRCODE='23514';
    END IF;
    PERFORM capture_pk FROM waveforms.captures WHERE capture_pk=ANY(p_ids) ORDER BY gateway_id,device_id,capture_id FOR UPDATE;
END $$;
CREATE FUNCTION waveforms.audited_attachment(p_id UUID,p_actor JSONB,p_bucket TEXT) RETURNS BYTEA
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE content BYTEA;
BEGIN
    IF p_id IS NULL OR NOT waveforms.valid_actor(p_actor) THEN RAISE EXCEPTION 'invalid attachment access' USING ERRCODE='23514'; END IF;
    PERFORM pg_advisory_xact_lock(-5633072428019381124::bigint);
    PERFORM waveforms.lock_business_capacity(p_bucket);
    SELECT payload INTO content FROM waveforms.attachments WHERE attachment_id=p_id AND accounting_bucket=p_bucket;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown attachment' USING ERRCODE='P0002'; END IF;
    INSERT INTO waveforms.business_audit(accounting_bucket,actor,action,subject_id,details) VALUES(p_bucket,p_actor,'attachment.read',p_id,'{}');
    RETURN content;
END $$;

DO $$ DECLARE name TEXT; BEGIN
    FOREACH name IN ARRAY ARRAY['business_settings','attachments','evidence_versions','evidence_events','restrictions','business_audit','business_receipts','quality_runs','pair_versions','pair_reviews'] LOOP
        EXECUTE format('CREATE TRIGGER immutable_content BEFORE UPDATE OR DELETE ON waveforms.%I FOR EACH ROW EXECUTE FUNCTION waveforms.immutable_row()',name);
        IF name<>'business_settings' THEN
            EXECUTE format('CREATE TRIGGER bounded_business_metadata AFTER INSERT ON waveforms.%I FOR EACH ROW EXECUTE FUNCTION waveforms.charge_business()',name);
        END IF;
    END LOOP;
END $$;

CREATE VIEW waveforms.attachment_metadata AS SELECT attachment_id,accounting_bucket,media_type,payload_bytes,payload_sha256,creator,recorded_at FROM waveforms.attachments;
CREATE VIEW waveforms.provenance_metadata AS SELECT o.observation_id,o.fact_id,o.outcome_id,o.intake_gateway_id,o.transport,o.source_ref,
    m.kind,m.status,m.reason_code,m.gateway_id,m.device_id,m.capture_id,f.payload_sha256,
    i.import_id,i.state AS import_state,l.line_number
    FROM waveforms.observations o JOIN waveforms.message_outcomes m ON m.outcome_id=o.outcome_id
    JOIN waveforms.facts f ON f.fact_id=o.fact_id
    LEFT JOIN waveforms.imports i ON o.transport='file' AND i.import_id=o.source_ref
    LEFT JOIN waveforms.import_lines l ON l.import_id=i.import_id AND l.fact_id=o.fact_id AND l.outcome_id=o.outcome_id;
CREATE VIEW waveforms.capture_restrictions AS SELECT conflict_id AS restriction_id,capture_pk AS subject_id,
    'capture'::text AS subject_kind,'capture_conflict'::text AS reason_code,recorded_at,conflict_id AS source_event_id FROM waveforms.capture_conflicts;
CREATE VIEW waveforms.registry_devices AS SELECT id,gateway_id,device_id,created_at FROM device_registry.devices;
CREATE VIEW waveforms.registry_device_states AS SELECT id,device_id,status,valid_from,valid_to,audit_event_id FROM device_registry.device_state_periods;
CREATE VIEW waveforms.registry_well_bindings AS SELECT id,device_id,well_id,valid_from,valid_to,audit_event_id FROM device_registry.well_bindings;
CREATE VIEW waveforms.registry_wells AS SELECT id,area_code,well_code,created_at FROM device_registry.wells;
CREATE VIEW waveforms.registry_head AS SELECT singleton,revision FROM device_registry.registry_head;

REVOKE ALL ON waveforms.business_settings,waveforms.attachments,waveforms.evidence_versions,waveforms.evidence_events,
    waveforms.restrictions,waveforms.business_audit,waveforms.business_receipts,waveforms.quality_runs,waveforms.pair_versions,waveforms.pair_reviews,
    waveforms.attachment_metadata,waveforms.provenance_metadata,waveforms.capture_restrictions,
    waveforms.registry_devices,waveforms.registry_device_states,waveforms.registry_well_bindings,waveforms.registry_wells,waveforms.registry_head FROM PUBLIC;
REVOKE ALL ON FUNCTION waveforms.charge_business(),waveforms.evidence_event_identity(),waveforms.evidence_identity(),
    waveforms.quality_identity(),waveforms.lock_business_capacity(TEXT),waveforms.lock_business_captures(UUID[]),
    waveforms.audited_attachment(UUID,JSONB,TEXT) FROM PUBLIC;
