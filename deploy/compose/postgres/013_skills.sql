-- T-M7-007 skill package metadata and workspace installation state.
-- Package bytes remain in a controlled artifact store; this schema stores only
-- validated manifest metadata, a content hash, and non-secret source identity.
CREATE TABLE IF NOT EXISTS ag_skill (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    publisher TEXT NOT NULL,
    name TEXT NOT NULL,
    semver TEXT NOT NULL,
    manifest JSONB NOT NULL,
    artifact_ref TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('mock', 'local')),
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    signature_status TEXT NOT NULL DEFAULT 'not_verified'
        CHECK (signature_status IN ('not_verified', 'verified', 'invalid', 'unsupported')),
    risk_level TEXT NOT NULL DEFAULT 'low'
        CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'needs_review', 'approved', 'rejected')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'blocked', 'revoked')),
    review_reason TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, publisher, name, semver)
);

CREATE TABLE IF NOT EXISTS ag_skill_install (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL REFERENCES ag_skill(id) ON DELETE CASCADE,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'disabled'
        CHECK (status IN ('disabled', 'enabled', 'blocked')),
    grants JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    installed_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, skill_id),
    UNIQUE (workspace_id, idempotency_key_hash)
);

CREATE INDEX IF NOT EXISTS idx_ag_skill_workspace_status
    ON ag_skill(workspace_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ag_skill_install_workspace_state
    ON ag_skill_install(workspace_id, enabled, updated_at DESC);
