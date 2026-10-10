CREATE SCHEMA inversion;
REVOKE ALL ON SCHEMA inversion FROM PUBLIC;
CREATE TABLE inversion.schema_migrations(version integer PRIMARY KEY,sha256 text NOT NULL,structure_sha256 text NOT NULL);
CREATE TABLE inversion.state_head(singleton integer PRIMARY KEY CHECK(singleton=1),version bigint NOT NULL CHECK(version>0));
INSERT INTO inversion.state_head VALUES(1,1);
CREATE TABLE inversion.assemblies(
 id uuid PRIMARY KEY,version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'),content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE inversion.assignments(
 id uuid PRIMARY KEY,assembly_id uuid NOT NULL REFERENCES inversion.assemblies(id),well_id text NOT NULL,
 valid_from timestamptz NOT NULL,valid_to timestamptz,CHECK(valid_to IS NULL OR valid_to>valid_from),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),body jsonb NOT NULL,content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE inversion.assignment_state(
 assignment_id uuid PRIMARY KEY REFERENCES inversion.assignments(id),version bigint NOT NULL CHECK(version>0),valid_to timestamptz);
CREATE TABLE inversion.assignment_events(
 id uuid PRIMARY KEY,assignment_id uuid NOT NULL REFERENCES inversion.assignments(id),version bigint NOT NULL CHECK(version>1),
 valid_to timestamptz NOT NULL,actor jsonb NOT NULL,reason text NOT NULL CHECK(length(reason) BETWEEN 1 AND 512),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(assignment_id,version));
CREATE FUNCTION inversion.assignment_project() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE start_at timestamptz; end_at timestamptz; previous_version bigint;
BEGIN
 IF TG_TABLE_NAME='assignments' THEN
   INSERT INTO inversion.assignment_state VALUES(NEW.id,1,NEW.valid_to);
 ELSE
   PERFORM pg_advisory_xact_lock(499001610);
   SELECT a.valid_from,s.valid_to,s.version INTO start_at,end_at,previous_version
    FROM inversion.assignments a JOIN inversion.assignment_state s ON s.assignment_id=a.id
    WHERE a.id=NEW.assignment_id FOR UPDATE OF s;
   IF NOT FOUND OR NEW.version<>previous_version+1 OR NEW.valid_to<=start_at OR (end_at IS NOT NULL AND NEW.valid_to>end_at) THEN
     RAISE EXCEPTION 'Invalid assignment end event' USING ERRCODE='23514';
   END IF;
   UPDATE inversion.assignment_state SET version=NEW.version,valid_to=NEW.valid_to WHERE assignment_id=NEW.assignment_id;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER assignment_project AFTER INSERT ON inversion.assignments FOR EACH ROW EXECUTE FUNCTION inversion.assignment_project();
CREATE TRIGGER assignment_end BEFORE INSERT ON inversion.assignment_events FOR EACH ROW EXECUTE FUNCTION inversion.assignment_project();
CREATE FUNCTION inversion.assignment_overlap() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(499001610);
 IF EXISTS(SELECT 1 FROM inversion.assignments a JOIN inversion.assignment_state s ON s.assignment_id=a.id WHERE (a.well_id=NEW.well_id OR a.assembly_id=NEW.assembly_id)
   AND tstzrange(a.valid_from,s.valid_to,'[)') && tstzrange(NEW.valid_from,NEW.valid_to,'[)')) THEN
   RAISE EXCEPTION 'Overlapping assignment' USING ERRCODE='23P01';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER assignment_overlap BEFORE INSERT ON inversion.assignments FOR EACH ROW EXECUTE FUNCTION inversion.assignment_overlap();
CREATE TABLE inversion.parameter_versions(
 id uuid PRIMARY KEY,assembly_id uuid NOT NULL REFERENCES inversion.assemblies(id),assembly_version bigint NOT NULL CHECK(assembly_version>0),
 version bigint NOT NULL CHECK(version>0),parent_version bigint CHECK(parent_version>0),author jsonb NOT NULL,
 body jsonb NOT NULL,content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(assembly_id,version));
CREATE TABLE inversion.reviews(
 id uuid PRIMARY KEY,subject_id uuid NOT NULL REFERENCES inversion.parameter_versions(id),version bigint NOT NULL CHECK(version>0),
 action text NOT NULL CHECK(action IN ('submit','approve','reject','revoke')),actor jsonb NOT NULL,reason text NOT NULL,
 body jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(subject_id,version));
