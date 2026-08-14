-- 065_memory_governance.sql
-- v7.164-A: 记忆完整形态（跨会话记忆治理、人工标注回流、遗忘曲线增强）

-- 记忆治理日志：记录去重、合并、遗忘等治理操作
CREATE TABLE IF NOT EXISTS memory_vector_governance_log (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('deduplicate', 'merge', 'forget', 'annotate', 'reindex')),
    source_vector_id TEXT,
    target_vector_id TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_vector_governance_workspace_idx ON memory_vector_governance_log(workspace_id);
CREATE INDEX IF NOT EXISTS memory_vector_governance_action_idx ON memory_vector_governance_log(action);
CREATE INDEX IF NOT EXISTS memory_vector_governance_created_idx ON memory_vector_governance_log(created_at);

-- 人工标注回流：用户/运营对记忆的相关性、准确性反馈
CREATE TABLE IF NOT EXISTS memory_vector_annotation (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    vector_id TEXT NOT NULL,
    relevance_score NUMERIC(3,2) CHECK (relevance_score BETWEEN 0.00 AND 1.00),
    accuracy_score NUMERIC(3,2) CHECK (accuracy_score BETWEEN 0.00 AND 1.00),
    feedback TEXT CHECK (length(feedback) <= 2000),
    actor_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_vector_annotation_vector_idx ON memory_vector_annotation(vector_id);
CREATE INDEX IF NOT EXISTS memory_vector_annotation_workspace_idx ON memory_vector_annotation(workspace_id);

-- 记忆引用计数（用于调整遗忘曲线）
ALTER TABLE memory_vector ADD COLUMN IF NOT EXISTS reference_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS memory_vector_reference_count_idx ON memory_vector(reference_count);
