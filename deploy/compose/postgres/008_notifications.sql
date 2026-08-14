-- WorkAMA P1 T-M9-007 notification preferences and delivery hardening.
-- Existing notification facts and routes remain compatible with
-- pending/sent/failed; new states and columns are additive.

CREATE TABLE IF NOT EXISTS id_notification_preference (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL DEFAULT '*',
    channel TEXT NOT NULL CHECK (channel IN ('in_app', 'email', 'webhook')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    quiet_start TIME,
    quiet_end TIME,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, workspace_id, event_type, channel)
);

CREATE INDEX IF NOT EXISTS idx_id_notification_preference_scope
    ON id_notification_preference(user_id, workspace_id, event_type);

CREATE TABLE IF NOT EXISTS id_notification_webhook (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL REFERENCES id_user(id),
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    events TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_id_notification_webhook_workspace
    ON id_notification_webhook(workspace_id, status, created_at DESC);

ALTER TABLE id_notification_delivery
    ADD COLUMN IF NOT EXISTS webhook_id TEXT REFERENCES id_notification_webhook(id) ON DELETE CASCADE;
ALTER TABLE id_notification_delivery
    ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 5;
ALTER TABLE id_notification_delivery
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE id_notification_delivery
    ADD COLUMN IF NOT EXISTS request_hash TEXT;
ALTER TABLE id_notification_delivery
    ADD COLUMN IF NOT EXISTS response_code INTEGER;
ALTER TABLE id_notification_delivery
    ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;
ALTER TABLE id_notification_delivery
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;

ALTER TABLE id_notification_delivery
    DROP CONSTRAINT IF EXISTS id_notification_delivery_status_check;
ALTER TABLE id_notification_delivery
    ADD CONSTRAINT id_notification_delivery_status_check
    CHECK (status IN ('pending', 'sending', 'accepted', 'delivered', 'sent', 'failed', 'retry_wait', 'permanent_failed', 'suppressed'));

ALTER TABLE id_notification_delivery
    DROP CONSTRAINT IF EXISTS id_notification_delivery_notification_id_channel_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_notification_delivery_target
    ON id_notification_delivery(notification_id, channel, COALESCE(webhook_id, ''));
CREATE INDEX IF NOT EXISTS idx_id_notification_delivery_due
    ON id_notification_delivery(channel, status, next_attempt_at, created_at);
