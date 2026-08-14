-- Multi-Agent Planner session and step persistence
CREATE TABLE IF NOT EXISTS ag_planner_session (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
  actor_id TEXT NOT NULL REFERENCES id_user(id),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','completed','failed')),
  context JSONB NOT NULL DEFAULT '{}'::jsonb,
  plan JSONB NOT NULL DEFAULT '{}'::jsonb,
  iterations INTEGER NOT NULL DEFAULT 0 CHECK (iterations >= 0),
  budget_used NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (budget_used >= 0),
  budget_limit NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (budget_limit >= 0),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ag_planner_session_workspace ON ag_planner_session(workspace_id,status,updated_at DESC);

CREATE TABLE IF NOT EXISTS ag_planner_step (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES ag_planner_session(id) ON DELETE CASCADE,
  step_order INTEGER NOT NULL CHECK (step_order >= 0),
  action TEXT NOT NULL,
  tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
  observations JSONB NOT NULL DEFAULT '[]'::jsonb,
  next_choices JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','skipped')),
  cost NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (cost >= 0),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ag_planner_step_session ON ag_planner_step(session_id,step_order);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ag_planner_step_session_order ON ag_planner_step(session_id,step_order);
