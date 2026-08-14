-- AMA-Design image generation jobs and canvas state
CREATE TABLE IF NOT EXISTS ag_design_image_job (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  project_id TEXT REFERENCES ag_design_project(id) ON DELETE CASCADE,
  prompt TEXT NOT NULL,
  style TEXT NOT NULL DEFAULT '',
  size TEXT NOT NULL DEFAULT '1024x1024',
  num_images INTEGER NOT NULL DEFAULT 1 CHECK (num_images > 0 AND num_images <= 8),
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  result_urls TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  model TEXT NOT NULL DEFAULT 'workama.mock.image.v1',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ag_design_image_job_workspace ON ag_design_image_job(workspace_id,status,created_at DESC);

CREATE TABLE IF NOT EXISTS ag_design_canvas (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  state JSONB NOT NULL DEFAULT '{}'::jsonb,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id)
);
CREATE INDEX IF NOT EXISTS idx_ag_design_canvas_workspace ON ag_design_canvas(workspace_id,updated_at DESC);
