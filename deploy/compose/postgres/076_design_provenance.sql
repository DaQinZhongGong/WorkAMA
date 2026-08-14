-- AMA-Design provenance manifest (C2PA-compatible unsigned claim).
-- Each generated asset can carry one or more manifests forming a hash chain.
-- manifest_version allows multiple manifests per asset while UNIQUE prevents
-- duplicate versions within the same workspace/asset pair.
CREATE TABLE IF NOT EXISTS ag_design_provenance_manifest (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES ag_design_project(id) ON DELETE CASCADE,
  asset_id TEXT NOT NULL REFERENCES ag_design_asset(id) ON DELETE CASCADE,
  manifest_version TEXT NOT NULL DEFAULT '1.0',
  generator JSONB NOT NULL DEFAULT '{}'::jsonb,
  prompt_hash TEXT NOT NULL DEFAULT '',
  source_assets JSONB NOT NULL DEFAULT '[]'::jsonb,
  claim_hash TEXT NOT NULL,
  parent_claim_hash TEXT,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(workspace_id, asset_id, manifest_version)
);
CREATE INDEX IF NOT EXISTS idx_ag_design_provenance_workspace ON ag_design_provenance_manifest(workspace_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ag_design_provenance_asset ON ag_design_provenance_manifest(asset_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ag_design_provenance_claim_hash ON ag_design_provenance_manifest(claim_hash);
CREATE INDEX IF NOT EXISTS idx_ag_design_provenance_parent_claim ON ag_design_provenance_manifest(parent_claim_hash);
