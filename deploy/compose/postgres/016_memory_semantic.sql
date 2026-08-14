-- Deterministic local semantic recall and explicit forgetting metadata.
ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS importance NUMERIC(4,3) NOT NULL DEFAULT 0.5;
ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS confidence NUMERIC(4,3) NOT NULL DEFAULT 0.5;
ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS retention_policy TEXT NOT NULL DEFAULT 'standard';
ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS semantic_embedding JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS semantic_version TEXT NOT NULL DEFAULT 'local-hash-v1';
ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ;
ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS forgotten_at TIMESTAMPTZ;
ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS forget_reason TEXT;
ALTER TABLE ag_memory DROP CONSTRAINT IF EXISTS ag_memory_kind_check;
ALTER TABLE ag_memory ADD CONSTRAINT ag_memory_kind_check CHECK (kind IN ('profile','episodic','semantic'));
ALTER TABLE ag_memory DROP CONSTRAINT IF EXISTS ag_memory_retention_policy_check;
ALTER TABLE ag_memory ADD CONSTRAINT ag_memory_retention_policy_check CHECK (retention_policy IN ('standard','session','indefinite'));
ALTER TABLE ag_memory DROP CONSTRAINT IF EXISTS ag_memory_importance_check;
ALTER TABLE ag_memory ADD CONSTRAINT ag_memory_importance_check CHECK (importance >= 0 AND importance <= 1);
ALTER TABLE ag_memory DROP CONSTRAINT IF EXISTS ag_memory_confidence_check;
ALTER TABLE ag_memory ADD CONSTRAINT ag_memory_confidence_check CHECK (confidence >= 0 AND confidence <= 1);
CREATE INDEX IF NOT EXISTS idx_ag_memory_semantic_recall
    ON ag_memory(workspace_id, user_id, status, updated_at DESC);
