CREATE TABLE IF NOT EXISTS id_federation_saml_replay (
    id TEXT PRIMARY KEY,
    config_id TEXT NOT NULL REFERENCES id_federation_sso_config(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    response_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(config_id, response_id)
);
CREATE INDEX IF NOT EXISTS idx_id_federation_saml_replay_expiry
    ON id_federation_saml_replay(config_id, expires_at);
