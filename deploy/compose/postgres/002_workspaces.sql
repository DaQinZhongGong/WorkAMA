-- WorkAMA P1 organization, multi-workspace, invitation and context foundation.
-- This migration is additive and safe to run more than once. The API also
-- exposes ensure_workspaces_schema(conn) for existing development volumes.

ALTER TABLE id_workspace ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE id_workspace ADD COLUMN IF NOT EXISTS created_by TEXT REFERENCES id_user(id);
ALTER TABLE id_workspace ADD COLUMN IF NOT EXISTS create_idempotency_key TEXT;

ALTER TABLE id_member ALTER COLUMN workspace_id DROP NOT NULL;
ALTER TABLE id_member ADD COLUMN IF NOT EXISTS invited_by TEXT REFERENCES id_user(id);
ALTER TABLE id_member ADD COLUMN IF NOT EXISTS joined_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE id_member DROP CONSTRAINT IF EXISTS id_member_workspace_id_user_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_id_member_org_workspace_user
    ON id_member(org_id, workspace_id, user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_workspace_create_idempotency
    ON id_workspace(org_id, create_idempotency_key)
    WHERE create_idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS id_invitation (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    email_normalized TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    token_hash TEXT NOT NULL UNIQUE,
    idempotency_key TEXT,
    invited_by TEXT NOT NULL REFERENCES id_user(id),
    expires_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'revoked', 'expired')),
    accepted_by TEXT REFERENCES id_user(id),
    accepted_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_id_invitation_workspace_status
    ON id_invitation(workspace_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_id_invitation_email_status
    ON id_invitation(email_normalized, status, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_invitation_active_target
    ON id_invitation(workspace_id, email_normalized)
    WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_invitation_idempotency
    ON id_invitation(workspace_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS id_workspace_audit (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT REFERENCES id_workspace(id) ON DELETE CASCADE,
    invitation_id TEXT REFERENCES id_invitation(id) ON DELETE SET NULL,
    actor_user_id TEXT REFERENCES id_user(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    request_id TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_id_workspace_audit_tenant_time
    ON id_workspace_audit(org_id, workspace_id, occurred_at DESC);
