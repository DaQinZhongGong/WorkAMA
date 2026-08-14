-- v7.164-B: Multi-Agent Planner v2 schema evolution
-- Extends 062_agent_planner.sql with parent_session_id, convergence_score, dedup_hash

-- Session table extensions
ALTER TABLE ag_planner_session
  ADD COLUMN IF NOT EXISTS parent_session_id TEXT REFERENCES ag_planner_session(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS convergence_score NUMERIC(5,4) DEFAULT NULL CHECK (convergence_score >= 0 AND convergence_score <= 1),
  ADD COLUMN IF NOT EXISTS dedup_hash TEXT DEFAULT NULL;

-- Index for parent_session_id lookups (sub-session bridging / fork queries)
CREATE INDEX IF NOT EXISTS idx_ag_planner_session_parent ON ag_planner_session(parent_session_id);

-- Index for dedup_hash lookups (plan deduplication)
CREATE INDEX IF NOT EXISTS idx_ag_planner_session_dedup ON ag_planner_session(workspace_id, dedup_hash);
