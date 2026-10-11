-- Installation declarations remain immutable. Only explicit end events shorten them.
CREATE TABLE inversion.mechanical_state(
 version_id uuid PRIMARY KEY REFERENCES inversion.assembly_versions(id),
 revision bigint NOT NULL CHECK(revision>0),valid_from timestamptz NOT NULL,valid_to timestamptz,
 CHECK(valid_to IS NULL OR valid_to>valid_from));
CREATE TABLE inversion.mechanical_events(
 id uuid PRIMARY KEY,version_id uuid NOT NULL REFERENCES inversion.assembly_versions(id),
 revision bigint NOT NULL CHECK(revision>1),valid_to timestamptz NOT NULL,
 actor jsonb NOT NULL,reason text NOT NULL,body jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(version_id,revision));
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON inversion.mechanical_events
 FOR EACH ROW EXECUTE FUNCTION inversion.reject_mutation();
CREATE FUNCTION inversion.mechanical_project() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE start_at timestamptz; end_at timestamptz; previous_revision bigint;
BEGIN
 PERFORM pg_advisory_xact_lock(499001610);
 IF TG_TABLE_NAME='assembly_versions' THEN
   -- Legacy snapshots without declared physical dates remain explicitly unresolved.
   IF NOT (NEW.body ? 'valid_from') THEN RETURN NEW; END IF;
   start_at=(NEW.body->>'valid_from')::timestamptz;
   end_at=(NEW.body->>'valid_to')::timestamptz;
   IF start_at IS NULL OR NOT isfinite(start_at) OR (end_at IS NOT NULL AND (NOT isfinite(end_at) OR end_at<=start_at)) THEN
     RAISE EXCEPTION 'Invalid physical declaration' USING ERRCODE='23514';
   END IF;
   IF EXISTS(SELECT 1 FROM inversion.assembly_versions v JOIN inversion.mechanical_state s ON s.version_id=v.id
     WHERE v.assembly_id=NEW.assembly_id AND tstzrange(s.valid_from,s.valid_to,'[)') && tstzrange(start_at,end_at,'[)')) THEN
     RAISE EXCEPTION 'Overlapping physical versions' USING ERRCODE='23P01';
   END IF;
   INSERT INTO inversion.mechanical_state VALUES(NEW.id,1,start_at,end_at);
 ELSE
   SELECT valid_from,valid_to,revision INTO start_at,end_at,previous_revision
     FROM inversion.mechanical_state WHERE version_id=NEW.version_id FOR UPDATE;
   IF NOT FOUND OR NEW.revision<>previous_revision+1 OR NOT isfinite(NEW.valid_to) OR NEW.valid_to<=start_at OR
      (end_at IS NOT NULL AND NEW.valid_to>end_at) THEN
     RAISE EXCEPTION 'Invalid physical end event' USING ERRCODE='23514';
   END IF;
   UPDATE inversion.mechanical_state SET valid_to=NEW.valid_to,revision=NEW.revision WHERE version_id=NEW.version_id;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER mechanical_initial AFTER INSERT ON inversion.assembly_versions
 FOR EACH ROW EXECUTE FUNCTION inversion.mechanical_project();
CREATE TRIGGER mechanical_end BEFORE INSERT ON inversion.mechanical_events
 FOR EACH ROW EXECUTE FUNCTION inversion.mechanical_project();
-- Never infer installation from insertion or approval timestamps.
INSERT INTO inversion.mechanical_state
 SELECT id,1,(body->>'valid_from')::timestamptz,(body->>'valid_to')::timestamptz
 FROM inversion.assembly_versions WHERE body ? 'valid_from';
REVOKE ALL ON inversion.mechanical_state,inversion.mechanical_events FROM PUBLIC;
REVOKE ALL ON FUNCTION inversion.mechanical_project() FROM PUBLIC;
