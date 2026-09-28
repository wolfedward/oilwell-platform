-- Metadata is separate from raw evidence. Runtime reader roles never gain
-- direct SELECT on facts, receipt bodies or the mutable worker table.
CREATE VIEW ingest_reliability.worker_health_metadata AS
    SELECT worker_id,last_seen,last_success,last_failure,error_code
    FROM ingest_reliability.worker_health;
REVOKE ALL ON ingest_reliability.worker_health_metadata FROM PUBLIC;

-- Returns one selected fact only and writes its access audit in the same
-- transaction. The service must commit before releasing bytes to its caller.
CREATE FUNCTION ingest_reliability.audited_raw(p_fact UUID, p_actor JSONB, p_preview BOOLEAN)
RETURNS TABLE(fact_id UUID,payload_sha256 TEXT,payload_bytes INTEGER,raw_bytes BYTEA)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE item ingest_reliability.facts%ROWTYPE; case_key UUID; receipt_key UUID;
BEGIN
    IF p_fact IS NULL OR p_preview IS NULL OR p_actor IS NULL OR jsonb_typeof(p_actor)<>'object' THEN
        RAISE EXCEPTION 'raw access arguments invalid' USING ERRCODE='22023';
    END IF;
    IF NOT (p_actor ?& ARRAY['domain','user_id','username','display_name','role_code'])
        OR p_actor-ARRAY['domain','user_id','username','display_name','role_code']<>'{}'::jsonb
        OR EXISTS(SELECT 1 FROM jsonb_each(p_actor) AS f
            WHERE jsonb_typeof(f.value)<>'string' OR length(f.value#>>'{}') NOT BETWEEN 1 AND 128
                OR length(btrim(f.value#>>'{}'))=0
                OR (f.value#>>'{}') ~ ('['||chr(1)||'-'||chr(31)||chr(127)||'-'||chr(159)||']')) THEN
        RAISE EXCEPTION 'raw access actor invalid' USING ERRCODE='22023';
    END IF;
    SELECT f.* INTO item FROM ingest_reliability.facts f WHERE f.fact_id=p_fact;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT c.case_id INTO case_key FROM ingest_reliability.cases c WHERE c.fact_id=p_fact;
    SELECT r.receipt_id INTO receipt_key FROM ingest_reliability.receipts r WHERE r.fact_id=p_fact;
    INSERT INTO ingest_reliability.audit_events(fact_id,case_id,receipt_id,actor,action,reason,before_data,after_data,recorded_at)
        VALUES(p_fact,case_key,receipt_key,p_actor,
            CASE WHEN p_preview THEN 'raw.preview' ELSE 'raw.download' END,
            CASE WHEN p_preview THEN '经授权查看原文预览' ELSE '经授权下载完整原文' END,
            jsonb_build_object('payload_sha256',item.payload_sha256,'payload_bytes',item.payload_bytes::text),
            jsonb_build_object('shown_bytes',(CASE WHEN p_preview THEN least(item.payload_bytes,4096) ELSE item.payload_bytes END)::text),
            clock_timestamp());
    RETURN QUERY SELECT item.fact_id,item.payload_sha256,item.payload_bytes,
        CASE WHEN p_preview THEN substring(item.raw_payload FROM 1 FOR 4096) ELSE item.raw_payload END;
END $$;
REVOKE ALL ON FUNCTION ingest_reliability.audited_raw(UUID,JSONB,BOOLEAN) FROM PUBLIC;
