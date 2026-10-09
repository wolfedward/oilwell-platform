-- Exact metadata cannot be reconstructed from legacy sessions.
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM waveforms.imports) THEN
        RAISE EXCEPTION 'legacy imports require a separately reviewed migration' USING ERRCODE='55000';
    END IF;
END $$;

ALTER TABLE waveforms.imports
    ADD COLUMN final_lf BOOLEAN NOT NULL,
    ADD COLUMN device_id TEXT NOT NULL CHECK(device_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    ADD COLUMN capture_id UUID NOT NULL,
    ADD COLUMN version BIGINT NOT NULL DEFAULT 1 CHECK(version>0),
    DROP CONSTRAINT imports_state_check,
    ADD CONSTRAINT imports_state_check CHECK(state IN ('recorded','complete','failed','cancelled'));
CREATE INDEX import_creator_open ON waveforms.imports((actor->>'domain'),(actor->>'user_id')) WHERE state='recorded';
CREATE INDEX import_capture_sources ON waveforms.imports(intake_gateway_id,device_id,capture_id);

CREATE FUNCTION waveforms.import_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF TG_OP='DELETE' OR (TG_OP='UPDATE' AND (
        (NEW.import_id,NEW.intake_gateway_id,NEW.actor,NEW.file_sha256,NEW.file_bytes,NEW.line_count,
         NEW.recorded_at,NEW.final_lf,NEW.device_id,NEW.capture_id) IS DISTINCT FROM
        (OLD.import_id,OLD.intake_gateway_id,OLD.actor,OLD.file_sha256,OLD.file_bytes,OLD.line_count,
         OLD.recorded_at,OLD.final_lf,OLD.device_id,OLD.capture_id)
        OR OLD.state<>'recorded' OR NEW.version<>OLD.version+1)) THEN
        RAISE EXCEPTION 'immutable import identity or invalid transition' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER import_identity BEFORE UPDATE OR DELETE ON waveforms.imports
    FOR EACH ROW EXECUTE FUNCTION waveforms.import_identity();

CREATE TABLE waveforms.import_events (
    import_id UUID NOT NULL,
    intake_gateway_id TEXT NOT NULL,
    version BIGINT NOT NULL CHECK(version>0),
    state TEXT NOT NULL CHECK(state IN ('recorded','complete','failed','cancelled')),
    action TEXT NOT NULL CHECK(action IN ('create','append','seal','cancel')),
    actor JSONB NOT NULL CHECK(waveforms.valid_actor(actor)),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 512 AND btrim(reason)<>'' AND reason !~ '[[:cntrl:]]'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(isfinite(recorded_at)),
    PRIMARY KEY(import_id,version),
    FOREIGN KEY(import_id,intake_gateway_id) REFERENCES waveforms.imports(import_id,intake_gateway_id)
);
CREATE TRIGGER immutable_content BEFORE UPDATE OR DELETE ON waveforms.import_events
    FOR EACH ROW EXECUTE FUNCTION waveforms.immutable_row();
CREATE TRIGGER charge_metadata AFTER INSERT ON waveforms.import_events
    FOR EACH ROW EXECUTE FUNCTION waveforms.charge_metadata();
REVOKE ALL ON waveforms.import_events FROM PUBLIC;
REVOKE ALL ON FUNCTION waveforms.import_identity() FROM PUBLIC;
