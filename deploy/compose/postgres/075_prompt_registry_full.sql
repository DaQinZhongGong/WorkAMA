-- 075_prompt_registry_full.sql
-- T-M1-007 完整 Prompt Registry CRUD：在 sec_prompt_version 基线上扩展元数据/软删除/灰度配置表。
-- 设计依据：《500-LLM网关设计》Prompt Registry 章节、《600-数据模型设计》Gateway 域。
-- 兼容性：保留 sec_prompt_version 原有字段与 status 取值，仅做 ADD COLUMN / 约束替换 / 新建表。

-- ---------------------------------------------------------------------------
-- 1. 扩展 sec_prompt_version 元数据与软删除字段
-- ---------------------------------------------------------------------------
ALTER TABLE sec_prompt_version
    ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE sec_prompt_version
    ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE sec_prompt_version
    ADD COLUMN IF NOT EXISTS template_variables JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE sec_prompt_version
    ADD COLUMN IF NOT EXISTS model_hint TEXT;
ALTER TABLE sec_prompt_version
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE sec_prompt_version
    ADD COLUMN IF NOT EXISTS parent_version_id TEXT;

-- 扩展 status 取值，新增 'deleted' 用于软删除（保留 'draft'/'published'/'archived'）
ALTER TABLE sec_prompt_version
    DROP CONSTRAINT IF EXISTS sec_prompt_version_status_check;
ALTER TABLE sec_prompt_version
    ADD CONSTRAINT sec_prompt_version_status_check
    CHECK (status IN ('draft', 'published', 'archived', 'deleted'));

-- ---------------------------------------------------------------------------
-- 2. pf_prompt_rollout：可配置灰度比例表（替代/补充 sec_prompt_version.rollout_percent）
--    UNIQUE(prompt_id, workspace_id) 保证同一 prompt 在同一 workspace 仅一份灰度配置。
--    percent 取值：10 / 25 / 50 / 100（CHECK 允许 0–100，业务层枚举校验）。
--    strategy 取值：'stable_sha256' / 'all'。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pf_prompt_rollout (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    prompt_id TEXT NOT NULL REFERENCES sec_prompt_version(id) ON DELETE CASCADE,
    percent INTEGER NOT NULL DEFAULT 100
        CHECK (percent BETWEEN 0 AND 100),
    strategy TEXT NOT NULL DEFAULT 'stable_sha256'
        CHECK (strategy IN ('stable_sha256', 'all')),
    updated_by TEXT REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, prompt_id)
);

CREATE INDEX IF NOT EXISTS idx_pf_prompt_rollout_workspace
    ON pf_prompt_rollout(workspace_id, prompt_id);

-- ---------------------------------------------------------------------------
-- 3. 辅助索引：列表过滤、全文搜索、软删除过滤
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_sec_prompt_version_workspace_status
    ON sec_prompt_version(workspace_id, status, name, version DESC);
CREATE INDEX IF NOT EXISTS idx_sec_prompt_version_workspace_name
    ON sec_prompt_version(workspace_id, name, version DESC);
CREATE INDEX IF NOT EXISTS idx_sec_prompt_version_tags
    ON sec_prompt_version USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_sec_prompt_version_description
    ON sec_prompt_version USING GIN (to_tsvector('simple', coalesce(description, '') || ' ' || coalesce(name, '')));

-- ---------------------------------------------------------------------------
-- 4. 旧版本发布记录的 rollout_percent 归一化（防御性，幂等）
--    若 v7.24 已通过 028_prompt_rollout.sql 执行过，本段为 no-op。
-- ---------------------------------------------------------------------------
UPDATE sec_prompt_version
SET rollout_percent = 100
WHERE status = 'published' AND rollout_percent = 0;

-- ---------------------------------------------------------------------------
-- 5. 视图：活跃 prompt（非 deleted）按 workspace 聚合，便于列表查询
--    仅暴露非 deleted 版本，便于审计。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gw_prompt_active AS
SELECT id, workspace_id, name, version, status, rollout_percent,
       description, tags, model_hint, deleted_at, created_at, published_at
FROM sec_prompt_version
WHERE status <> 'deleted';
