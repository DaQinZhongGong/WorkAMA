-- v7.151 P2 M5 工作流 v2 版本快照 / 回滚 / 对比支持
-- 为 workflow.py (v2 编排模块) 提供版本快照表，支持：
--   - 创建版本快照（version 自增，snapshot 存完整 workflow 定义）
--   - 列表/详情查询
--   - 回滚到指定版本（先快照当前 → 恢复目标 → 新建快照）
--   - 两个版本之间的差异对比（节点增删改 / 边增删改 / metadata 变更）
-- 与既有 pf_workflow_version 表独立共存（pf_workflow_version 由 workflows.py 使用）
CREATE TABLE IF NOT EXISTS workflow_v2_version_snapshot (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL REFERENCES workflow(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    snapshot JSONB NOT NULL,          -- 完整 workflow 定义（nodes/edges/metadata/name/description/status）
    changelog TEXT,                   -- 可选变更说明
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, workflow_id, version)
);
CREATE INDEX IF NOT EXISTS workflow_v2_version_snapshot_workspace_idx
    ON workflow_v2_version_snapshot(workspace_id);
CREATE INDEX IF NOT EXISTS workflow_v2_version_snapshot_workflow_idx
    ON workflow_v2_version_snapshot(workflow_id);
CREATE INDEX IF NOT EXISTS workflow_v2_version_snapshot_version_idx
    ON workflow_v2_version_snapshot(workflow_id, version DESC);
CREATE INDEX IF NOT EXISTS workflow_v2_version_snapshot_created_idx
    ON workflow_v2_version_snapshot(workflow_id, created_at DESC);
