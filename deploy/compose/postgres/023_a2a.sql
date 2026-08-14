CREATE TABLE IF NOT EXISTS pf_a2a_agent_card (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  version TEXT NOT NULL,
  capabilities TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  skills TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  authentication TEXT NOT NULL DEFAULT 'delegated' CHECK (authentication IN ('none','delegated','oauth')),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','revoked')),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,agent_id,version)
);
CREATE INDEX IF NOT EXISTS idx_pf_a2a_agent_card_workspace ON pf_a2a_agent_card(workspace_id,status,updated_at DESC);

CREATE TABLE IF NOT EXISTS pf_a2a_task (
  id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL REFERENCES pf_a2a_agent_card(id) ON DELETE CASCADE,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  operation TEXT NOT NULL,
  message_hash TEXT NOT NULL,
  artifact_refs TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','working','completed','failed','cancelled')),
  result_summary TEXT NOT NULL DEFAULT '',
  idempotency_key TEXT NOT NULL,
  delegated_credential_hash TEXT,
  execution_mode TEXT NOT NULL DEFAULT 'pending_external',
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(card_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_pf_a2a_task_workspace_time ON pf_a2a_task(workspace_id,created_at DESC);
