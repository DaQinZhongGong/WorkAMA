-- WorkAMA P1 enterprise identity foundation.
-- This migration is additive and safe to run more than once. The API module
-- exposes ensure_enterprise_schema(conn) for existing development volumes.

ALTER TABLE id_org ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE id_org ADD COLUMN IF NOT EXISTS deletion_requested_at TIMESTAMPTZ;
ALTER TABLE id_org ADD COLUMN IF NOT EXISTS deletion_scheduled_at TIMESTAMPTZ;
ALTER TABLE id_org ADD COLUMN IF NOT EXISTS deletion_cancelled_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS id_service_account (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    owner_user_id TEXT NOT NULL REFERENCES id_user(id),
    purpose TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked', 'expired')),
    expires_at TIMESTAMPTZ,
    network_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    scopes TEXT[] NOT NULL DEFAULT ARRAY['platform:read']::text[],
    active_credential_version INTEGER NOT NULL DEFAULT 1 CHECK (active_credential_version > 0),
    last_used_at TIMESTAMPTZ,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    create_idempotency_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, name)
);

CREATE INDEX IF NOT EXISTS idx_id_service_account_org_status
    ON id_service_account(org_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_id_service_account_workspace_status
    ON id_service_account(workspace_id, status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_service_account_idempotency
    ON id_service_account(workspace_id, create_idempotency_key)
    WHERE create_idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS id_service_account_credential (
    id TEXT PRIMARY KEY,
    service_account_id TEXT NOT NULL REFERENCES id_service_account(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    token_hash TEXT NOT NULL UNIQUE,
    last_four TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'rotated', 'revoked')),
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    revoke_reason TEXT,
    UNIQUE(service_account_id, version)
);

CREATE INDEX IF NOT EXISTS idx_id_service_account_credential_active
    ON id_service_account_credential(service_account_id, status, version DESC);

CREATE TABLE IF NOT EXISTS id_org_owner_transfer (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    from_owner_user_id TEXT NOT NULL REFERENCES id_user(id),
    to_owner_user_id TEXT NOT NULL REFERENCES id_user(id),
    initiated_by TEXT NOT NULL REFERENCES id_user(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'cancelled', 'expired')),
    confirmation_token_hash TEXT UNIQUE,
    action_hash TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_by TEXT REFERENCES id_user(id),
    confirmed_at TIMESTAMPTZ,
    cancelled_by TEXT REFERENCES id_user(id),
    cancelled_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_id_org_owner_transfer_pending
    ON id_org_owner_transfer(org_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_id_org_owner_transfer_org_time
    ON id_org_owner_transfer(org_id, created_at DESC);

CREATE TABLE IF NOT EXISTS id_org_owner_transfer_fact (
    id TEXT PRIMARY KEY,
    transfer_id TEXT NOT NULL REFERENCES id_org_owner_transfer(id) ON DELETE CASCADE,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    step SMALLINT NOT NULL CHECK (step IN (1, 2)),
    fact_type TEXT NOT NULL CHECK (fact_type IN ('initiated', 'confirmed')),
    actor_user_id TEXT NOT NULL REFERENCES id_user(id),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(transfer_id, step)
);

CREATE INDEX IF NOT EXISTS idx_id_org_owner_transfer_fact_org_time
    ON id_org_owner_transfer_fact(org_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS id_org_deletion_request (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    requested_by TEXT NOT NULL REFERENCES id_user(id),
    status TEXT NOT NULL DEFAULT 'retention'
        CHECK (status IN ('retention', 'cancelled', 'deleting', 'deleted')),
    reason TEXT NOT NULL DEFAULT '',
    retention_until TIMESTAMPTZ NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_by TEXT REFERENCES id_user(id),
    cancelled_at TIMESTAMPTZ,
    cancel_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_id_org_deletion_request_active
    ON id_org_deletion_request(org_id)
    WHERE status IN ('retention', 'deleting');
CREATE INDEX IF NOT EXISTS idx_id_org_deletion_request_org_time
    ON id_org_deletion_request(org_id, created_at DESC);

CREATE TABLE IF NOT EXISTS id_enterprise_audit_event (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    actor_user_id TEXT REFERENCES id_user(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_id_enterprise_audit_org_time
    ON id_enterprise_audit_event(org_id, occurred_at DESC);
