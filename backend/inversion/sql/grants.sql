REVOKE ALL ON SCHEMA inversion FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA inversion FROM PUBLIC,{reader},{operator};
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA inversion FROM PUBLIC,{reader},{operator};
GRANT USAGE ON SCHEMA inversion TO {reader},{operator};
GRANT SELECT ON ALL TABLES IN SCHEMA inversion TO {reader},{operator};
GRANT INSERT ON inversion.assemblies,inversion.assembly_versions,inversion.assignments,inversion.assignment_events,inversion.parameter_versions,inversion.reviews,
 inversion.model_versions,inversion.model_reviews,inversion.selections,inversion.run_requests,inversion.run_state,inversion.run_events,
 inversion.attempts,inversion.attempt_events,inversion.results,inversion.result_chunks,inversion.result_reviews,
 inversion.restrictions,inversion.audit,inversion.receipts,inversion.reservations TO {operator};
GRANT UPDATE(version) ON inversion.state_head TO {operator};
-- PostgreSQL row locks require an UPDATE privilege. Immutable triggers still reject writes.
GRANT UPDATE(id) ON inversion.assemblies,inversion.assembly_versions,inversion.parameter_versions,inversion.model_versions,
 inversion.run_requests,inversion.results TO {operator};
GRANT UPDATE(queued,running,reserved_bytes,used_bytes,version) ON inversion.capacity TO {operator};
GRANT UPDATE(state,used_bytes,version) ON inversion.reservations TO {operator};
GRANT UPDATE(version,state,generation,owner_id,deadline,heartbeat_at,reason_code) ON inversion.run_state TO {operator};
