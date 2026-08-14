-- T-M3-004 controlled enterprise knowledge connector projections.
-- Connector credentials are represented by a safe reference and HMAC only;
-- this migration never stores provider secrets or arbitrary remote URLs.
CREATE TABLE IF NOT EXISTS pf_connector (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('mock','local')),
    auth_mode TEXT NOT NULL CHECK (auth_mode IN ('none','oauth','service_account')),
    endpoint_ref TEXT NOT NULL,
    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    credential_ref TEXT,
    credential_hash TEXT,
    status TEXT NOT NULL DEFAULT 'disabled' CHECK (status IN ('active','disabled','pending','revoked')),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    source_cursor JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_sync_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, name)
);

CREATE TABLE IF NOT EXISTS pf_connector_run (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    connector_id TEXT NOT NULL REFERENCES pf_connector(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK (mode IN ('full','incremental')),
    idempotency_key TEXT NOT NULL,
    idempotency_key_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    source_cursor_before JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_cursor_after JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','succeeded','failed','unsupported','cancelled')),
    execution_status TEXT NOT NULL DEFAULT 'pending' CHECK (execution_status IN ('pending','executed','unsupported')),
    executed BOOLEAN NOT NULL DEFAULT FALSE,
    documents_seen INTEGER NOT NULL DEFAULT 0,
    documents_upserted INTEGER NOT NULL DEFAULT 0,
    documents_tombstoned INTEGER NOT NULL DEFAULT 0,
    documents_revoked INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    created_by TEXT REFERENCES id_user(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE(connector_id, idempotency_key_hash)
);

CREATE TABLE IF NOT EXISTS pf_connector_document (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    connector_id TEXT NOT NULL REFERENCES pf_connector(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_etag TEXT,
    source_updated_at TIMESTAMPTZ,
    title TEXT NOT NULL,
    content TEXT,
    content_ref TEXT,
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    acl JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','tombstone','revoked')),
    last_run_id TEXT REFERENCES pf_connector_run(id) ON DELETE SET NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(connector_id, source_id)
);

CREATE TABLE IF NOT EXISTS pf_connector_document_acl (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    connector_id TEXT NOT NULL REFERENCES pf_connector(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES pf_connector_document(id) ON DELETE CASCADE,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user','group','role')),
    principal_id TEXT NOT NULL,
    effect TEXT NOT NULL DEFAULT 'allow' CHECK (effect IN ('allow','deny')),
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(document_id, principal_type, principal_id)
);

CREATE TABLE IF NOT EXISTS pf_connector_identity_mapping (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    connector_id TEXT NOT NULL REFERENCES pf_connector(id) ON DELETE CASCADE,
    external_type TEXT NOT NULL CHECK (external_type IN ('user','group','role')),
    external_id TEXT NOT NULL,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user','group','role')),
    principal_id TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(connector_id, external_type, external_id, principal_type, principal_id)
);

CREATE INDEX IF NOT EXISTS idx_pf_connector_workspace_status
    ON pf_connector(workspace_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pf_connector_run_workspace_time
    ON pf_connector_run(workspace_id, connector_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pf_connector_document_visible
    ON pf_connector_document(workspace_id, connector_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pf_connector_acl_principal
    ON pf_connector_document_acl(workspace_id, principal_type, principal_id);
CREATE INDEX IF NOT EXISTS idx_pf_connector_mapping_user
    ON pf_connector_identity_mapping(workspace_id, connector_id, principal_type, principal_id, enabled);
