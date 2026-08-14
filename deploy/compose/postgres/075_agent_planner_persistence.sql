-- T-M4-005: Multi-Agent Planner persistence & recovery
-- Extends 062_agent_planner.sql / 066_agent_planner_v2.sql with checkpoint/recovery
-- support and a dedicated checkpoint history table.
--
-- 注意：任务原计划命名为 070_agent_planner_persistence.sql，但 070 已被
-- 070_connectors_v2.sql 占用，故改用 075 以避免冲突（不修改其他业务模块）。

-- 1. 会话恢复状态列：planner_session_state 独立追踪恢复生命周期（active/paused/
--    completed/failed/recovering），不改动既有 status 列的 CHECK 约束，保持向后兼容。
ALTER TABLE ag_planner_session
  ADD COLUMN IF NOT EXISTS planner_session_state TEXT NOT NULL DEFAULT 'active'
    CHECK (planner_session_state IN ('active','paused','completed','failed','recovering')),
  ADD COLUMN IF NOT EXISTS last_checkpoint_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS checkpoint_data JSONB NOT NULL DEFAULT '{}'::jsonb;

-- 2. Worker 恢复扫描索引：仅索引 recovering 行，workspace 维度扫描高效。
CREATE INDEX IF NOT EXISTS idx_ag_planner_session_recovering
  ON ag_planner_session(workspace_id, planner_session_state, updated_at DESC)
  WHERE planner_session_state = 'recovering';

-- 3. 检查点历史表：保存完整可恢复状态快照。
CREATE TABLE IF NOT EXISTS ag_planner_checkpoint (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES ag_planner_session(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  checkpoint_data JSONB NOT NULL DEFAULT '{}'::jsonb,
  size_bytes BIGINT NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
  label TEXT,
  created_by TEXT REFERENCES id_user(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ag_planner_checkpoint_session ON ag_planner_checkpoint(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ag_planner_checkpoint_workspace ON ag_planner_checkpoint(workspace_id, created_at DESC);
