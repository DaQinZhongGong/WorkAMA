-- AMA-Design canvas history for undo/redo and export jobs
CREATE TABLE IF NOT EXISTS ag_design_canvas_history (
  id TEXT PRIMARY KEY,
  canvas_id TEXT NOT NULL REFERENCES ag_design_canvas(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  state JSONB NOT NULL DEFAULT '{}'::jsonb,
  version INTEGER NOT NULL,
  action TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ag_design_canvas_history_canvas ON ag_design_canvas_history(canvas_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ag_design_canvas_history_project ON ag_design_canvas_history(project_id,created_at DESC);

CREATE TABLE IF NOT EXISTS ag_design_export_job (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  format TEXT NOT NULL CHECK (format IN ('svg','png','jpeg','pdf')),
  include_layers BOOLEAN NOT NULL DEFAULT true,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed')),
  result_bytes BYTEA,
  result_sha256 TEXT,
  error_message TEXT,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ag_design_export_job_workspace ON ag_design_export_job(workspace_id,status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ag_design_export_job_project ON ag_design_export_job(project_id,created_at DESC);
