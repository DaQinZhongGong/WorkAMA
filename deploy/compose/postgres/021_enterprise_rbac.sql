CREATE TABLE IF NOT EXISTS id_group (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  external_id TEXT,
  source TEXT NOT NULL DEFAULT 'local' CHECK (source IN ('local','scim','directory')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  create_idempotency_key TEXT,
  request_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(org_id,name)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_group_idempotency ON id_group(org_id,create_idempotency_key) WHERE create_idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_group_external_id ON id_group(org_id,source,external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_id_group_org_status ON id_group(org_id,status,updated_at DESC);

CREATE TABLE IF NOT EXISTS id_group_member (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  group_id TEXT NOT NULL REFERENCES id_group(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
  source TEXT NOT NULL DEFAULT 'local' CHECK (source IN ('local','scim','directory')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  create_idempotency_key TEXT,
  request_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(group_id,user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_group_member_idempotency ON id_group_member(group_id,create_idempotency_key) WHERE create_idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_id_group_member_org_user ON id_group_member(org_id,user_id,status);

CREATE TABLE IF NOT EXISTS id_role (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  capabilities TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  system BOOLEAN NOT NULL DEFAULT FALSE,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  create_idempotency_key TEXT,
  request_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,name)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_role_idempotency ON id_role(workspace_id,create_idempotency_key) WHERE create_idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_id_role_workspace_status ON id_role(workspace_id,status,updated_at DESC);

CREATE TABLE IF NOT EXISTS id_role_binding (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  role_id TEXT NOT NULL REFERENCES id_role(id) ON DELETE CASCADE,
  subject_type TEXT NOT NULL CHECK (subject_type IN ('user','group','service_account')),
  subject_id TEXT NOT NULL,
  resource_type TEXT,
  resource_id TEXT,
  conditions JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  expires_at TIMESTAMPTZ,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  create_idempotency_key TEXT,
  request_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_role_binding_idempotency ON id_role_binding(workspace_id,create_idempotency_key) WHERE create_idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_id_role_binding_subject ON id_role_binding(org_id,workspace_id,subject_type,subject_id,status);
CREATE INDEX IF NOT EXISTS idx_id_role_binding_role ON id_role_binding(workspace_id,role_id,status,expires_at);

CREATE TABLE IF NOT EXISTS id_service_account_policy (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  service_account_id TEXT NOT NULL REFERENCES id_service_account(id) ON DELETE CASCADE,
  allowed_scopes TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  allowed_ip_cidrs TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  expires_at TIMESTAMPTZ,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  create_idempotency_key TEXT,
  request_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,service_account_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_service_account_policy_idempotency ON id_service_account_policy(workspace_id,create_idempotency_key) WHERE create_idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_id_service_account_policy_workspace ON id_service_account_policy(workspace_id,status,expires_at);

CREATE TABLE IF NOT EXISTS id_auth_strength_policy (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  operation TEXT NOT NULL,
  required_auth_strength SMALLINT NOT NULL CHECK (required_auth_strength BETWEEN 1 AND 4),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  create_idempotency_key TEXT,
  request_hash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id,operation)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_id_auth_strength_policy_idempotency ON id_auth_strength_policy(workspace_id,create_idempotency_key) WHERE create_idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_id_auth_strength_policy_workspace ON id_auth_strength_policy(workspace_id,status,operation);
