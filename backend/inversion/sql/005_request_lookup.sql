-- Bounded actor-scoped correlation for read-only lost-response recovery.
CREATE INDEX audit_actor_request ON inversion.audit
 ((actor->>'domain'), (actor->>'user_id'), request_key);
