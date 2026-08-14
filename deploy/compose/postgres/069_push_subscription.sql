-- v7.165: PWA Web Push 订阅表
-- 存储 endpoint + p256dh + auth，支持 workspace/user 隔离

CREATE TABLE IF NOT EXISTS push_subscription (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL,
    p256dh TEXT,
    auth TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, endpoint)
);

CREATE INDEX IF NOT EXISTS idx_push_subscription_workspace_user
    ON push_subscription(workspace_id, user_id, created_at DESC);
