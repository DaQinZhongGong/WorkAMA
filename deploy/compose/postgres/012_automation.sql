-- AMA-Work automation schedules and idempotent trigger records.
CREATE TABLE IF NOT EXISTS ops_automation_schedule (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('cron','webhook')),
    cron_expression TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    target_type TEXT NOT NULL CHECK (target_type IN ('work_plan','workflow','agent')),
    target_id TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    webhook_secret_hash TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, name)
);
CREATE TABLE IF NOT EXISTS ops_automation_run (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES ops_automation_schedule(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    trigger_source TEXT NOT NULL CHECK (trigger_source IN ('cron','webhook','manual')),
    idempotency_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    triggered_by TEXT REFERENCES id_user(id) ON DELETE SET NULL,
    operation_id TEXT REFERENCES ops_async_operation(id) ON DELETE SET NULL,
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE(schedule_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_ops_automation_schedule_due ON ops_automation_schedule(workspace_id, enabled, status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_ops_automation_run_schedule_time ON ops_automation_run(schedule_id, created_at DESC);
