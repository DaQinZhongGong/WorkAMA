-- Controlled subscription account pools, IM bridge records and React miniapp state.
-- External OAuth/provider delivery remains pending until staging credentials exist.

CREATE TABLE IF NOT EXISTS gw_subscription_account_pool (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  provider TEXT NOT NULL,
  sticky_ttl_seconds INTEGER NOT NULL DEFAULT 3600 CHECK (sticky_ttl_seconds BETWEEN 60 AND 604800),
  billing_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, name)
);
CREATE TABLE IF NOT EXISTS gw_subscription_account (
  id TEXT PRIMARY KEY,
  pool_id TEXT NOT NULL REFERENCES gw_subscription_account_pool(id) ON DELETE CASCADE,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  display_name TEXT NOT NULL,
  account_ref_enc TEXT NOT NULL,
  account_ref_hash TEXT NOT NULL,
  last_four TEXT NOT NULL,
  region TEXT NOT NULL DEFAULT 'global',
  weight INTEGER NOT NULL DEFAULT 100 CHECK (weight > 0),
  quota_remaining BIGINT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','exhausted','revoked')),
  lease_owner_hash TEXT,
  lease_expires_at TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  error_count INTEGER NOT NULL DEFAULT 0 CHECK (error_count >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(pool_id, account_ref_hash)
);
CREATE INDEX IF NOT EXISTS idx_gw_subscription_account_lease ON gw_subscription_account(pool_id, status, lease_expires_at, last_used_at);
CREATE TABLE IF NOT EXISTS gw_subscription_session (
  id TEXT PRIMARY KEY,
  pool_id TEXT NOT NULL REFERENCES gw_subscription_account_pool(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL REFERENCES gw_subscription_account(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  session_key_hash TEXT NOT NULL,
  model TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','released')),
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(pool_id, session_key_hash)
);

CREATE TABLE IF NOT EXISTS im_channel (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('wecom','dingtalk','feishu','telegram')),
  name TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  signing_secret_enc TEXT,
  signing_secret_hash TEXT,
  agent_id TEXT,
  status TEXT NOT NULL DEFAULT 'disabled' CHECK (status IN ('disabled','active','pending_external','revoked')),
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_im_channel_workspace_status ON im_channel(workspace_id, status, kind);
CREATE TABLE IF NOT EXISTS im_message (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL REFERENCES im_channel(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  external_message_id TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
  sender_ref_hash TEXT,
  payload_min JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted','delivered','pending_external','failed','replayed')),
  response_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(channel_id, external_message_id, direction)
);
CREATE INDEX IF NOT EXISTS idx_im_message_channel_time ON im_message(channel_id, created_at DESC);

CREATE TABLE IF NOT EXISTS miniapp_session (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
  provider TEXT NOT NULL DEFAULT 'wechat',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS miniapp_message (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES miniapp_session(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'delivered' CHECK (status IN ('queued','delivered','pending_external','failed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS miniapp_subscription (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
  topic TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'wechat',
  status TEXT NOT NULL DEFAULT 'pending_external' CHECK (status IN ('pending_external','subscribed','revoked')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, user_id, topic, provider)
);
