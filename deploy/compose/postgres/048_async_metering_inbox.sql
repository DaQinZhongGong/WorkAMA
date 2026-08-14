-- v7.125 P1 async metering idempotency observability
-- Adds a direct request_id column and index to ops_inbox so the billing
-- observability endpoint can look up metering events by gateway request id
-- without scanning the JSONB payload.

ALTER TABLE ops_inbox ADD COLUMN IF NOT EXISTS request_id TEXT;
CREATE INDEX IF NOT EXISTS idx_ops_inbox_request_id ON ops_inbox(request_id);
