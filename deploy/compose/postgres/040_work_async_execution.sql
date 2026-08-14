-- AMA-Work execution orchestration uses the shared async operation/job runtime.
-- The operation row remains the source of truth for queue, progress, retry, and cancellation;
-- this table binds one execution request to its plan and source selection.
CREATE TABLE IF NOT EXISTS work_execution (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    plan_id TEXT NOT NULL REFERENCES work_plan(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL UNIQUE REFERENCES ops_async_operation(id) ON DELETE CASCADE,
    source_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    requested_by TEXT NOT NULL REFERENCES id_user(id),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_work_execution_plan_time
    ON work_execution(workspace_id, plan_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_execution_operation
    ON work_execution(operation_id);
