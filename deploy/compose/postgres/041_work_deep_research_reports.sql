-- AMA-Work deep research execution mode and report artifact metadata.
-- Reports are generated only from the controlled local source boundary unless
-- an approved external browser/provider is configured in a later environment.

ALTER TABLE work_execution
    ADD COLUMN IF NOT EXISTS execution_mode TEXT NOT NULL DEFAULT 'plan';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'work_execution_execution_mode_check'
    ) THEN
        ALTER TABLE work_execution
            ADD CONSTRAINT work_execution_execution_mode_check
            CHECK (execution_mode IN ('plan', 'deep_research'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_work_execution_mode
    ON work_execution(workspace_id, execution_mode, created_at DESC);
