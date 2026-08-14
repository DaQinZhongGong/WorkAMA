-- WorkAMA P1 T-M9-008 Passkey and device security foundation.
-- This migration is additive and safe to run more than once. The API module
-- exposes ensure_passkey_schema(conn) for existing development volumes.

CREATE TABLE IF NOT EXISTS id_passkey (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    credential_id TEXT NOT NULL UNIQUE,
    public_key BYTEA NOT NULL,
    sign_count BIGINT NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
    transports TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    aaguid TEXT,
    name TEXT NOT NULL DEFAULT 'Passkey',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    revoke_reason TEXT
);

CREATE TABLE IF NOT EXISTS id_passkey_challenge (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES id_user(id) ON DELETE CASCADE,
    workspace_id TEXT REFERENCES id_workspace(id) ON DELETE CASCADE,
    flow TEXT NOT NULL CHECK (flow IN ('registration', 'authentication')),
    challenge TEXT NOT NULL UNIQUE,
    rp_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_id_passkey_user_workspace
    ON id_passkey(user_id, workspace_id, revoked_at, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_id_passkey_challenge_expiry
    ON id_passkey_challenge(expires_at, consumed_at);
CREATE INDEX IF NOT EXISTS idx_id_passkey_challenge_user
    ON id_passkey_challenge(user_id, flow, created_at DESC);
