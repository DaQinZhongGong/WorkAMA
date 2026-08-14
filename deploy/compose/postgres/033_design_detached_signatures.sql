-- AMA-Design workspace-scoped Ed25519 detached claim signing.
-- The private key is Fernet-encrypted by platform-api; only its fingerprint is public.
CREATE TABLE IF NOT EXISTS ag_design_signing_key (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  algorithm TEXT NOT NULL CHECK (algorithm IN ('Ed25519')),
  private_key_enc TEXT NOT NULL,
  public_key_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id)
);
CREATE INDEX IF NOT EXISTS idx_ag_design_signing_key_workspace ON ag_design_signing_key(workspace_id,status);
