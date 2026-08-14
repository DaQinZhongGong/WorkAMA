-- WorkAMA P1 T-M7-005 AMA-Work plan/task/event/citation/artifact metadata.
-- Office bytes use the existing artifacts/{workspace}/... MinIO key layout;
-- this migration stores metadata only and never stores provider credentials.

CREATE TABLE IF NOT EXISTS work_plan (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES ag_session(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    objective TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'ready', 'running', 'paused', 'succeeded', 'failed', 'cancelled')),
    last_event_seq BIGINT NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS work_task (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    plan_id TEXT NOT NULL REFERENCES work_plan(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL CHECK (position >= 0),
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo', 'in_progress', 'blocked', 'done', 'failed', 'cancelled')),
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(plan_id, position)
);

CREATE TABLE IF NOT EXISTS work_event (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    plan_id TEXT NOT NULL REFERENCES work_plan(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES work_task(id) ON DELETE SET NULL,
    seq BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(plan_id, seq)
);

CREATE TABLE IF NOT EXISTS work_citation (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    plan_id TEXT NOT NULL REFERENCES work_plan(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES work_task(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('https', 'mock')),
    url TEXT NOT NULL,
    title TEXT,
    excerpt TEXT,
    content_sha256 TEXT,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(plan_id, url)
);

CREATE TABLE IF NOT EXISTS work_artifact (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
    plan_id TEXT NOT NULL REFERENCES work_plan(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES work_task(id) ON DELETE SET NULL,
    artifact_id TEXT REFERENCES ag_artifact(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'office',
    content_type TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    content_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('pending', 'ready', 'failed')),
    preview JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL REFERENCES id_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_work_plan_workspace_time
    ON work_plan(workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_task_plan_position
    ON work_task(workspace_id, plan_id, position);
CREATE INDEX IF NOT EXISTS idx_work_event_plan_seq
    ON work_event(workspace_id, plan_id, seq);
CREATE INDEX IF NOT EXISTS idx_work_citation_plan_time
    ON work_citation(workspace_id, plan_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_work_artifact_plan_time
    ON work_artifact(workspace_id, plan_id, created_at DESC);
