-- 073_memory_governance_worker.sql
-- v7.167: Memory Governance Worker platform-worker 集成 + 自动遗忘定时任务
-- 为 workspace 级别遗忘策略配置提供存储

CREATE TABLE IF NOT EXISTS memory_governance_policy (
    workspace_id TEXT PRIMARY KEY,
    retention_days_by_importance JSONB NOT NULL DEFAULT '{}'::jsonb,
    default_importance SMALLINT NOT NULL DEFAULT 3 CHECK (default_importance BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_governance_policy_workspace_idx ON memory_governance_policy(workspace_id);
