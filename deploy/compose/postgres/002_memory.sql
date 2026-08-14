-- Runtime schema is also applied by platform-api for existing local volumes.
CREATE TABLE IF NOT EXISTS ag_memory (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('profile','episodic')),
  memory_key TEXT NOT NULL,
  content TEXT NOT NULL,
  source_session_id TEXT REFERENCES ag_session(id) ON DELETE SET NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted','expired')),
  expires_at TIMESTAMPTZ,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  UNIQUE(workspace_id, user_id, kind, memory_key)
);
CREATE INDEX IF NOT EXISTS idx_ag_memory_owner_status ON ag_memory(workspace_id, user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ag_memory_search ON ag_memory USING GIN (to_tsvector('simple', memory_key || ' ' || content));
