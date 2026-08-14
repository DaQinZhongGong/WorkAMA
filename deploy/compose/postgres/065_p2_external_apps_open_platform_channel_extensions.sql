-- v7.164-C: P2 外部应用真实执行 + 开放平台文档站 + 订阅账号池增强

-- 1) 外部应用调用审计日志
CREATE TABLE IF NOT EXISTS pf_external_app_audit_log (
  id TEXT PRIMARY KEY,
  invocation_id TEXT NOT NULL REFERENCES pf_external_app_invocation(id) ON DELETE CASCADE,
  app_id TEXT NOT NULL REFERENCES pf_external_app(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  actor_id TEXT,
  action TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  response_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  status_code INTEGER,
  error_code TEXT,
  attempt INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_external_app_audit_app ON pf_external_app_audit_log(app_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_external_app_audit_workspace ON pf_external_app_audit_log(workspace_id, created_at DESC);

-- 2) 开放平台公开文档区块
CREATE TABLE IF NOT EXISTS pf_open_platform_doc (
  id TEXT PRIMARY KEY,
  workspace_id TEXT REFERENCES id_workspace(id) ON DELETE CASCADE,
  slug TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  doc_type TEXT NOT NULL DEFAULT 'guide' CHECK (doc_type IN ('guide','api_reference','sdk','quickstart','webhook','oauth')),
  sort_order INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'published' CHECK (status IN ('draft','published','archived')),
  created_by TEXT NOT NULL REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_open_platform_doc_published ON pf_open_platform_doc(status, doc_type, sort_order);

-- 3) 订阅账号池用量与计费记录
CREATE TABLE IF NOT EXISTS gw_subscription_account_usage (
  id TEXT PRIMARY KEY,
  pool_id TEXT NOT NULL REFERENCES gw_subscription_account_pool(id) ON DELETE CASCADE,
  account_id TEXT REFERENCES gw_subscription_account(id) ON DELETE SET NULL,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  session_key_hash TEXT,
  model TEXT,
  prompt_tokens BIGINT NOT NULL DEFAULT 0,
  completion_tokens BIGINT NOT NULL DEFAULT 0,
  cost_credits BIGINT NOT NULL DEFAULT 0,
  billing_period TEXT NOT NULL DEFAULT 'current',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','billed','voided')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_account_usage_pool ON gw_subscription_account_usage(pool_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_usage_period ON gw_subscription_account_usage(pool_id, billing_period, status);

-- 4) 订阅账号池计费事件（幂等扣费）
CREATE TABLE IF NOT EXISTS gw_subscription_pool_billing_event (
  id TEXT PRIMARY KEY,
  pool_id TEXT NOT NULL REFERENCES gw_subscription_account_pool(id) ON DELETE CASCADE,
  account_id TEXT REFERENCES gw_subscription_account(id) ON DELETE SET NULL,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK (event_type IN ('lease','renew','release','topup')),
  idempotency_key TEXT NOT NULL,
  amount BIGINT NOT NULL DEFAULT 0,
  balance_after BIGINT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','succeeded','failed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(pool_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_pool_billing_event ON gw_subscription_pool_billing_event(pool_id, created_at DESC);
