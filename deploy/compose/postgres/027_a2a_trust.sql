-- A2A Agent Card trust material. Public keys are encrypted at rest and only
-- their SHA-256 fingerprints are exposed through the API.
CREATE TABLE IF NOT EXISTS pf_a2a_agent_key (
  id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL REFERENCES pf_a2a_agent_card(id) ON DELETE CASCADE,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  key_id TEXT NOT NULL,
  algorithm TEXT NOT NULL CHECK (algorithm IN ('Ed25519')),
  public_key_enc TEXT NOT NULL,
  public_key_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
  valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_until TIMESTAMPTZ,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(card_id,key_id)
);
CREATE INDEX IF NOT EXISTS idx_pf_a2a_agent_key_workspace ON pf_a2a_agent_key(workspace_id,card_id,status);

ALTER TABLE pf_a2a_task ADD COLUMN IF NOT EXISTS signature_key_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE pf_a2a_task ADD COLUMN IF NOT EXISTS signature_mode TEXT NOT NULL DEFAULT 'digest';
CREATE UNIQUE INDEX IF NOT EXISTS idx_pf_a2a_task_card_nonce ON pf_a2a_task(card_id,nonce) WHERE nonce IS NOT NULL;
