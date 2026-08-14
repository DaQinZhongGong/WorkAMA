-- v7.151 P1 工作流编排模块 (workflow)
-- DAG 编排：节点（llm_call/tool_call/rag_query/memory_recall/memory_extract/condition/output）+ 边
-- 与既有 pf_workflow 表独立共存（pf_workflow 由 workflows.py 使用，本表由 workflow.py 使用）
CREATE TABLE IF NOT EXISTS workflow (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    nodes JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 节点定义数组
    edges JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 边定义数组
    status TEXT NOT NULL DEFAULT 'draft',  -- draft/published/archived
    version INTEGER NOT NULL DEFAULT 1,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS workflow_run (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    input JSONB NOT NULL DEFAULT '{}'::jsonb,
    output JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/running/completed/failed
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS workflow_workspace_idx ON workflow(workspace_id);
CREATE INDEX IF NOT EXISTS workflow_run_workflow_idx ON workflow_run(workflow_id);
