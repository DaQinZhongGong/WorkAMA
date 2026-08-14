-- External app HTTP test execution and bounded delivery state.
ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 1;
ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;
ALTER TABLE pf_external_app_invocation ADD COLUMN IF NOT EXISTS response_code INTEGER;

CREATE INDEX IF NOT EXISTS idx_pf_external_app_invocation_delivery
  ON pf_external_app_invocation(status, last_attempt_at, created_at);
