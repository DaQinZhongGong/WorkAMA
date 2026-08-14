-- 079: 微信小程序会话安全强化 - 会话审计日志表
-- 记录 login/logout/refresh/revoke 事件，支持安全审计与异常检测

CREATE TABLE IF NOT EXISTS wx_miniapp_session_log (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('login', 'logout', 'refresh', 'revoke')),
    ip TEXT,
    user_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wx_miniapp_session_log_workspace_user
    ON wx_miniapp_session_log(workspace_id, user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_wx_miniapp_session_log_session
    ON wx_miniapp_session_log(session_id, created_at DESC);
