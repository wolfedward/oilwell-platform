-- Bounded internal research identity. Existing full dataset export is unchanged.
CREATE TABLE waveforms.member_research_exports (
    export_id UUID PRIMARY KEY,
    version_id UUID NOT NULL,
    member_ordinal INTEGER NOT NULL CHECK(member_ordinal BETWEEN 0 AND 999),
    accounting_bucket TEXT NOT NULL REFERENCES waveforms.business_settings(accounting_bucket),
    actor JSONB NOT NULL CHECK(waveforms.valid_actor(actor)),
    content_sha256 TEXT NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_sha256 TEXT NOT NULL CHECK(evidence_sha256 ~ '^[0-9a-f]{64}$'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(version_id,member_ordinal) REFERENCES waveforms.dataset_members(version_id,ordinal)
);
CREATE TRIGGER immutable_content BEFORE UPDATE OR DELETE ON waveforms.member_research_exports
    FOR EACH ROW EXECUTE FUNCTION waveforms.immutable_row();
CREATE TRIGGER bounded_business_metadata AFTER INSERT ON waveforms.member_research_exports
    FOR EACH ROW EXECUTE FUNCTION waveforms.charge_business();
REVOKE ALL ON waveforms.member_research_exports FROM PUBLIC;

-- Accepted manifest projections, never raw bytes. Restrictions remain visible.
CREATE VIEW waveforms.archive_channel_metadata AS
SELECT c.capture_pk,c.gateway_id,c.device_id,c.capture_id,c.manifest_sha256,c.state,
       done.status AS completion_status,
       EXISTS(SELECT 1 FROM waveforms.capture_restrictions r WHERE r.subject_id=c.capture_pk) AS restricted,
       manifest.body->>'utc_start' AS utc_start,
       manifest.body->>'utc_uncertainty_ns' AS utc_uncertainty_ns,
       manifest.body->>'source_kind' AS source_kind,
       manifest.body->'cycle' AS cycle,
       s.stream_id,s.specification
FROM waveforms.captures c
JOIN waveforms.facts f ON f.fact_id=c.manifest_fact_id
CROSS JOIN LATERAL (SELECT convert_from(f.raw_payload,'UTF8')::jsonb AS body) manifest
JOIN waveforms.streams s ON s.capture_pk=c.capture_pk AND s.manifest_fact_id=c.manifest_fact_id
LEFT JOIN waveforms.completion done ON done.capture_pk=c.capture_pk;
REVOKE ALL ON waveforms.archive_channel_metadata FROM PUBLIC;

CREATE VIEW waveforms.registry_point_maps AS
SELECT device_id,version,transport,canonical_rule,content,sha256,status,created_at,reviewed_at,
       created_event_id,review_event_id FROM device_registry.point_map_versions;
CREATE VIEW waveforms.registry_map_assignments AS
SELECT id,device_id,map_version,valid_from,valid_to,audit_event_id FROM device_registry.point_map_assignments;
REVOKE ALL ON waveforms.registry_point_maps,waveforms.registry_map_assignments FROM PUBLIC;
