CREATE TABLE IF NOT EXISTS ag_design_project (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  slug TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  canvas_width INTEGER NOT NULL DEFAULT 1440 CHECK (canvas_width > 0 AND canvas_width <= 8192),
  canvas_height INTEGER NOT NULL DEFAULT 900 CHECK (canvas_height > 0 AND canvas_height <= 8192),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_ag_design_project_workspace ON ag_design_project(workspace_id,status,updated_at DESC);

CREATE TABLE IF NOT EXISTS ag_design_asset (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('image','prototype','canvas')),
  content_type TEXT NOT NULL,
  artifact_ref TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
  provenance_hash TEXT NOT NULL,
  parent_asset_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready','deleted')),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ag_design_asset_project ON ag_design_asset(project_id,created_at DESC);

CREATE TABLE IF NOT EXISTS ag_design_job (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  operation TEXT NOT NULL CHECK (operation IN ('generate','edit','prototype')),
  prompt_hash TEXT NOT NULL,
  source_refs TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  parent_asset_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  output_format TEXT NOT NULL CHECK (output_format IN ('svg','png','json')),
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  asset_id TEXT REFERENCES ag_design_asset(id) ON DELETE SET NULL,
  error_code TEXT,
  error_message TEXT,
  idempotency_key TEXT,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  UNIQUE(project_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_ag_design_job_workspace_time ON ag_design_job(workspace_id,created_at DESC);
