-- WorkAMA Automation v2: triggers and trigger runs with enhanced executor support.
CREATE TABLE IF NOT EXISTS automation_trigger (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('cron','event','webhook')),
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    executor_type TEXT NOT NULL CHECK (executor_type IN ('agent','workflow','script')),
    executor_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','archived')),
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, name)
);

CREATE TABLE IF NOT EXISTS automation_trigger_run (
    id TEXT PRIMARY KEY,
    trigger_id TEXT NOT NULL REFERENCES automation_trigger(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    trigger_source TEXT NOT NULL CHECK (trigger_source IN ('cron','event','webhook','manual')),
    idempotency_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error_code TEXT,
    error_message TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(trigger_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_automation_trigger_due ON automation_trigger(workspace_id, enabled, status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_automation_trigger_run_trigger_time ON automation_trigger_run(trigger_id, created_at DESC);
