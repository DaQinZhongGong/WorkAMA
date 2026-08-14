-- 053_audit_log.sql
-- v7.147: P1 身份/审查/MCP 模块 - 审计日志表
-- 记录 workspace 内的关键操作（create/update/delete/login/logout/enable/disable/export/config_change）

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_email TEXT,
    action TEXT NOT NULL,  -- create/update/delete/login/logout/enable/disable/export/config_change
    resource_type TEXT NOT NULL,  -- user/workspace/channel/knowledge_base/device/document/etc
    resource_id TEXT,
    severity TEXT NOT NULL DEFAULT 'info',  -- info/warning/critical
    description TEXT,
    source_ip TEXT,
    user_agent TEXT,
    request_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_workspace_idx ON audit_log(workspace_id);
CREATE INDEX IF NOT EXISTS audit_actor_idx ON audit_log(actor_id);
CREATE INDEX IF NOT EXISTS audit_action_idx ON audit_log(action);
CREATE INDEX IF NOT EXISTS audit_resource_idx ON audit_log(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS audit_severity_idx ON audit_log(severity);
CREATE INDEX IF NOT EXISTS audit_created_idx ON audit_log(created_at);
