-- WorkAMA P1 MCP registry. The API exposes ensure_mcp_schema(conn) with the
-- same additive contract for existing development volumes.

CREATE TABLE IF NOT EXISTS ag_mcp_server (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    transport TEXT NOT NULL CHECK (transport IN ('stdio','sse','streamable_http')),
    endpoint_or_command TEXT NOT NULL,
    auth_type TEXT NOT NULL DEFAULT 'none' CHECK (auth_type IN ('none','oauth','bearer')),
    auth_ref TEXT,
    protocol_version TEXT NOT NULL,
    server_identity JSONB NOT NULL DEFAULT '{}'::jsonb,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    schema_hash TEXT NOT NULL DEFAULT '',
    roots JSONB NOT NULL DEFAULT '[]'::jsonb,
    approval_policy TEXT NOT NULL DEFAULT 'explicit'
        CHECK (approval_policy IN ('explicit','workspace','always')),
    risk_policy JSONB NOT NULL DEFAULT '{"source":"workama","sensitive_default":"approval"}'::jsonb,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','validating','enabled','degraded','circuit_open','half_open','disabled','deleted')),
    last_test JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_tested_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ag_mcp_server_workspace_name_active
    ON ag_mcp_server(workspace_id, name)
    WHERE status <> 'deleted';
CREATE INDEX IF NOT EXISTS idx_ag_mcp_server_workspace_status
    ON ag_mcp_server(workspace_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ag_mcp_server_org_status
    ON ag_mcp_server(org_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ag_mcp_server_schema_hash
    ON ag_mcp_server(workspace_id, schema_hash);
