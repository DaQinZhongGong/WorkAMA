-- 055_billing.sql
-- v7.148: 订阅与计费模块（套餐 / 订阅 / 用量 / 发票）
-- 与既有 bill_plan/bill_subscription（subscriptions.py）独立，使用 billing_* 前缀。

CREATE TABLE IF NOT EXISTS billing_plan (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,  -- free/starter/pro/enterprise
    name TEXT NOT NULL,
    description TEXT,
    price_monthly DECIMAL(10,2) NOT NULL DEFAULT 0,
    price_yearly DECIMAL(10,2) NOT NULL DEFAULT 0,
    token_quota BIGINT NOT NULL DEFAULT 1000000,
    seat_quota INTEGER NOT NULL DEFAULT 1,
    storage_quota_gb INTEGER NOT NULL DEFAULT 1,
    api_rate_limit INTEGER NOT NULL DEFAULT 60,  -- requests per minute
    features JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS billing_subscription (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES billing_plan(id),
    billing_cycle TEXT NOT NULL DEFAULT 'monthly',  -- monthly/yearly
    status TEXT NOT NULL DEFAULT 'active',  -- active/past_due/canceled/trialing
    current_period_start TIMESTAMPTZ NOT NULL DEFAULT now(),
    current_period_end TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days'),
    trial_end TIMESTAMPTZ,
    canceled_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS billing_usage_record (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    subscription_id TEXT NOT NULL REFERENCES billing_subscription(id),
    metric TEXT NOT NULL,  -- tokens_used/seats_used/storage_used_gb/api_calls
    value BIGINT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS billing_invoice (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    subscription_id TEXT NOT NULL REFERENCES billing_subscription(id),
    amount DECIMAL(10,2) NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/paid/void/refunded
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    paid_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS billing_plan_code_idx ON billing_plan(code);
CREATE INDEX IF NOT EXISTS billing_sub_workspace_idx ON billing_subscription(workspace_id);
CREATE INDEX IF NOT EXISTS billing_sub_plan_idx ON billing_subscription(plan_id);
CREATE INDEX IF NOT EXISTS billing_usage_workspace_idx ON billing_usage_record(workspace_id);
CREATE INDEX IF NOT EXISTS billing_usage_metric_idx ON billing_usage_record(metric);
CREATE INDEX IF NOT EXISTS billing_invoice_workspace_idx ON billing_invoice(workspace_id);
