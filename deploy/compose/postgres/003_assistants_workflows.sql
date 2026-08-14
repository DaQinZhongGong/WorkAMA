-- Runtime schema is also applied by platform-api for existing local volumes.
CREATE TABLE IF NOT EXISTS pf_assistant (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  current_version_id TEXT,
  share_token_hash TEXT,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, name)
);
CREATE TABLE IF NOT EXISTS pf_assistant_version (
  id TEXT PRIMARY KEY,
  assistant_id TEXT NOT NULL REFERENCES pf_assistant(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  model_config JSONB NOT NULL DEFAULT '{}'::jsonb,
  toolset TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  dataset_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  greeting TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','retired')),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(assistant_id, version)
);
CREATE TABLE IF NOT EXISTS pf_app_run (
  id TEXT PRIMARY KEY,
  app_id TEXT NOT NULL REFERENCES pf_assistant(id) ON DELETE CASCADE,
  app_type TEXT NOT NULL DEFAULT 'assistant' CHECK (app_type IN ('assistant','workflow','agent','external')),
  version_id TEXT NOT NULL REFERENCES pf_assistant_version(id) ON DELETE RESTRICT,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  actor_id TEXT NOT NULL REFERENCES id_user(id),
  trigger TEXT NOT NULL DEFAULT 'console' CHECK (trigger IN ('console','api','share')),
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  input_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
  output_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
  error TEXT,
  credits NUMERIC,
  duration_ms INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS pf_app_run_event (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES pf_app_run(id) ON DELETE CASCADE,
  app_id TEXT NOT NULL REFERENCES pf_assistant(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(run_id, seq)
);
CREATE TABLE IF NOT EXISTS pf_workflow (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  graph JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','archived')),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, name)
);
CREATE TABLE IF NOT EXISTS pf_workflow_run (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL REFERENCES pf_workflow(id) ON DELETE CASCADE,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  input JSONB NOT NULL DEFAULT '{}'::jsonb,
  output JSONB NOT NULL DEFAULT '{}'::jsonb,
  trace JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','pending_approval','succeeded','failed','cancelled')),
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS pf_workflow_event (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES pf_workflow_run(id) ON DELETE CASCADE,
  workflow_id TEXT NOT NULL REFERENCES pf_workflow(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_pf_assistant_workspace_status ON pf_assistant(workspace_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pf_app_run_app_time ON pf_app_run(app_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pf_app_run_workspace_time ON pf_app_run(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pf_app_run_event_run_seq ON pf_app_run_event(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_pf_workflow_workspace_status ON pf_workflow(workspace_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pf_workflow_run_workflow_time ON pf_workflow_run(workflow_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pf_workflow_event_run_seq ON pf_workflow_event(run_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pf_assistant_share_token ON pf_assistant(share_token_hash) WHERE share_token_hash IS NOT NULL;
