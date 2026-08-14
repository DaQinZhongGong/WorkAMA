-- 080: 海外区部署选项 + GDPR 数据本地化 (P3)
-- 数据驻留区域治理：workspace 区域绑定、数据本地化策略、DSAR 数据主体访问请求、跨境传输审计。
-- 依据《410》§7 删除传播矩阵与《815》端口约束（端点位于 platform-api 20200 端口下，不新增端口）。

-- workspace 绑定数据驻留区域（CN/EU/US/SG）；向后兼容：未绑定时由应用层默认 settings.default_region
ALTER TABLE id_workspace ADD COLUMN IF NOT EXISTS region TEXT;

-- 数据驻留策略表：每个 workspace 一条，region 绑定后不可更改，仅 cross_region_allowed 可调
CREATE TABLE IF NOT EXISTS data_residency_policy (
    workspace_id TEXT PRIMARY KEY REFERENCES id_workspace(id) ON DELETE CASCADE,
    region TEXT NOT NULL CHECK (region IN ('CN','EU','US','SG')),
    data_localization_enforced BOOLEAN NOT NULL DEFAULT TRUE,
    cross_region_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- DSAR（数据主体访问请求）表：access / erasure / portability，状态 pending→processing→completed|rejected
CREATE TABLE IF NOT EXISTS dsar_request (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
    request_type TEXT NOT NULL CHECK (request_type IN ('access','erasure','portability')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','rejected')),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dsar_request_workspace_status
    ON dsar_request(workspace_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dsar_request_user
    ON dsar_request(user_id, created_at DESC);

-- 跨境传输审计表：任何跨区域数据访问记录（user_id/region_from/region_to/resource_type/resource_id/audit_reason）
CREATE TABLE IF NOT EXISTS cross_region_access_audit (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    user_id TEXT REFERENCES id_user(id) ON DELETE SET NULL,
    region_from TEXT NOT NULL,
    region_to TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    audit_reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cross_region_access_audit_workspace_time
    ON cross_region_access_audit(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cross_region_access_audit_user
    ON cross_region_access_audit(user_id, created_at DESC);
