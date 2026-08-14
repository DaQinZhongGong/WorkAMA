-- AMA-Code repository/task/event foundation. Safe to apply after 001_init.sql.

CREATE TABLE IF NOT EXISTS code_repository (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'local'
        CHECK (provider IN ('local', 'github', 'gitlab', 'generic')),
    remote_url TEXT,
    default_branch TEXT NOT NULL DEFAULT 'main',
    credential_enc TEXT,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, name)
);

CREATE TABLE IF NOT EXISTS code_task (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    repository_id TEXT REFERENCES code_repository(id) ON DELETE SET NULL,
    session_id TEXT REFERENCES ag_session(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled')),
    last_event_seq BIGINT NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS code_event (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES code_task(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    seq BIGINT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('code.diff', 'terminal.output', 'test.report')),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(task_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_code_repository_workspace_time
    ON code_repository(workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_code_task_workspace_time
    ON code_task(workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_code_task_session_time
    ON code_task(workspace_id, session_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_code_event_task_seq
    ON code_event(workspace_id, task_id, seq);

