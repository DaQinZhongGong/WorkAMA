-- 054_mcp_server.sql
-- v7.147: P1 身份/审查/MCP 模块 - MCP 工具注册表
-- 存储 workspace 内注册的 MCP 工具及其 Python handler 引用

CREATE TABLE IF NOT EXISTS mcp_tool (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    input_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_schema JSONB,
    kind TEXT NOT NULL DEFAULT 'function',  -- function/resource/prompt
    handler TEXT,  -- Python 可调用函数名（如 "workama_platform.modules.mcp_server.builtin_get_current_time"）
    status TEXT NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, name)
);

CREATE INDEX IF NOT EXISTS mcp_tool_workspace_idx ON mcp_tool(workspace_id);
CREATE INDEX IF NOT EXISTS mcp_tool_status_idx ON mcp_tool(status);
