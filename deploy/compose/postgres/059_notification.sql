-- 059_notification.sql
-- v7.153: P1 平台支撑模块 - 通知中心 (notification-center)
-- 简洁的 in-app 通知 CRUD + 未读数接口，与既有 id_notification 表独立共存。
-- 既有 notification/ 包（id_notification + preferences + deliveries）处理
-- 模板化投递与偏好；本表面向简单 in-app 通知场景，由 notification.py 使用。

CREATE TABLE IF NOT EXISTS notification (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'info',  -- info/success/warning/error/system
    title TEXT NOT NULL DEFAULT '',
    body TEXT,
    action_url TEXT,
    action_label TEXT,
    read BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS notification_workspace_user_idx
    ON notification(workspace_id, user_id);
CREATE INDEX IF NOT EXISTS notification_user_unread_idx
    ON notification(user_id, workspace_id, read)
    WHERE read = FALSE;
CREATE INDEX IF NOT EXISTS notification_created_at_idx
    ON notification(created_at DESC);
