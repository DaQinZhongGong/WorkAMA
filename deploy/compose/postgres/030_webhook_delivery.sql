-- Production webhook delivery queue, bounded retry state, and safe attempt summaries.
ALTER TABLE pf_webhook_delivery
  ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS response_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

ALTER TABLE pf_webhook_delivery
  DROP CONSTRAINT IF EXISTS pf_webhook_delivery_status_check,
  DROP CONSTRAINT IF EXISTS pf_webhook_delivery_delivery_mode_check;

ALTER TABLE pf_webhook_delivery
  ADD CONSTRAINT pf_webhook_delivery_status_check
    CHECK (status IN ('pending','delivering','retry_wait','delivered','failed','disabled','blocked_external')),
  ADD CONSTRAINT pf_webhook_delivery_delivery_mode_check
    CHECK (delivery_mode IN ('controlled_mock','external','blocked_external'));

ALTER TABLE pf_webhook_delivery
  ALTER COLUMN status SET DEFAULT 'pending',
  ALTER COLUMN delivery_mode SET DEFAULT 'external';

UPDATE pf_webhook_delivery
SET status = 'pending'
WHERE status = 'blocked_external';

UPDATE pf_webhook_delivery
SET delivery_mode = 'external'
WHERE delivery_mode = 'blocked_external' AND status = 'pending';

CREATE INDEX IF NOT EXISTS idx_pf_webhook_delivery_claimable
  ON pf_webhook_delivery(status,next_attempt_at,created_at);
