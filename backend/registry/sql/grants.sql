-- Placeholders are psycopg Identifier objects, never string-interpolated SQL.
GRANT USAGE ON SCHEMA device_registry TO {writer}, {reader};
GRANT SELECT ON ALL TABLES IN SCHEMA device_registry TO {writer}, {reader};
GRANT INSERT ON device_registry.wells, device_registry.devices,
    device_registry.device_state_periods, device_registry.well_bindings,
    device_registry.point_map_versions, device_registry.point_map_assignments,
    device_registry.audit_events, device_registry.operation_receipts TO {writer};
GRANT UPDATE (name,status,version,updated_at) ON device_registry.wells TO {writer};
GRANT UPDATE (name,manufacturer,model,serial_number,check_note,usage,usage_locked,status,version,updated_at)
    ON device_registry.devices TO {writer};
GRANT UPDATE (valid_to) ON device_registry.device_state_periods TO {writer};
GRANT UPDATE (valid_to) ON device_registry.well_bindings TO {writer};
GRANT UPDATE (valid_to) ON device_registry.point_map_assignments TO {writer};
GRANT UPDATE (status,reviewed_at,review_event_id) ON device_registry.point_map_versions TO {writer};
GRANT UPDATE (revision) ON device_registry.registry_head TO {writer};
GRANT USAGE ON SCHEMA public TO {reader};
GRANT SELECT ON public.ingest_messages, public.telemetry_samples TO {reader};
