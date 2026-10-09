-- Additive immutable dataset publication and actor-bound export identities.
CREATE TABLE waveforms.dataset_drafts (
    dataset_id UUID PRIMARY KEY,
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    creator JSONB NOT NULL CHECK(waveforms.valid_actor(creator)),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at))
);
CREATE TABLE waveforms.dataset_revisions (
    dataset_id UUID NOT NULL REFERENCES waveforms.dataset_drafts(dataset_id),
    revision BIGINT NOT NULL CHECK(revision>=1),
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    state TEXT NOT NULL CHECK(state IN ('draft','submitted','frozen')),
    body JSONB NOT NULL CHECK(jsonb_typeof(body)='object' AND octet_length(body::text)<=524288),
    actor JSONB NOT NULL CHECK(waveforms.valid_actor(actor)),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at)),
    PRIMARY KEY(dataset_id,revision),
    CHECK(jsonb_typeof(body->'members')='array' AND jsonb_array_length(body->'members')<=1000),
    CHECK(body->>'purpose' IN ('raw_archive','software_pair_test','field_pair_input'))
);
CREATE TABLE waveforms.dataset_versions (
    version_id UUID PRIMARY KEY,
    dataset_id UUID NOT NULL UNIQUE REFERENCES waveforms.dataset_drafts(dataset_id),
    draft_revision BIGINT NOT NULL,
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    purpose TEXT NOT NULL CHECK(purpose IN ('raw_archive','software_pair_test','field_pair_input')),
    metadata JSONB NOT NULL CHECK(jsonb_typeof(metadata)='object' AND octet_length(metadata::text)<=16384),
    content_sha256 TEXT NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
    manifest_bytes INTEGER NOT NULL CHECK(manifest_bytes BETWEEN 1 AND 268500992),
    member_count INTEGER NOT NULL CHECK(member_count BETWEEN 1 AND 1000),
    file_count INTEGER NOT NULL CHECK(file_count BETWEEN 1 AND 2000),
    chunk_count INTEGER NOT NULL CHECK(chunk_count BETWEEN 1 AND 4097),
    reviewer JSONB NOT NULL CHECK(waveforms.valid_actor(reviewer)),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at)),
    FOREIGN KEY(dataset_id,draft_revision) REFERENCES waveforms.dataset_revisions(dataset_id,revision)
);
CREATE TABLE waveforms.dataset_events (
    event_id UUID PRIMARY KEY,
    dataset_id UUID NOT NULL,
    revision BIGINT NOT NULL,
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    state TEXT NOT NULL CHECK(state IN ('draft','submitted','frozen')),
    version_id UUID REFERENCES waveforms.dataset_versions(version_id),
    actor JSONB NOT NULL CHECK(waveforms.valid_actor(actor)),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 512 AND btrim(reason)<>'' AND reason !~ '[[:cntrl:]]'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at)),
    UNIQUE(dataset_id,revision),
    FOREIGN KEY(dataset_id,revision) REFERENCES waveforms.dataset_revisions(dataset_id,revision),
    CHECK((state='frozen')=(version_id IS NOT NULL))
);
CREATE TABLE waveforms.dataset_members (
    version_id UUID NOT NULL REFERENCES waveforms.dataset_versions(version_id),
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 999),
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    kind TEXT NOT NULL CHECK(kind IN ('capture','pair')),
    reference_id UUID NOT NULL,
    quality_run_id UUID NOT NULL REFERENCES waveforms.quality_runs(quality_run_id),
    split TEXT NOT NULL CHECK(split IN ('train','validation','test','unassigned')),
    snapshot JSONB NOT NULL CHECK(jsonb_typeof(snapshot)='object' AND octet_length(snapshot::text)<=262144),
    PRIMARY KEY(version_id,ordinal),
    UNIQUE(version_id,kind,reference_id)
);
CREATE TABLE waveforms.dataset_files (
    version_id UUID NOT NULL REFERENCES waveforms.dataset_versions(version_id),
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 1999),
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    capture_pk UUID NOT NULL REFERENCES waveforms.captures(capture_pk),
    descriptor JSONB NOT NULL CHECK(jsonb_typeof(descriptor)='object' AND octet_length(descriptor::text)<=8192),
    plan JSONB NOT NULL CHECK(jsonb_typeof(plan)='array' AND jsonb_array_length(plan) BETWEEN 1 AND 513 AND octet_length(plan::text)<=131072),
    PRIMARY KEY(version_id,ordinal),
    UNIQUE(version_id,capture_pk)
);
CREATE TABLE waveforms.dataset_manifest_chunks (
    version_id UUID NOT NULL REFERENCES waveforms.dataset_versions(version_id),
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 4096),
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    payload BYTEA NOT NULL CHECK(octet_length(payload) BETWEEN 1 AND 65536),
    PRIMARY KEY(version_id,ordinal)
);
CREATE TABLE waveforms.dataset_exports (
    export_id UUID PRIMARY KEY,
    version_id UUID NOT NULL REFERENCES waveforms.dataset_versions(version_id),
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    intent TEXT NOT NULL CHECK(intent IN ('research','training','evidence')),
    actor JSONB NOT NULL CHECK(waveforms.valid_actor(actor)),
    content_sha256 TEXT NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at))
);

