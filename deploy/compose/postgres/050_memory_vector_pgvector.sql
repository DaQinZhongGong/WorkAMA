-- 050_memory_vector_pgvector.sql
-- v7.136: 记忆真实向量索引（pgvector 1536 维余弦相似度）
-- 替换 v7.135 ensure_runtime_schema() 创建的占位表（旧表仅有 memory_id PK /
-- workspace_id / content / embedding / metadata / created_at / updated_at /
-- last_accessed_at / access_count，无 kind / importance / last_referenced_at /
-- expires_at，且无任何端点写入数据，可安全 DROP 后重建）。

CREATE EXTENSION IF NOT EXISTS vector;

-- Drop the stub table created by ensure_runtime_schema() (v7.135).
-- It has a different schema (memory_id PK) and no data was written to it
-- (the v7.135 stub had no write endpoints).
DROP TABLE IF EXISTS memory_vector;

CREATE TABLE memory_vector (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    memory_id TEXT,  -- 关联 ag_memory 表，可空
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'semantic',
    importance SMALLINT NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    embedding vector(1536) NOT NULL,
    last_referenced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS memory_vector_workspace_idx ON memory_vector(workspace_id);
CREATE INDEX IF NOT EXISTS memory_vector_kind_idx ON memory_vector(kind);
CREATE INDEX IF NOT EXISTS memory_vector_importance_idx ON memory_vector(importance);
-- ivfflat 余弦索引（1536 维，lists=100）
CREATE INDEX IF NOT EXISTS memory_vector_embedding_idx
    ON memory_vector USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
