-- SIEM HTTP worker claim state, bounded retry metadata, and safe response summaries.
ALTER TABLE sec_siem_delivery
  ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS response_code INTEGER,
  ADD COLUMN IF NOT EXISTS response_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE sec_siem_delivery
  DROP CONSTRAINT IF EXISTS sec_siem_delivery_status_check;

ALTER TABLE sec_siem_delivery
  ADD CONSTRAINT sec_siem_delivery_status_check
    CHECK (status IN ('pending_external','delivering','retry_wait','delivered','failed','disabled'));

ALTER TABLE sec_siem_delivery
  ALTER COLUMN status SET DEFAULT 'pending_external';

CREATE INDEX IF NOT EXISTS idx_sec_siem_delivery_claimable
  ON sec_siem_delivery(status,next_attempt_at,created_at);
