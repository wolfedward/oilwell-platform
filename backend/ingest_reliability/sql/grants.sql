GRANT USAGE ON SCHEMA ingest_reliability,public TO {ingestor},{publisher},{reader},{operator};
GRANT SELECT ON ingest_reliability.schema_migrations TO {ingestor},{publisher},{reader},{operator};
GRANT SELECT ON ingest_reliability.case_metadata,ingest_reliability.receipt_metadata,
    ingest_reliability.health_metadata,ingest_reliability.audit_metadata,
    ingest_reliability.worker_health_metadata TO {reader},{operator};
GRANT EXECUTE ON FUNCTION ingest_reliability.audited_raw(UUID,JSONB,BOOLEAN) TO {operator};

GRANT SELECT,INSERT ON public.ingest_messages,public.telemetry_samples TO {ingestor};
GRANT SELECT,INSERT ON ingest_reliability.facts,ingest_reliability.fact_observations,
    ingest_reliability.cases,ingest_reliability.receipts,ingest_reliability.deliveries TO {ingestor};
GRANT SELECT ON ingest_reliability.capacity TO {ingestor},{publisher};
GRANT UPDATE(used_total_bytes,used_quarantine_bytes) ON ingest_reliability.capacity TO {ingestor};
GRANT UPDATE(last_clock_seen) ON ingest_reliability.capacity TO {publisher};
GRANT UPDATE(last_seen,duplicate_count,transport_anomaly_count,counters_saturated)
    ON ingest_reliability.fact_observations TO {ingestor};
GRANT UPDATE(requested_generation,requested_at,next_attempt_at,version)
    ON ingest_reliability.deliveries TO {ingestor},{operator};
GRANT INSERT ON ingest_reliability.audit_events TO {ingestor},{operator};
GRANT EXECUTE ON FUNCTION ingest_reliability.reopen_repeated_case(UUID,TIMESTAMPTZ) TO {ingestor};

GRANT SELECT ON ingest_reliability.receipts,ingest_reliability.deliveries TO {publisher};
GRANT UPDATE(sent_generation,lease_token,lease_generation,lease_until,next_attempt_at,
    attempts,last_error_code,broker_acked_at,version) ON ingest_reliability.deliveries TO {publisher};
GRANT SELECT,INSERT ON ingest_reliability.worker_health TO {publisher};
GRANT UPDATE(last_seen,last_success,last_failure,error_code) ON ingest_reliability.worker_health TO {publisher};

GRANT SELECT ON ingest_reliability.cases,ingest_reliability.deliveries TO {operator};
GRANT UPDATE(state,version,updated_at) ON ingest_reliability.cases TO {operator};
GRANT SELECT,INSERT ON ingest_reliability.operation_receipts TO {operator};
