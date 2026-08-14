CREATE TABLE IF NOT EXISTS pf_oauth_client (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  client_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  client_secret_hash TEXT NOT NULL,
  client_secret_last4 TEXT NOT NULL,
  redirect_uris TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  scopes TEXT[] NOT NULL DEFAULT ARRAY['openid'],
  grant_types TEXT[] NOT NULL DEFAULT ARRAY['authorization_code','refresh_token'],
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','revoked')),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  last_used_at TIMESTAMPTZ,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_pf_oauth_client_workspace_status ON pf_oauth_client(workspace_id,status,updated_at DESC);

CREATE TABLE IF NOT EXISTS pf_oauth_code (
  id TEXT PRIMARY KEY,
  client_id TEXT NOT NULL REFERENCES pf_oauth_client(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
  code_hash TEXT NOT NULL UNIQUE,
  redirect_uri TEXT NOT NULL,
  scope TEXT NOT NULL,
  code_challenge TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pf_oauth_code_expiry ON pf_oauth_code(expires_at,consumed_at);

CREATE TABLE IF NOT EXISTS pf_oauth_token (
  id TEXT PRIMARY KEY,
  client_id TEXT NOT NULL REFERENCES pf_oauth_client(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
  access_token_hash TEXT NOT NULL UNIQUE,
  refresh_token_hash TEXT NOT NULL UNIQUE,
  scope TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked','expired')),
  access_expires_at TIMESTAMPTZ NOT NULL,
  refresh_expires_at TIMESTAMPTZ NOT NULL,
  last_used_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pf_oauth_token_workspace ON pf_oauth_token(workspace_id,status,created_at DESC);

CREATE TABLE IF NOT EXISTS pf_webhook (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  events TEXT[] NOT NULL,
  secret_hash TEXT NOT NULL,
  secret_last4 TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','revoked')),
  failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
  last_delivered_at TIMESTAMPTZ,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pf_webhook_workspace_status ON pf_webhook(workspace_id,status,updated_at DESC);

CREATE TABLE IF NOT EXISTS pf_webhook_delivery (
  id TEXT PRIMARY KEY,
  webhook_id TEXT NOT NULL REFERENCES pf_webhook(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','failed','blocked_external')),
  attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  next_attempt_at TIMESTAMPTZ,
  response_code INTEGER,
  error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(webhook_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_pf_webhook_delivery_webhook_time ON pf_webhook_delivery(webhook_id,created_at DESC);
