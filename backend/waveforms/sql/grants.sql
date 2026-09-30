GRANT USAGE ON SCHEMA waveforms TO {receiver},{assembler},{receipt_publisher},{reader},{operator};
GRANT SELECT ON waveforms.schema_migrations TO {receiver},{assembler},{receipt_publisher},{reader},{operator};
GRANT SELECT ON waveforms.fact_metadata,waveforms.outcome_metadata,waveforms.capture_metadata,waveforms.receipt_metadata,
    waveforms.audit_metadata,waveforms.capacity_metadata,waveforms.worker_metadata TO {reader},{operator};
GRANT EXECUTE ON FUNCTION waveforms.audited_raw(UUID,JSONB) TO {reader};
GRANT EXECUTE ON FUNCTION waveforms.valid_actor(JSONB) TO {receiver},{operator};

GRANT SELECT,INSERT ON waveforms.facts,waveforms.message_outcomes,waveforms.observations,waveforms.captures,waveforms.streams,
    waveforms.chunks,waveforms.chunk_slots,waveforms.capture_conflicts,waveforms.assembly_jobs,
    waveforms.receipts,waveforms.deliveries,waveforms.imports,waveforms.import_lines,
    waveforms.import_events,waveforms.operation_receipts TO {receiver};
GRANT INSERT ON waveforms.audit TO {receiver};
GRANT SELECT ON waveforms.capacity,waveforms.state_head TO {receiver},{assembler};
GRANT UPDATE(used_raw_bytes,reserved_raw_bytes,used_facts,used_incomplete_captures,used_orphan_bytes,used_quarantine_bytes)
    ON waveforms.capacity TO {receiver};
GRANT UPDATE(last_seen,duplicate_count,anomaly_count,counters_saturated) ON waveforms.observations TO {receiver};
GRANT UPDATE(manifest_fact_id,manifest_outcome_id,manifest_sha256,last_valid_at,reserved_raw_bytes,used_raw_bytes,orphan_bytes,version)
    ON waveforms.captures TO {receiver};
GRANT UPDATE(requested_generation,next_attempt_at,version) ON waveforms.assembly_jobs TO {receiver};
GRANT UPDATE(state,version) ON waveforms.imports TO {receiver};
GRANT UPDATE(version) ON waveforms.state_head TO {receiver},{assembler},{operator},{receipt_publisher};
GRANT UPDATE(requested_generation,requested_at,next_attempt_at,version) ON waveforms.deliveries TO {receiver},{assembler};

GRANT SELECT ON waveforms.facts,waveforms.message_outcomes,waveforms.captures,waveforms.streams,waveforms.chunks,waveforms.chunk_slots,
    waveforms.capture_conflicts,waveforms.observations TO {assembler};
GRANT SELECT,INSERT ON waveforms.completion,waveforms.receipts,waveforms.deliveries TO {assembler};
GRANT SELECT ON waveforms.assembly_jobs TO {assembler};
GRANT UPDATE(finished_generation,lease_token,lease_generation,lease_until,next_attempt_at,attempts,last_error_code,version)
    ON waveforms.assembly_jobs TO {assembler};
GRANT UPDATE(state,reserved_raw_bytes,version) ON waveforms.captures TO {assembler};
GRANT UPDATE(reserved_raw_bytes,used_incomplete_captures) ON waveforms.capacity TO {assembler};

GRANT SELECT ON waveforms.receipts,waveforms.deliveries,waveforms.capacity,waveforms.state_head TO {receipt_publisher};
GRANT UPDATE(sent_generation,lease_token,lease_generation,lease_until,next_attempt_at,attempts,last_error_code,broker_acked_at,version)
    ON waveforms.deliveries TO {receipt_publisher};
GRANT UPDATE(last_clock_seen) ON waveforms.capacity TO {receipt_publisher};
GRANT SELECT,INSERT ON waveforms.worker_health TO {receiver},{assembler},{receipt_publisher};
GRANT UPDATE(last_seen,last_success,last_failure,error_code) ON waveforms.worker_health TO {receiver},{assembler},{receipt_publisher};

GRANT SELECT ON waveforms.state_head TO {operator};
GRANT SELECT,INSERT ON waveforms.operation_receipts TO {operator};
GRANT INSERT ON waveforms.audit TO {operator};

GRANT SELECT ON waveforms.business_settings,waveforms.evidence_versions,waveforms.evidence_events,
    waveforms.restrictions,waveforms.quality_runs,waveforms.pair_versions,waveforms.pair_reviews,
    waveforms.attachment_metadata,waveforms.provenance_metadata,waveforms.capture_restrictions,
    waveforms.business_audit TO {reader},{operator};
GRANT SELECT ON waveforms.business_receipts TO {operator};
GRANT INSERT ON waveforms.attachments,waveforms.evidence_versions,waveforms.evidence_events,
    waveforms.restrictions,waveforms.quality_runs,waveforms.pair_versions,waveforms.pair_reviews,
    waveforms.business_audit,waveforms.business_receipts TO {operator};
GRANT SELECT ON waveforms.registry_devices,waveforms.registry_device_states,waveforms.registry_well_bindings,
    waveforms.registry_wells,waveforms.registry_head TO {reader};
GRANT EXECUTE ON FUNCTION waveforms.lock_business_capacity(TEXT),waveforms.lock_business_captures(UUID[]) TO {operator};
GRANT EXECUTE ON FUNCTION waveforms.audited_attachment(UUID,JSONB,TEXT) TO {reader};
GRANT SELECT(capture_pk,stream_id,chunk_index,fact_id) ON waveforms.chunk_slots TO {reader};
GRANT SELECT(capture_pk,manifest_fact_id,status,reason_code,result,completed_at) ON waveforms.completion TO {reader},{operator};
GRANT SELECT(import_id,file_sha256,file_bytes,line_count,final_lf) ON waveforms.imports TO {reader};
GRANT SELECT(singleton,version) ON waveforms.state_head TO {reader};
GRANT SELECT(capture_pk,stream_id,chunk_count) ON waveforms.streams TO {reader};
GRANT SELECT ON waveforms.dataset_drafts,waveforms.dataset_revisions,waveforms.dataset_events,
    waveforms.dataset_versions,waveforms.dataset_members,waveforms.dataset_files,
    waveforms.dataset_manifest_chunks,waveforms.dataset_exports TO {reader},{operator};
GRANT INSERT ON waveforms.dataset_drafts,waveforms.dataset_revisions,waveforms.dataset_events,
    waveforms.dataset_versions,waveforms.dataset_members,waveforms.dataset_files,
    waveforms.dataset_manifest_chunks,waveforms.dataset_exports TO {operator};
