-- Stable identity and physical configuration version are independent.
-- Mirror only known initial content/time; never invent earlier configurations.
CREATE TABLE inversion.assembly_versions(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),assembly_id uuid NOT NULL REFERENCES inversion.assemblies(id),
 version bigint NOT NULL CHECK(version>0),body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'),
 content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(assembly_id,version));
INSERT INTO inversion.assembly_versions(assembly_id,version,body,content_sha256,created_at)
 SELECT id,version,body,content_sha256,created_at FROM inversion.assemblies;
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON inversion.assembly_versions FOR EACH ROW EXECUTE FUNCTION inversion.reject_mutation();
CREATE FUNCTION inversion.initial_assembly_version() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 INSERT INTO inversion.assembly_versions(assembly_id,version,body,content_sha256,created_at)
 VALUES(NEW.id,NEW.version,NEW.body,NEW.content_sha256,NEW.created_at);
 RETURN NEW;
END $$;
CREATE TRIGGER initial_assembly_version AFTER INSERT ON inversion.assemblies FOR EACH ROW EXECUTE FUNCTION inversion.initial_assembly_version();
ALTER TABLE inversion.parameter_versions ADD CONSTRAINT parameter_mechanical_version
 FOREIGN KEY(assembly_id,assembly_version) REFERENCES inversion.assembly_versions(assembly_id,version);
REVOKE ALL ON inversion.assembly_versions FROM PUBLIC;
REVOKE ALL ON FUNCTION inversion.initial_assembly_version() FROM PUBLIC;
