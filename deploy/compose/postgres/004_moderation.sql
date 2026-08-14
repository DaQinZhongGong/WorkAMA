-- WorkAMA P1 moderation policy, deterministic rules, and audit evidence.
-- The API also exposes ensure_moderation_schema() for existing installations.

CREATE TABLE IF NOT EXISTS sec_moderation_policy (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    default_input_action TEXT NOT NULL DEFAULT 'log'
        CHECK (default_input_action IN ('block', 'mask', 'log')),
    default_output_action TEXT NOT NULL DEFAULT 'block'
        CHECK (default_output_action IN ('block', 'mask', 'log')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('draft', 'active', 'archived')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by TEXT REFERENCES id_user(id) ON DELETE SET NULL,
    updated_by TEXT REFERENCES id_user(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name)
);

CREATE TABLE IF NOT EXISTS sec_moderation_rule (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES sec_moderation_policy(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('sensitive_word', 'regex', 'length')),
    direction TEXT NOT NULL CHECK (direction IN ('input', 'output', 'both')),
    pattern TEXT,
    max_length INTEGER,
    action TEXT NOT NULL CHECK (action IN ('block', 'mask', 'log')),
    replacement TEXT NOT NULL DEFAULT '***',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    priority INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (kind = 'length' AND max_length IS NOT NULL AND max_length > 0)
        OR (kind <> 'length' AND pattern IS NOT NULL AND length(trim(pattern)) > 0)
    )
);

CREATE INDEX IF NOT EXISTS idx_sec_moderation_policy_workspace
    ON sec_moderation_policy(workspace_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sec_moderation_rule_policy
    ON sec_moderation_rule(policy_id, enabled, priority, id);

CREATE TABLE IF NOT EXISTS sec_moderation_audit (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    policy_id TEXT REFERENCES sec_moderation_policy(id) ON DELETE SET NULL,
    policy_version INTEGER NOT NULL,
    actor_id TEXT REFERENCES id_user(id) ON DELETE SET NULL,
    direction TEXT NOT NULL CHECK (direction IN ('input', 'output')),
    action TEXT NOT NULL CHECK (action IN ('allow', 'block', 'mask', 'log')),
    matched_rule_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    rule_hits JSONB NOT NULL DEFAULT '[]'::jsonb,
    content_hash TEXT NOT NULL,
    request_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sec_moderation_audit_workspace_time
    ON sec_moderation_audit(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sec_moderation_audit_request
    ON sec_moderation_audit(workspace_id, request_id)
    WHERE request_id IS NOT NULL;
