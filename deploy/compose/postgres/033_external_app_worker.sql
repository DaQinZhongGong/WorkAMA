-- Additive queue and lease state for explicitly enabled external HTTP calls.
ALTER TABLE pf_external_app_invocation
  ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS execution_mode TEXT NOT NULL DEFAULT 'external_pending',
  ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS lease_owner TEXT,
  ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;

ALTER TABLE pf_external_app_invocation
  DROP CONSTRAINT IF EXISTS pf_external_app_invocation_execution_mode_check;

ALTER TABLE pf_external_app_invocation
  ADD CONSTRAINT pf_external_app_invocation_execution_mode_check
  CHECK (execution_mode IN ('controlled_mock','http_test','external_http','external_pending'));

UPDATE pf_external_app_invocation i
SET execution_mode = CASE
  WHEN a.endpoint ~ '^(mock|local)://(dify|fastgpt|ragflow)(/|$)' THEN 'controlled_mock'
  WHEN a.endpoint ~ '^https?://' AND a.config->>'execution_mode' IN ('http_test','external_http') THEN a.config->>'execution_mode'
  ELSE 'external_pending'
END
FROM pf_external_app a
WHERE a.id=i.app_id AND i.execution_mode='external_pending';

CREATE INDEX IF NOT EXISTS idx_pf_external_app_invocation_worker
  ON pf_external_app_invocation(execution_mode,status,next_attempt_at,lease_expires_at,created_at);
