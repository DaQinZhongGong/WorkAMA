-- v7.165: 订阅账号池增强（sweep / auto_topup / release_expired_leases 支持索引）

CREATE INDEX IF NOT EXISTS idx_gw_subscription_account_exhausted ON gw_subscription_account(pool_id, status, error_count, quota_remaining);
