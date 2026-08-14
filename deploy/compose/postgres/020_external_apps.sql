CREATE TABLE IF NOT EXISTS pf_external_app (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  provider TEXT NOT NULL CHECK (provider IN ('dify','fastgpt','ragflow')),
  endpoint TEXT NOT NULL,
  credential_hash TEXT,
  credential_last4 TEXT,
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','revoked')),
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  version INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,name)
);
CREATE INDEX IF NOT EXISTS idx_pf_external_app_workspace ON pf_external_app(workspace_id,status,updated_at DESC);

CREATE TABLE IF NOT EXISTS pf_external_app_invocation (
  id TEXT PRIMARY KEY,
  app_id TEXT NOT NULL REFERENCES pf_external_app(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  operation TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_external' CHECK (status IN ('queued','running','succeeded','failed','pending_external')),
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_code TEXT,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  UNIQUE(app_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_pf_external_app_invocation_workspace ON pf_external_app_invocation(workspace_id,created_at DESC);

CREATE TABLE IF NOT EXISTS pf_marketplace_template (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  display_name TEXT NOT NULL,
  template_type TEXT NOT NULL CHECK (template_type IN ('assistant','workflow','skill')),
  version TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
  artifact_ref TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','approved','rejected')),
  visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','public')),
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','archived')),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  reviewed_by TEXT REFERENCES id_user(id),
  reviewed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,name,version)
);
CREATE INDEX IF NOT EXISTS idx_pf_marketplace_template_public ON pf_marketplace_template(status,review_status,template_type,updated_at DESC);

CREATE TABLE IF NOT EXISTS pf_marketplace_copy (
  id TEXT PRIMARY KEY,
  template_id TEXT NOT NULL REFERENCES pf_marketplace_template(id) ON DELETE CASCADE,
  source_workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  target_workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  idempotency_key TEXT NOT NULL,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(template_id,target_workspace_id,idempotency_key)
);
