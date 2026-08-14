-- 051_device_telemetry.sql
-- v7.142: AMA-Work 设备协同与本地观测闭环
-- 设备注册 / 心跳上报 / 遥测事件 / 离线扫描

CREATE TABLE IF NOT EXISTS device_telemetry (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    device_name TEXT,
    device_kind TEXT NOT NULL DEFAULT 'desktop',  -- desktop/laptop/server/edge/iot
    os TEXT,
    app_version TEXT,
    last_heartbeat_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'offline',  -- online/offline/warning
    telemetry JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, device_id)
);

CREATE INDEX IF NOT EXISTS device_telemetry_workspace_idx ON device_telemetry(workspace_id);
CREATE INDEX IF NOT EXISTS device_telemetry_status_idx ON device_telemetry(status);
CREATE INDEX IF NOT EXISTS device_telemetry_heartbeat_idx ON device_telemetry(last_heartbeat_at);
