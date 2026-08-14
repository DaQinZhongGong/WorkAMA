CREATE TABLE IF NOT EXISTS sec_audit_chain (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  sequence BIGINT NOT NULL,
  event_type TEXT NOT NULL,
  actor_user_id TEXT REFERENCES id_user(id) ON DELETE SET NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  record_hash TEXT NOT NULL,
  previous_hash TEXT NOT NULL DEFAULT '',
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,sequence), UNIQUE(workspace_id,record_hash)
);
CREATE INDEX IF NOT EXISTS idx_sec_audit_chain_workspace_time ON sec_audit_chain(workspace_id,occurred_at DESC,sequence DESC);

CREATE TABLE IF NOT EXISTS sec_audit_export (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('queued','completed','failed','expired')),
  format TEXT NOT NULL CHECK (format IN ('jsonl','manifest')),
  filter_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  record_count INTEGER NOT NULL DEFAULT 0,
  content_hash TEXT,
  manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  idempotency_key TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours'),
  UNIQUE(workspace_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_sec_audit_export_workspace_time ON sec_audit_export(workspace_id,created_at DESC);

CREATE TABLE IF NOT EXISTS sec_siem_config (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  credential_hash TEXT,
  credential_last4 TEXT,
  events TEXT[] NOT NULL DEFAULT ARRAY['*']::text[],
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  version INTEGER NOT NULL DEFAULT 1,
  updated_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,name)
);

CREATE TABLE IF NOT EXISTS sec_siem_delivery (
  id TEXT PRIMARY KEY,
  config_id TEXT NOT NULL REFERENCES sec_siem_config(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_external' CHECK (status IN ('pending_external','delivered','failed')),
  error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(config_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_sec_siem_delivery_workspace_time ON sec_siem_delivery(workspace_id,created_at DESC);
