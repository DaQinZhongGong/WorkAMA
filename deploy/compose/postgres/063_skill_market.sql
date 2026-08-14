-- v7.161: 技能市场与 Agent 技能挂载模块
-- 表：skill_package / skill_install / skill_invocation_log

CREATE TABLE IF NOT EXISTS skill_package (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    manifest_url TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    tags TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
    downloads INTEGER NOT NULL DEFAULT 0,
    rating NUMERIC(2,1) NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','published','archived')),
    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS skill_install (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    package_id TEXT NOT NULL REFERENCES skill_package(id) ON DELETE CASCADE,
    installed_version TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'installed'
        CHECK (status IN ('installed','error')),
    installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, package_id)
);

CREATE TABLE IF NOT EXISTS skill_invocation_log (
    id TEXT PRIMARY KEY,
    install_id TEXT NOT NULL REFERENCES skill_install(id) ON DELETE CASCADE,
    input JSONB NOT NULL DEFAULT '{}'::jsonb,
    output JSONB NOT NULL DEFAULT '{}'::jsonb,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_skill_package_status ON skill_package(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_package_tags ON skill_package USING gin(tags);
CREATE INDEX IF NOT EXISTS idx_skill_install_workspace ON skill_install(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_skill_invocation_log_install ON skill_invocation_log(install_id, created_at DESC);
