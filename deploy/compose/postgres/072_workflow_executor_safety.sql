-- v7.160 T-M5-003 工作流生产执行器安全增强迁移
-- 嵌套限制/审批超时/Loop安全/可观测性

-- pf_workflow_run（workflows.py 主执行器）
ALTER TABLE pf_workflow_run ADD COLUMN IF NOT EXISTS error_category TEXT;
ALTER TABLE pf_workflow_run ADD COLUMN IF NOT EXISTS timeout_at TIMESTAMPTZ;
ALTER TABLE pf_workflow_run ADD COLUMN IF NOT EXISTS iteration_count INTEGER;
ALTER TABLE pf_workflow_run ADD COLUMN IF NOT EXISTS nesting_depth INTEGER;

-- workflow_run（workflow.py P1 编排模块）
ALTER TABLE workflow_run ADD COLUMN IF NOT EXISTS error_category TEXT;
ALTER TABLE workflow_run ADD COLUMN IF NOT EXISTS timeout_at TIMESTAMPTZ;
ALTER TABLE workflow_run ADD COLUMN IF NOT EXISTS iteration_count INTEGER;
ALTER TABLE workflow_run ADD COLUMN IF NOT EXISTS nesting_depth INTEGER;