CREATE TABLE inversion.model_versions(
 id uuid PRIMARY KEY,model_key text NOT NULL,version bigint NOT NULL CHECK(version>0),body jsonb NOT NULL,
 content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(model_key,version));
CREATE TABLE inversion.model_reviews(
 id uuid PRIMARY KEY,subject_id uuid NOT NULL REFERENCES inversion.model_versions(id),version bigint NOT NULL CHECK(version>0),
 action text NOT NULL CHECK(action IN ('submit','approve','reject','revoke')),actor jsonb NOT NULL,reason text NOT NULL,
 body jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(subject_id,version));
CREATE TABLE inversion.selections(
 id uuid PRIMARY KEY,assembly_id uuid NOT NULL REFERENCES inversion.assemblies(id),parameter_id uuid NOT NULL REFERENCES inversion.parameter_versions(id),
 model_id uuid NOT NULL REFERENCES inversion.model_versions(id),version bigint NOT NULL CHECK(version>0),actor jsonb NOT NULL,reason text NOT NULL,
 body jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(assembly_id,version));
CREATE TABLE inversion.run_requests(
 id uuid PRIMARY KEY,pair_id uuid NOT NULL,parameter_id uuid NOT NULL REFERENCES inversion.parameter_versions(id),model_id uuid NOT NULL REFERENCES inversion.model_versions(id),
 predecessor_id uuid REFERENCES inversion.run_requests(id),actor jsonb NOT NULL,body jsonb NOT NULL,
 content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),input_sha256 text NOT NULL CHECK(input_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE inversion.run_state(
 run_id uuid PRIMARY KEY REFERENCES inversion.run_requests(id),version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 state text NOT NULL CHECK(state IN ('queued','running','completed','blocked','failed','cancelled')),
 generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),owner_id uuid,deadline timestamptz,heartbeat_at timestamptz,reason_code text,
 CHECK(state<>'running' OR (generation>0 AND owner_id IS NOT NULL AND deadline IS NOT NULL)));
CREATE TABLE inversion.run_events(
 id uuid PRIMARY KEY,run_id uuid NOT NULL REFERENCES inversion.run_requests(id),version bigint NOT NULL CHECK(version>0),
 state text NOT NULL CHECK(state IN ('queued','running','completed','blocked','failed','cancelled')),body jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(run_id,version));
CREATE TABLE inversion.attempts(
 id uuid PRIMARY KEY,run_id uuid NOT NULL REFERENCES inversion.run_requests(id),generation bigint NOT NULL CHECK(generation>0),
 owner_id uuid NOT NULL,deadline timestamptz NOT NULL,body jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(run_id,generation));
CREATE TABLE inversion.attempt_events(
 id uuid PRIMARY KEY,attempt_id uuid NOT NULL REFERENCES inversion.attempts(id),action text NOT NULL,body jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE inversion.results(
 id uuid PRIMARY KEY,run_id uuid NOT NULL UNIQUE,generation bigint NOT NULL,
 input_snapshot jsonb NOT NULL,body jsonb NOT NULL,content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),FOREIGN KEY(run_id,generation) REFERENCES inversion.attempts(run_id,generation));
CREATE TABLE inversion.result_chunks(
 result_id uuid NOT NULL REFERENCES inversion.results(id),ordinal integer NOT NULL CHECK(ordinal>=0),payload bytea NOT NULL,
 content_sha256 text NOT NULL CHECK(content_sha256=encode(sha256(payload),'hex')),PRIMARY KEY(result_id,ordinal));
CREATE TABLE inversion.result_reviews(
 id uuid PRIMARY KEY,subject_id uuid NOT NULL REFERENCES inversion.results(id),version bigint NOT NULL CHECK(version>0),
 action text NOT NULL CHECK(action IN ('approve','reject','revoke')),actor jsonb NOT NULL,reason text NOT NULL,body jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(subject_id,version));
CREATE TABLE inversion.restrictions(
 id uuid PRIMARY KEY,subject_id uuid NOT NULL,subject_kind text NOT NULL CHECK(subject_kind IN ('parameter','model','result','assembly')),
 version bigint NOT NULL CHECK(version>0),action text NOT NULL,body jsonb NOT NULL,actor jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(subject_kind,subject_id,version));
