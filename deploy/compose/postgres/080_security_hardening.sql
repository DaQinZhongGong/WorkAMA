-- 080: P3 第二次渗透测试安全加固 - 速率限制桶 / JWT 黑名单 / 审计日志链
-- 配合 apps/platform-api/src/workama_platform/modules/security_hardening.py 使用

-- 速率限制滑动窗口桶（内存兜底的持久化镜像，主路径仍走进程内存）
CREATE TABLE IF NOT EXISTS rate_limit_bucket (
    key TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (key, window_start)
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket_key_window
    ON rate_limit_bucket(key, window_start DESC);

-- JWT 令牌黑名单（refresh 轮换撤销 / 主动登出 / 令牌绑定失效）
CREATE TABLE IF NOT EXISTS jwt_token_blacklist (
    jti TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jwt_token_blacklist_expires
    ON jwt_token_blacklist(expires_at);

-- 审计日志链式 hash（prev_hash + payload -> chain_hash，防篡改）
CREATE TABLE IF NOT EXISTS audit_log_chain (
    audit_id TEXT PRIMARY KEY,
    prev_hash TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL,
    chain_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_chain_created
    ON audit_log_chain(created_at DESC);
