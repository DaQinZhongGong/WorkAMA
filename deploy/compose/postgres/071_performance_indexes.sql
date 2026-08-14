-- 071_performance_indexes.sql
-- v7.165: Gateway 503 根因修复配套性能优化索引
-- 补充高频查询表缺失的复合索引，降低连接池等待与全表扫描。

-- memory_vector: 按 workspace + kind + created_at 过滤/排序
CREATE INDEX IF NOT EXISTS idx_memory_vector_workspace_kind_created
    ON memory_vector(workspace_id, kind, created_at);

-- knowledge_chunk: 按 knowledge_base + created_at 过滤/排序
CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_kb_created
    ON knowledge_chunk(knowledge_base_id, created_at);

-- gw_subscription_account: 按 workspace + status 查询活跃订阅
CREATE INDEX IF NOT EXISTS idx_gw_subscription_account_workspace_status
    ON gw_subscription_account(workspace_id, status);

-- ops_async_operation: 按 workspace + status + created_at 列表/扫描
CREATE INDEX IF NOT EXISTS idx_ops_async_operation_workspace_status_created
    ON ops_async_operation(workspace_id, status, created_at);

-- pf_workflow_run: 按 workspace + status + created_at 列表/扫描
CREATE INDEX IF NOT EXISTS idx_pf_workflow_run_workspace_status_created
    ON pf_workflow_run(workspace_id, status, created_at);
