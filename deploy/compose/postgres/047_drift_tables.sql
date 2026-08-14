-- v7.121 P1 contract drift reduction: 7 missing PostgreSQL tables
-- Design baseline: WorkAMA-Docs/620-物理数据字典与状态机注册表.md

CREATE TABLE IF NOT EXISTS ag_channel_binding (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  channel_type TEXT NOT NULL CHECK (channel_type IN ('slack','teams','wecom','dingtalk','feishu','telegram','custom')),
  external_subject TEXT NOT NULL,
  credential_ref TEXT,
  mapping JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','revoked')),
  last_sync TIMESTAMPTZ,
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, channel_type, external_subject)
);

CREATE INDEX IF NOT EXISTS idx_ag_channel_binding_workspace_status ON ag_channel_binding(workspace_id, status, channel_type);

CREATE TABLE IF NOT EXISTS ag_schedule (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  target_type TEXT NOT NULL CHECK (target_type IN ('work_plan','workflow','agent','skill')),
  target_id TEXT NOT NULL,
  cron_expression TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  dst_policy TEXT NOT NULL DEFAULT 'skip' CHECK (dst_policy IN ('skip','previous','next')),
  missed_policy TEXT NOT NULL DEFAULT 'run_once' CHECK (missed_policy IN ('run_once','skip','catchup')),
  budget JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
  next_run TIMESTAMPTZ,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, name)
);

CREATE INDEX IF NOT EXISTS idx_ag_schedule_due ON ag_schedule(workspace_id, status, next_run);

CREATE TABLE IF NOT EXISTS bill_entitlement (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  source TEXT NOT NULL CHECK (source IN ('plan','contract','grant','license')),
  plan_code TEXT,
  contract_id TEXT,
  entitlement_key TEXT NOT NULL,
  value JSONB NOT NULL,
  effective_from TIMESTAMPTZ NOT NULL,
  effective_to TIMESTAMPTZ,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','revoked')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(org_id, entitlement_key, effective_from)
);

CREATE INDEX IF NOT EXISTS idx_bill_entitlement_org_key ON bill_entitlement(org_id, entitlement_key, effective_from DESC);

CREATE TABLE IF NOT EXISTS gw_channel_model (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL REFERENCES gw_channel(id) ON DELETE CASCADE,
  provider_model TEXT NOT NULL,
  capabilities TEXT[] NOT NULL DEFAULT '{}',
  context_limit INTEGER CHECK (context_limit > 0),
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  metadata_version INTEGER NOT NULL DEFAULT 1 CHECK (metadata_version > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(channel_id, provider_model)
);

CREATE INDEX IF NOT EXISTS idx_gw_channel_model_channel ON gw_channel_model(channel_id, enabled);

CREATE TABLE IF NOT EXISTS gw_model_alias (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  channel_model_id TEXT NOT NULL REFERENCES gw_channel_model(id) ON DELETE CASCADE,
  priority INTEGER NOT NULL DEFAULT 100 CHECK (priority >= 0),
  conditions JSONB NOT NULL DEFAULT '{}'::jsonb,
  effective_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, alias, priority)
);

CREATE INDEX IF NOT EXISTS idx_gw_model_alias_workspace ON gw_model_alias(workspace_id, alias, priority DESC);

CREATE TABLE IF NOT EXISTS id_credential (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('password','recovery_code','api_secret','oauth_token')),
  secret_hash TEXT,
  secret_ciphertext TEXT,
  hash_version TEXT NOT NULL DEFAULT 'argon2id',
  expires_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (secret_hash IS NOT NULL OR secret_ciphertext IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_id_credential_user ON id_credential(user_id, kind) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS id_identity (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  subject TEXT NOT NULL,
  profile_min JSONB NOT NULL DEFAULT '{}'::jsonb,
  verified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(provider, subject)
);

CREATE INDEX IF NOT EXISTS idx_id_identity_user ON id_identity(user_id, provider);
