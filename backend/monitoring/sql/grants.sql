-- schema: public
-- public schema USAGE must be granted by the database/schema owner, not the table owner.
GRANT SELECT ON public.telemetry_samples TO {reader};
GRANT SELECT(message_id,topic,gateway_id,device_id,boot_id,sequence_first,sequence_last,created_at,received_at,payload_sha256)
    ON public.ingest_messages TO {reader};
-- schema: device_registry
GRANT USAGE ON SCHEMA device_registry TO {reader};
GRANT SELECT ON device_registry.wells,device_registry.devices,device_registry.registry_head,
    device_registry.device_state_periods,device_registry.well_bindings,device_registry.point_map_assignments TO {reader};
GRANT SELECT(device_id,version,parent_version,transport,canonical_rule,content,sha256,status,created_at,created_event_id,reviewed_at,review_event_id)
    ON device_registry.point_map_versions TO {reader};
-- schema: ingest_reliability
GRANT USAGE ON SCHEMA ingest_reliability TO {reader};
GRANT SELECT ON ingest_reliability.case_metadata,ingest_reliability.receipt_metadata TO {reader};
