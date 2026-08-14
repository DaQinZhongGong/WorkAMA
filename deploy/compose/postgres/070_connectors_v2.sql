-- v7.165: 企业知识连接器 v2 表
-- 支持 Google Drive / Notion 适配器骨架，workspace 隔离，增量同步游标

CREATE TABLE IF NOT EXISTS connector_config_v2 (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('google_drive','notion')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('active','pending','disabled','error')),
    auth_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    sync_root TEXT,
    last_cursor TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, name)
);

CREATE INDEX IF NOT EXISTS idx_connector_config_v2_workspace
    ON connector_config_v2(workspace_id, status, updated_at DESC);
