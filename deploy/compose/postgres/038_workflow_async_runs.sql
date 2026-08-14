ALTER TABLE pf_workflow_run
  ADD COLUMN IF NOT EXISTS operation_id TEXT REFERENCES ops_async_operation(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_pf_workflow_run_operation
  ON pf_workflow_run(operation_id);
