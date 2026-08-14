-- v7.151 P1 助手模块 (assistant)
-- 整合 gateway LLM + knowledge_base RAG + memory_vector + mcp_server 工具调用
-- 与既有 pf_assistant 表独立共存（pf_assistant 由 workflows.py 使用，本表由 assistant.py 使用）
CREATE TABLE IF NOT EXISTS assistant (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    system_prompt TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'gpt-4o-mini',
    temperature REAL NOT NULL DEFAULT 0.7,
    max_tokens INTEGER NOT NULL DEFAULT 2048,
    tools JSONB NOT NULL DEFAULT '[]'::jsonb,  -- MCP tool ids
    knowledge_base_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    memory_enabled BOOLEAN NOT NULL DEFAULT true,
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS assistant_run (
    id TEXT PRIMARY KEY,
    assistant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    assistant_message TEXT,
    model TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'completed',  -- pending/running/completed/failed
    error TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assistant_workspace_idx ON assistant(workspace_id);
CREATE INDEX IF NOT EXISTS assistant_run_assistant_idx ON assistant_run(assistant_id);
CREATE INDEX IF NOT EXISTS assistant_run_workspace_idx ON assistant_run(workspace_id);
