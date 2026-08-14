-- 052_knowledge_base.sql
-- v7.146: P1 知识库/RAG 增强模块
-- 知识库 / 文档 / 切片（pgvector 1536 维余弦相似度）

CREATE TABLE IF NOT EXISTS knowledge_base (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    kind TEXT NOT NULL DEFAULT 'general',  -- general/code/faq/product/policy
    embedding_model TEXT NOT NULL DEFAULT 'text-embedding-3-small',
    embedding_dimensions INTEGER NOT NULL DEFAULT 1536,
    chunk_size INTEGER NOT NULL DEFAULT 800,
    chunk_overlap INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_document (
    id TEXT PRIMARY KEY,
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_base(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'manual',  -- manual/upload/api/crawl
    source_url TEXT,
    content TEXT NOT NULL,
    content_hash TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/processing/ready/failed
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_chunk (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES knowledge_document(id) ON DELETE CASCADE,
    knowledge_base_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    content TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    embedding vector(1536),
    token_count INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kb_workspace_idx ON knowledge_base(workspace_id);
CREATE INDEX IF NOT EXISTS kb_doc_kb_idx ON knowledge_document(knowledge_base_id);
CREATE INDEX IF NOT EXISTS kb_doc_workspace_idx ON knowledge_document(workspace_id);
CREATE INDEX IF NOT EXISTS kb_chunk_doc_idx ON knowledge_chunk(document_id);
CREATE INDEX IF NOT EXISTS kb_chunk_kb_idx ON knowledge_chunk(knowledge_base_id);
CREATE INDEX IF NOT EXISTS kb_chunk_workspace_idx ON knowledge_chunk(workspace_id);
CREATE INDEX IF NOT EXISTS kb_chunk_embedding_idx ON knowledge_chunk USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
