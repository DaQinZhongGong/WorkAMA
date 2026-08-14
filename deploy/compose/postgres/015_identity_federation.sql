-- Enterprise SSO/SCIM baseline. All new identity-federation tables are tenant scoped.
-- This migration is additive and repeatable; secrets and bearer tokens are hash-only.
CREATE TABLE IF NOT EXISTS id_federation_sso_config (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('oidc','saml')),
    name TEXT NOT NULL,
    issuer TEXT,
    metadata_url TEXT,
    authorization_endpoint TEXT,
    client_id TEXT,
    client_secret_hash TEXT,
    client_secret_ref TEXT,
    client_secret_last4 TEXT,
    certificate_hash TEXT,
    certificate_ref TEXT,
    certificate_last4 TEXT,
    redirect_allowlist TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    mapping JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'disabled'
      CHECK (status IN ('disabled','pending','active','degraded','deleted')),
    pending_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    last_tested_at TIMESTAMPTZ,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    UNIQUE(workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_id_federation_sso_workspace_status
    ON id_federation_sso_config(workspace_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS id_federation_oidc_state (
    id TEXT PRIMARY KEY,
    config_id TEXT NOT NULL REFERENCES id_federation_sso_config(id) ON DELETE CASCADE,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    state_hash TEXT NOT NULL UNIQUE,
    nonce_hash TEXT NOT NULL,
    code_verifier_hash TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_id_federation_oidc_state_expiry
    ON id_federation_oidc_state(workspace_id, expires_at, consumed_at);

CREATE TABLE IF NOT EXISTS id_federation_scim_token (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    last_four TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked','expired')),
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_id_federation_scim_token_workspace
    ON id_federation_scim_token(workspace_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS id_federation_scim_user (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
    user_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, external_id),
    UNIQUE(workspace_id, user_id),
    UNIQUE(workspace_id, user_name)
);
CREATE INDEX IF NOT EXISTS idx_id_federation_scim_user_workspace_active
    ON id_federation_scim_user(workspace_id, active, updated_at DESC);

CREATE TABLE IF NOT EXISTS id_federation_scim_group (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_id_federation_scim_group_workspace
    ON id_federation_scim_group(workspace_id, active, updated_at DESC);

CREATE TABLE IF NOT EXISTS id_federation_scim_group_member (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL REFERENCES id_federation_scim_group(id) ON DELETE CASCADE,
    scim_user_id TEXT REFERENCES id_federation_scim_user(id) ON DELETE SET NULL,
    external_member_id TEXT NOT NULL,
    display TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(group_id, external_member_id)
);
CREATE INDEX IF NOT EXISTS idx_id_federation_scim_group_member_workspace
    ON id_federation_scim_group_member(workspace_id, group_id);
