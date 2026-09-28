-- Deliberately narrow owner operation: ingestion cannot issue arbitrary case
-- state changes. Reopening and its fixed system audit are one caller transaction.
CREATE FUNCTION ingest_reliability.reopen_repeated_case(p_fact UUID, p_instant TIMESTAMPTZ)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE previous ingest_reliability.cases%ROWTYPE;
BEGIN
    IF p_instant IS NULL OR NOT isfinite(p_instant) THEN
        RAISE EXCEPTION 'repeat observation time invalid' USING ERRCODE='22007';
    END IF;
    SELECT c.* INTO previous FROM ingest_reliability.cases c
        JOIN ingest_reliability.fact_observations o USING(fact_id)
        WHERE c.fact_id=p_fact AND c.state='closed' AND o.duplicate_count>0
        FOR UPDATE OF c;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    UPDATE ingest_reliability.cases SET state='open',version=previous.version+1,
        updated_at=greatest(previous.updated_at,p_instant) WHERE case_id=previous.case_id;
    INSERT INTO ingest_reliability.audit_events(fact_id,case_id,actor,action,reason,before_data,after_data,recorded_at)
        VALUES (p_fact,previous.case_id,
            jsonb_build_object('domain','system','user_id','ingest-receiver','username','ingest-receiver',
                'display_name','中心接收程序','role_code','system'),
            'case.repeat_reopen','已关闭隔离报文再次出现，自动重新打开',
            jsonb_build_object('state',previous.state,'version',previous.version::text),
            jsonb_build_object('state','open','version',(previous.version+1)::text),p_instant);
    RETURN TRUE;
END $$;
REVOKE ALL ON FUNCTION ingest_reliability.reopen_repeated_case(UUID,TIMESTAMPTZ) FROM PUBLIC;
