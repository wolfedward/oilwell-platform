-- Metadata-only fingerprint transition. The explicit migration API first
-- validates every prior checksum and the version4 legacy structure digest.
-- It then appends immutable version5 with qualified-name index ordering.
-- No existing history, schema definition, grant or application row is changed.
SELECT 1;
