-- Explicit migration only. No production catalog rows or runtime auto-DDL.
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE SCHEMA device_registry;
REVOKE ALL ON SCHEMA device_registry FROM PUBLIC;

CREATE TABLE device_registry.schema_migrations (
    version bigint PRIMARY KEY CHECK (version > 0),
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE device_registry.wells (
    id uuid PRIMARY KEY,
    area_code text NOT NULL CHECK (area_code ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    well_code text NOT NULL CHECK (well_code ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (area_code, well_code)
);

CREATE TABLE device_registry.devices (
    id uuid PRIMARY KEY,
    gateway_id text NOT NULL CHECK (gateway_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    device_id text NOT NULL CHECK (device_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    manufacturer text NOT NULL DEFAULT '' CHECK (char_length(manufacturer) <= 120),
    model text NOT NULL DEFAULT '' CHECK (char_length(model) <= 120),
    serial_number text NOT NULL DEFAULT '' CHECK (char_length(serial_number) <= 128),
    check_note text NOT NULL DEFAULT '' CHECK (char_length(check_note) <= 1000),
    transport text NOT NULL CHECK (transport IN ('modbus_tcp','modbus_rtu','mqtt_direct')),
    usage text NOT NULL CHECK (usage IN ('commissioning','production')),
    usage_locked boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','suspended','retired')),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (gateway_id, device_id)
);

CREATE TABLE device_registry.audit_events (
    id uuid PRIMARY KEY,
    actor jsonb NOT NULL CHECK (jsonb_typeof(actor)='object'),
    action text NOT NULL,
    object_type text NOT NULL,
    object_id uuid NOT NULL,
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 2 AND 500),
    before_data jsonb NOT NULL CHECK (jsonb_typeof(before_data)='object'),
    after_data jsonb NOT NULL CHECK (jsonb_typeof(after_data)='object'),
    event_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    request_id uuid NOT NULL
);
CREATE INDEX registry_audit_object_time ON device_registry.audit_events(object_id,event_at DESC,id DESC);
CREATE INDEX registry_audit_time ON device_registry.audit_events(event_at DESC,id DESC);

CREATE TABLE device_registry.device_state_periods (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id uuid NOT NULL REFERENCES device_registry.devices(id),
    status text NOT NULL CHECK (status IN ('pending','active','suspended','retired')),
    valid_from timestamptz NOT NULL CHECK (isfinite(valid_from)),
    valid_to timestamptz,
    audit_event_id uuid NOT NULL REFERENCES device_registry.audit_events(id) DEFERRABLE INITIALLY DEFERRED,
    CHECK (valid_to IS NULL OR (isfinite(valid_to) AND valid_to > valid_from)),
    CONSTRAINT state_no_overlap EXCLUDE USING gist
        (device_id WITH =, tstzrange(valid_from,valid_to,'[)') WITH &&)
);

CREATE TABLE device_registry.well_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id uuid NOT NULL REFERENCES device_registry.devices(id),
    well_id uuid NOT NULL REFERENCES device_registry.wells(id),
    valid_from timestamptz NOT NULL CHECK (isfinite(valid_from)),
    valid_to timestamptz,
    audit_event_id uuid NOT NULL REFERENCES device_registry.audit_events(id) DEFERRABLE INITIALLY DEFERRED,
    CHECK (valid_to IS NULL OR (isfinite(valid_to) AND valid_to > valid_from)),
    CONSTRAINT binding_no_overlap EXCLUDE USING gist
        (device_id WITH =, tstzrange(valid_from,valid_to,'[)') WITH &&)
);
CREATE INDEX registry_binding_well_time ON device_registry.well_bindings(well_id,valid_from DESC);

CREATE TABLE device_registry.point_map_versions (
    device_id uuid NOT NULL REFERENCES device_registry.devices(id),
    version bigint NOT NULL CHECK (version > 0),
    parent_version bigint,
    transport text NOT NULL CHECK (transport IN ('modbus_tcp','modbus_rtu','mqtt_direct')),
    canonical_rule text NOT NULL,
    canonical_bytes bytea NOT NULL,
    content jsonb NOT NULL CHECK (jsonb_typeof(content)='object'),
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','rejected')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_event_id uuid NOT NULL REFERENCES device_registry.audit_events(id) DEFERRABLE INITIALLY DEFERRED,
    reviewed_at timestamptz,
    review_event_id uuid REFERENCES device_registry.audit_events(id) DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY (device_id,version),
    FOREIGN KEY (device_id,parent_version) REFERENCES device_registry.point_map_versions(device_id,version),
    CHECK (parent_version IS NULL OR (parent_version > 0 AND parent_version < version)),
    CHECK ((status='draft' AND reviewed_at IS NULL AND review_event_id IS NULL) OR
           (status IN ('approved','rejected') AND reviewed_at IS NOT NULL AND review_event_id IS NOT NULL))
);

CREATE TABLE device_registry.point_map_assignments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id uuid NOT NULL,
    map_version bigint NOT NULL CHECK (map_version > 0),
    valid_from timestamptz NOT NULL CHECK (isfinite(valid_from)),
    valid_to timestamptz,
    audit_event_id uuid NOT NULL REFERENCES device_registry.audit_events(id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (device_id,map_version) REFERENCES device_registry.point_map_versions(device_id,version),
    CHECK (valid_to IS NULL OR (isfinite(valid_to) AND valid_to > valid_from)),
    CONSTRAINT assignment_no_overlap EXCLUDE USING gist
        (device_id WITH =, tstzrange(valid_from,valid_to,'[)') WITH &&)
);

CREATE TABLE device_registry.operation_receipts (
    actor_domain text NOT NULL,
    actor_id text NOT NULL,
    key uuid NOT NULL,
    request_hash char(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    result jsonb NOT NULL CHECK (jsonb_typeof(result)='object'),
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (actor_domain,actor_id,key)
);

CREATE TABLE device_registry.registry_head (
    singleton smallint PRIMARY KEY CHECK (singleton=1),
    revision bigint NOT NULL CHECK (revision > 0)
);
INSERT INTO device_registry.registry_head(singleton,revision) VALUES (1,1);

CREATE FUNCTION device_registry.close_period_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable history'; END IF;
    IF OLD.valid_to IS NOT NULL OR NEW.valid_to IS NULL OR NEW.valid_to <= OLD.valid_from
       OR (to_jsonb(NEW)-'valid_to') IS DISTINCT FROM (to_jsonb(OLD)-'valid_to')
    THEN RAISE EXCEPTION 'only closing an open period is allowed'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER state_close_only BEFORE UPDATE OR DELETE ON device_registry.device_state_periods
    FOR EACH ROW EXECUTE FUNCTION device_registry.close_period_only();
CREATE TRIGGER binding_close_only BEFORE UPDATE OR DELETE ON device_registry.well_bindings
    FOR EACH ROW EXECUTE FUNCTION device_registry.close_period_only();
CREATE TRIGGER assignment_close_only BEFORE UPDATE OR DELETE ON device_registry.point_map_assignments
    FOR EACH ROW EXECUTE FUNCTION device_registry.close_period_only();

CREATE FUNCTION device_registry.immutable_row() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable row';
END $$;
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON device_registry.audit_events
    FOR EACH ROW EXECUTE FUNCTION device_registry.immutable_row();
CREATE TRIGGER receipt_append_only BEFORE UPDATE OR DELETE ON device_registry.operation_receipts
    FOR EACH ROW EXECUTE FUNCTION device_registry.immutable_row();

CREATE FUNCTION device_registry.review_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable point map'; END IF;
    IF OLD.status <> 'draft' OR NEW.status NOT IN ('approved','rejected')
       OR NEW.reviewed_at IS NULL OR NEW.review_event_id IS NULL
       OR (to_jsonb(NEW)-ARRAY['status','reviewed_at','review_event_id']) IS DISTINCT FROM
          (to_jsonb(OLD)-ARRAY['status','reviewed_at','review_event_id'])
    THEN RAISE EXCEPTION 'point map can only be reviewed once'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER map_review_once BEFORE UPDATE OR DELETE ON device_registry.point_map_versions
    FOR EACH ROW EXECUTE FUNCTION device_registry.review_once();

CREATE FUNCTION device_registry.device_identity_fixed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'device identities cannot be reused'; END IF;
    IF OLD.status='retired' OR NEW.id IS DISTINCT FROM OLD.id
       OR NEW.gateway_id IS DISTINCT FROM OLD.gateway_id
       OR NEW.device_id IS DISTINCT FROM OLD.device_id
       OR NEW.transport IS DISTINCT FROM OLD.transport
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (OLD.usage_locked AND NOT NEW.usage_locked)
       OR (NEW.usage IS DISTINCT FROM OLD.usage AND (OLD.usage_locked OR OLD.status <> 'pending'))
    THEN RAISE EXCEPTION 'immutable device identity or retired device'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER device_identity_fixed BEFORE UPDATE OR DELETE ON device_registry.devices
    FOR EACH ROW EXECUTE FUNCTION device_registry.device_identity_fixed();

CREATE FUNCTION device_registry.well_identity_fixed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'well identities cannot be reused'; END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.area_code IS DISTINCT FROM OLD.area_code
       OR NEW.well_code IS DISTINCT FROM OLD.well_code OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (OLD.status='archived' AND NEW.status <> 'archived')
    THEN RAISE EXCEPTION 'immutable well identity'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER well_identity_fixed BEFORE UPDATE OR DELETE ON device_registry.wells
    FOR EACH ROW EXECUTE FUNCTION device_registry.well_identity_fixed();

REVOKE ALL ON ALL TABLES IN SCHEMA device_registry FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA device_registry FROM PUBLIC;
