-- Monthly granted-credit buckets. The aggregate bill_account columns remain
-- compatible with existing callers while bucket rows provide expiry facts.

CREATE TABLE IF NOT EXISTS bill_credit_grant (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    subscription_id TEXT,
    source TEXT NOT NULL CHECK (source IN ('initial', 'subscription', 'manual', 'migration')),
    period_start TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    initial_amount NUMERIC(18,6) NOT NULL CHECK (initial_amount > 0),
    remaining_amount NUMERIC(18,6) NOT NULL CHECK (remaining_amount >= 0 AND remaining_amount <= initial_amount),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'exhausted', 'expired')),
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expired_at TIMESTAMPTZ,
    UNIQUE(workspace_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_bill_credit_grant_expiry
    ON bill_credit_grant(workspace_id, status, expires_at, period_start);

INSERT INTO bill_credit_grant(
    id, workspace_id, source, period_start, initial_amount, remaining_amount,
    status, idempotency_key
)
SELECT
    'grant_migration_' || a.id, a.workspace_id, 'migration', date_trunc('month', now()),
    a.granted_balance, a.granted_balance, 'active', 'migration:' || a.workspace_id
FROM bill_account a
WHERE a.granted_balance > 0
ON CONFLICT(workspace_id, idempotency_key) DO NOTHING;