CREATE TABLE inversion.audit(
 id uuid PRIMARY KEY,actor jsonb NOT NULL,action text NOT NULL,subject_id uuid NOT NULL,reason text NOT NULL,
 request_key uuid NOT NULL,response jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE inversion.receipts(
 actor_domain text NOT NULL,actor_id text NOT NULL,key uuid NOT NULL,request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
 subject_id uuid NOT NULL,version bigint NOT NULL CHECK(version>0),content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
 response jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(actor_domain,actor_id,key));
CREATE TABLE inversion.capacity(
 singleton integer PRIMARY KEY CHECK(singleton=1),limits jsonb NOT NULL,total_storage_bytes bigint NOT NULL CHECK(total_storage_bytes>0),
 queued integer NOT NULL DEFAULT 0 CHECK(queued BETWEEN 0 AND 16),running integer NOT NULL DEFAULT 0 CHECK(running BETWEEN 0 AND 1),
 reserved_bytes bigint NOT NULL DEFAULT 0 CHECK(reserved_bytes>=0),used_bytes bigint NOT NULL DEFAULT 0 CHECK(used_bytes>=0),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),CHECK(reserved_bytes+used_bytes<=total_storage_bytes));
CREATE TABLE inversion.reservations(
 run_id uuid PRIMARY KEY,input_bytes bigint NOT NULL CHECK(input_bytes=67108864),result_bytes bigint NOT NULL CHECK(result_bytes=16777216),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),state text NOT NULL CHECK(state IN ('queued','running','settled')),
 used_bytes bigint CHECK(used_bytes BETWEEN 0 AND 83886080),CHECK((state='settled')=(used_bytes IS NOT NULL)));
CREATE FUNCTION inversion.reject_mutation() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN RAISE EXCEPTION 'Immutable inversion record' USING ERRCODE='55000'; END $$;
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['assemblies','assignments','assignment_events','parameter_versions','reviews','model_versions','model_reviews','selections','run_requests','run_events','attempts','attempt_events','results','result_chunks','result_reviews','restrictions','audit','receipts'] LOOP
  EXECUTE format('CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON inversion.%I FOR EACH ROW EXECUTE FUNCTION inversion.reject_mutation()',name);
 END LOOP;
END $$;
CREATE FUNCTION inversion.check_run_transition() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='INSERT' THEN
   IF NEW.state<>'queued' OR NEW.version<>1 OR NEW.generation<>0 OR NEW.owner_id IS NOT NULL OR NEW.deadline IS NOT NULL THEN
     RAISE EXCEPTION 'Runs must start queued' USING ERRCODE='23514';
   END IF;
   RETURN NEW;
 END IF;
 IF NEW.run_id<>OLD.run_id OR NEW.version<>OLD.version+1 OR
    NOT ((OLD.state='queued' AND NEW.state IN ('running','blocked','failed','cancelled')) OR
         (OLD.state='running' AND NEW.state IN ('running','completed','blocked','failed','cancelled'))) OR
    (OLD.state='running' AND (NEW.generation<>OLD.generation OR NEW.owner_id IS DISTINCT FROM OLD.owner_id OR NEW.deadline IS DISTINCT FROM OLD.deadline)) OR
    (NEW.state='running' AND OLD.state='queued' AND NEW.generation<>OLD.generation+1) THEN
   RAISE EXCEPTION 'Invalid run transition' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER run_transition BEFORE INSERT OR UPDATE ON inversion.run_state FOR EACH ROW EXECUTE FUNCTION inversion.check_run_transition();
CREATE FUNCTION inversion.require_run_event() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM inversion.run_events e WHERE e.run_id=NEW.run_id AND e.version=NEW.version AND e.state=NEW.state) THEN
   RAISE EXCEPTION 'Run state requires matching append event' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER run_event_required AFTER INSERT OR UPDATE ON inversion.run_state DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION inversion.require_run_event();
CREATE FUNCTION inversion.check_result_fence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE current_state inversion.run_state; attempt_owner uuid;
BEGIN
 SELECT * INTO current_state FROM inversion.run_state WHERE run_id=NEW.run_id FOR UPDATE;
 SELECT owner_id INTO attempt_owner FROM inversion.attempts WHERE run_id=NEW.run_id AND generation=NEW.generation;
 IF current_state.state IS DISTINCT FROM 'running' OR current_state.generation IS DISTINCT FROM NEW.generation
    OR current_state.owner_id IS DISTINCT FROM attempt_owner OR current_state.deadline<=clock_timestamp() THEN
   RAISE EXCEPTION 'Result attempt is no longer admitted' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER result_fence BEFORE INSERT ON inversion.results FOR EACH ROW EXECUTE FUNCTION inversion.check_result_fence();
REVOKE ALL ON ALL TABLES IN SCHEMA inversion FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA inversion FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA inversion REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
