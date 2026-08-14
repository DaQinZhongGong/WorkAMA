-- Billing commercial completion: immutable price snapshots, orders, signed
-- provider events, hash-only payment methods, refunds and invoice requests.
-- All objects are additive and safe to re-run by the local migration runner.

ALTER TABLE bill_payment ADD COLUMN IF NOT EXISTS order_id TEXT;
ALTER TABLE bill_invoice ADD COLUMN IF NOT EXISTS order_id TEXT;

CREATE TABLE IF NOT EXISTS bill_price_catalog (
    id TEXT PRIMARY KEY,
    plan_code TEXT NOT NULL REFERENCES bill_plan(code),
    region TEXT NOT NULL,
    currency TEXT NOT NULL,
    tax_mode TEXT NOT NULL CHECK (tax_mode IN ('exclusive', 'inclusive')),
    price NUMERIC(18,2) NOT NULL CHECK (price >= 0),
    tax_rate NUMERIC(8,6) NOT NULL DEFAULT 0 CHECK (tax_rate >= 0),
    version INTEGER NOT NULL,
    effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    effective_to TIMESTAMPTZ,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(plan_code, region, currency, tax_mode, version)
);

CREATE TABLE IF NOT EXISTS bill_order (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    order_no TEXT NOT NULL UNIQUE,
    order_type TEXT NOT NULL CHECK (order_type IN ('subscription', 'credits')),
    plan_code TEXT REFERENCES bill_plan(code),
    amount NUMERIC(18,2) NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL,
    credits NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (credits >= 0),
    price_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    tax_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    discount_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'succeeded', 'failed', 'canceled', 'unknown', 'charged_back', 'partially_refunded', 'refunded')),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 minutes'),
    idempotency_key TEXT NOT NULL,
    version BIGINT NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS bill_payment_event (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    payment_id TEXT NOT NULL REFERENCES bill_payment(id) ON DELETE CASCADE,
    payload_hash TEXT NOT NULL,
    outcome TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at TIMESTAMPTZ,
    UNIQUE(provider, provider_event_id)
);

CREATE TABLE IF NOT EXISTS bill_payment_method (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    method_type TEXT NOT NULL,
    display_label TEXT NOT NULL,
    provider_ref_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'detached')),
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    detached_at TIMESTAMPTZ,
    detach_reason TEXT,
    UNIQUE(workspace_id, provider, provider_ref_hash)
);

CREATE TABLE IF NOT EXISTS bill_refund (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    payment_id TEXT NOT NULL REFERENCES bill_payment(id),
    order_id TEXT REFERENCES bill_order(id),
    amount NUMERIC(18,2) NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'succeeded', 'failed', 'unknown', 'pending_external')),
    provider_ref_hash TEXT,
    idempotency_key TEXT NOT NULL,
    approval_ref TEXT,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS bill_invoice_request (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    order_id TEXT NOT NULL REFERENCES bill_order(id),
    tax_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending_external' CHECK (status IN ('pending_external', 'queued', 'issued', 'failed')),
    idempotency_key TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_bill_order_workspace_time ON bill_order(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bill_payment_event_payment ON bill_payment_event(payment_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_bill_refund_workspace_time ON bill_refund(workspace_id, created_at DESC);

INSERT INTO bill_price_catalog(id, plan_code, region, currency, tax_mode, price, tax_rate, version)
SELECT 'price_' || p.code || '_v1', p.code, 'CN', p.currency, 'exclusive', p.monthly_price, 0, 1
FROM bill_plan p
ON CONFLICT(plan_code, region, currency, tax_mode, version) DO UPDATE
SET price = EXCLUDED.price, active = TRUE;
