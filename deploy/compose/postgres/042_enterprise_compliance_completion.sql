-- Enterprise commercial entitlement and privacy trust controls.
-- Raw license keys and access credentials are never persisted.

CREATE TABLE IF NOT EXISTS bill_license (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  plan_code TEXT NOT NULL,
  license_key_hash TEXT NOT NULL UNIQUE,
  license_key_last_four TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','suspended','expired','revoked')),
  seats INTEGER NOT NULL CHECK (seats > 0),
  credit_limit BIGINT,
  concurrency_limit INTEGER CHECK (concurrency_limit IS NULL OR concurrency_limit > 0),
  features JSONB NOT NULL DEFAULT '{}'::jsonb,
  issued_by TEXT NOT NULL REFERENCES id_user(id),
  idempotency_key TEXT,
  valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_until TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ,
  revoke_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(org_id, workspace_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_bill_license_workspace_status
  ON bill_license(workspace_id, status, valid_until);

CREATE TABLE IF NOT EXISTS bill_sla_policy (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL UNIQUE REFERENCES id_workspace(id) ON DELETE CASCADE,
  service_tier TEXT NOT NULL,
  availability_target NUMERIC(6,3) NOT NULL CHECK (availability_target > 0 AND availability_target <= 100),
  response_target_seconds INTEGER NOT NULL CHECK (response_target_seconds > 0),
  support_window TEXT NOT NULL,
  credits_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','draft','retired')),
  effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
  effective_until TIMESTAMPTZ,
  updated_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sec_region_policy (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL UNIQUE REFERENCES id_workspace(id) ON DELETE CASCADE,
  home_region TEXT NOT NULL,
  allowed_regions TEXT[] NOT NULL,
  provider_regions TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
  cross_border_mode TEXT NOT NULL DEFAULT 'deny' CHECK (cross_border_mode IN ('deny','allowlist')),
  residency_required BOOLEAN NOT NULL DEFAULT TRUE,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  updated_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sec_jit_grant (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  subject_user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
  approved_by TEXT NOT NULL REFERENCES id_user(id),
  capabilities TEXT[] NOT NULL,
  resource_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
  reason TEXT NOT NULL,
  grant_hash TEXT NOT NULL UNIQUE,
  auth_strength SMALLINT NOT NULL CHECK (auth_strength BETWEEN 1 AND 4),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','revoked')),
  starts_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  revoke_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sec_jit_grant_subject
  ON sec_jit_grant(workspace_id, subject_user_id, status, expires_at);

CREATE TABLE IF NOT EXISTS sec_subprocessor (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  regions TEXT[] NOT NULL,
  data_classes TEXT[] NOT NULL,
  dpa_status TEXT NOT NULL DEFAULT 'pending' CHECK (dpa_status IN ('pending','reviewed','signed','expired')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','retired')),
  privacy_url TEXT,
  trust_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  reviewed_at TIMESTAMPTZ,
  reviewed_by TEXT REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, name)
);

CREATE TABLE IF NOT EXISTS sec_privacy_event (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('low','medium','high','critical')),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','investigating','contained','closed')),
  summary TEXT NOT NULL,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  reported_by TEXT NOT NULL REFERENCES id_user(id),
  resolved_by TEXT REFERENCES id_user(id),
  resolved_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sec_privacy_event_workspace_status
  ON sec_privacy_event(workspace_id, status, created_at DESC);
