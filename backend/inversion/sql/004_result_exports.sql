-- Export identity only. Payload remains in the immutable result/chunk records.
CREATE TABLE inversion.result_exports(
 id uuid PRIMARY KEY,
 result_id uuid NOT NULL REFERENCES inversion.results(id),
 result_sha256 text NOT NULL CHECK(result_sha256 ~ '^[0-9a-f]{64}$'),
 intent text NOT NULL CHECK(intent IN ('quantitative','evidence')),
 format text NOT NULL CHECK(format IN ('json','csv')),
 actor_domain text NOT NULL, actor_id text NOT NULL,
 manifest jsonb NOT NULL CHECK(jsonb_typeof(manifest)='object'),
 manifest_sha256 text NOT NULL CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON inversion.result_exports
 FOR EACH ROW EXECUTE FUNCTION inversion.reject_mutation();
REVOKE ALL ON inversion.result_exports FROM PUBLIC;