CREATE FUNCTION waveforms.dataset_identity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE prior waveforms.dataset_revisions; draft waveforms.dataset_drafts;
    frozen waveforms.dataset_versions; revision_row waveforms.dataset_revisions; cert waveforms.quality_runs;
BEGIN
    IF TG_TABLE_NAME='dataset_revisions' THEN
        SELECT * INTO draft FROM waveforms.dataset_drafts WHERE dataset_id=NEW.dataset_id FOR UPDATE;
        SELECT * INTO prior FROM waveforms.dataset_revisions WHERE dataset_id=NEW.dataset_id ORDER BY revision DESC LIMIT 1;
        IF prior.revision IS NULL THEN
            IF NEW.revision<>1 OR NEW.state<>'draft' OR NEW.actor<>draft.creator THEN RAISE EXCEPTION 'invalid dataset creation' USING ERRCODE='23514'; END IF;
        ELSE
            IF NEW.revision<>prior.revision+1 OR prior.state='frozen'
                OR (prior.state='submitted' AND (NEW.state<>'frozen' OR NEW.body<>prior.body))
                OR (prior.state='draft' AND NEW.state='frozen') THEN
                RAISE EXCEPTION 'invalid dataset transition' USING ERRCODE='23514';
            END IF;
            IF NEW.state='submitted' AND (NEW.body<>prior.body OR jsonb_array_length(NEW.body->'members')=0) THEN
                RAISE EXCEPTION 'submission differs from draft' USING ERRCODE='23514';
            END IF;
            IF NEW.state='frozen' THEN
                SELECT * INTO frozen FROM waveforms.dataset_versions WHERE dataset_id=NEW.dataset_id;
                IF frozen.version_id IS NULL OR frozen.draft_revision<>prior.revision OR frozen.reviewer<>NEW.actor THEN
                    RAISE EXCEPTION 'frozen revision lacks publication' USING ERRCODE='23514';
                END IF;
            END IF;
        END IF;
    ELSIF TG_TABLE_NAME='dataset_versions' THEN
        SELECT * INTO draft FROM waveforms.dataset_drafts WHERE dataset_id=NEW.dataset_id FOR UPDATE;
        SELECT * INTO prior FROM waveforms.dataset_revisions WHERE dataset_id=NEW.dataset_id ORDER BY revision DESC LIMIT 1;
        IF prior.state IS DISTINCT FROM 'submitted' OR NEW.draft_revision IS DISTINCT FROM prior.revision
            OR NEW.purpose IS DISTINCT FROM prior.body->>'purpose'
            OR (NEW.reviewer->>'domain',NEW.reviewer->>'user_id')=(draft.creator->>'domain',draft.creator->>'user_id')
            OR (NEW.reviewer->>'domain',NEW.reviewer->>'user_id')=(prior.actor->>'domain',prior.actor->>'user_id') THEN
            RAISE EXCEPTION 'dataset publication requires independent review of submission' USING ERRCODE='23514';
        END IF;
    ELSIF TG_TABLE_NAME='dataset_events' THEN
        SELECT * INTO revision_row FROM waveforms.dataset_revisions WHERE dataset_id=NEW.dataset_id AND revision=NEW.revision;
        IF NEW.state IS DISTINCT FROM revision_row.state OR NEW.actor IS DISTINCT FROM revision_row.actor THEN
            RAISE EXCEPTION 'dataset event differs from revision' USING ERRCODE='23514';
        END IF;
        IF NEW.version_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM waveforms.dataset_versions WHERE version_id=NEW.version_id AND dataset_id=NEW.dataset_id AND draft_revision=NEW.revision-1) THEN
            RAISE EXCEPTION 'dataset event publication differs' USING ERRCODE='23514';
        END IF;
    ELSIF TG_TABLE_NAME='dataset_exports' THEN
        IF NOT EXISTS(SELECT 1 FROM waveforms.dataset_versions WHERE version_id=NEW.version_id AND content_sha256=NEW.content_sha256) THEN
            RAISE EXCEPTION 'export content identity differs' USING ERRCODE='23514';
        END IF;
    ELSE
        SELECT * INTO frozen FROM waveforms.dataset_versions WHERE version_id=NEW.version_id FOR UPDATE;
        SELECT * INTO prior FROM waveforms.dataset_revisions WHERE dataset_id=frozen.dataset_id ORDER BY revision DESC LIMIT 1;
        IF prior.state IS DISTINCT FROM 'submitted' OR prior.revision IS DISTINCT FROM frozen.draft_revision THEN
            RAISE EXCEPTION 'dataset publication is closed' USING ERRCODE='23514';
        END IF;
        IF TG_TABLE_NAME='dataset_members' THEN
            SELECT * INTO cert FROM waveforms.quality_runs WHERE quality_run_id=NEW.quality_run_id;
            IF NEW.ordinal>=frozen.member_count
                OR (NEW.kind='capture' AND (frozen.purpose<>'raw_archive' OR cert.capture_pk IS DISTINCT FROM NEW.reference_id))
                OR (NEW.kind='pair' AND NOT EXISTS(SELECT 1 FROM waveforms.pair_versions WHERE pair_version_id=NEW.reference_id AND NEW.quality_run_id IN (left_quality_run_id,right_quality_run_id))) THEN
                RAISE EXCEPTION 'dataset member identity differs' USING ERRCODE='23514';
            END IF;
        ELSIF TG_TABLE_NAME='dataset_files' THEN
            IF NEW.ordinal>=frozen.file_count OR NEW.descriptor->>'capture_pk' IS DISTINCT FROM NEW.capture_pk::text THEN
                RAISE EXCEPTION 'dataset file identity differs' USING ERRCODE='23514';
            END IF;
        ELSIF TG_TABLE_NAME='dataset_manifest_chunks' AND NEW.ordinal>=frozen.chunk_count THEN
            RAISE EXCEPTION 'dataset manifest chunk outside publication' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION waveforms.dataset_publication_complete() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM waveforms.dataset_events WHERE version_id=NEW.version_id AND state='frozen')
        OR (SELECT count(*) FROM waveforms.dataset_members WHERE version_id=NEW.version_id)<>NEW.member_count
        OR (SELECT count(*) FROM waveforms.dataset_files WHERE version_id=NEW.version_id)<>NEW.file_count
        OR (SELECT count(*) FROM waveforms.dataset_manifest_chunks WHERE version_id=NEW.version_id)<>NEW.chunk_count
        OR (SELECT coalesce(sum(octet_length(payload)),0) FROM waveforms.dataset_manifest_chunks WHERE version_id=NEW.version_id)<>NEW.manifest_bytes THEN
        RAISE EXCEPTION 'dataset publication is incomplete' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER dataset_publication_complete AFTER INSERT ON waveforms.dataset_versions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION waveforms.dataset_publication_complete();

DO $$ DECLARE name TEXT; BEGIN
    FOREACH name IN ARRAY ARRAY['dataset_drafts','dataset_revisions','dataset_events','dataset_versions','dataset_members','dataset_files','dataset_manifest_chunks','dataset_exports'] LOOP
        EXECUTE format('CREATE TRIGGER immutable_content BEFORE UPDATE OR DELETE ON waveforms.%I FOR EACH ROW EXECUTE FUNCTION waveforms.immutable_row()',name);
        EXECUTE format('CREATE TRIGGER bounded_business_metadata AFTER INSERT ON waveforms.%I FOR EACH ROW EXECUTE FUNCTION waveforms.charge_business()',name);
        IF name<>'dataset_drafts' THEN
            EXECUTE format('CREATE TRIGGER dataset_identity BEFORE INSERT ON waveforms.%I FOR EACH ROW EXECUTE FUNCTION waveforms.dataset_identity()',name);
        END IF;
        EXECUTE format('REVOKE ALL ON waveforms.%I FROM PUBLIC',name);
    END LOOP;
END $$;
REVOKE ALL ON FUNCTION waveforms.dataset_identity(),waveforms.dataset_publication_complete() FROM PUBLIC;
