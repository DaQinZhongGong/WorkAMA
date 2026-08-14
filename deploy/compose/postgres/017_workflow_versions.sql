-- Workflow version snapshots, rollback provenance, and run comparison support.
ALTER TABLE pf_workflow_run ADD COLUMN IF NOT EXISTS workflow_version INTEGER NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS pf_workflow_version (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL REFERENCES pf_workflow(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  graph JSONB NOT NULL,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workflow_id, version)
);
INSERT INTO pf_workflow_version(id,workflow_id,workspace_id,version,graph,created_by)
SELECT 'wfv-' || id, id, workspace_id, version, graph, created_by
FROM pf_workflow
ON CONFLICT (workflow_id, version) DO NOTHING;
CREATE INDEX IF NOT EXISTS idx_pf_workflow_version_time
    ON pf_workflow_version(workflow_id, version DESC);
