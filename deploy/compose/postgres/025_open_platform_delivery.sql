-- Controlled webhook delivery metadata; public endpoints remain pending until a worker is enabled.
ALTER TABLE pf_webhook_delivery
  ADD COLUMN IF NOT EXISTS delivery_mode TEXT NOT NULL DEFAULT 'blocked_external';
ALTER TABLE pf_webhook_delivery
  ADD COLUMN IF NOT EXISTS signature TEXT;
ALTER TABLE pf_webhook_delivery
  ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_pf_webhook_delivery_mode
  ON pf_webhook_delivery(delivery_mode,status,created_at DESC);
