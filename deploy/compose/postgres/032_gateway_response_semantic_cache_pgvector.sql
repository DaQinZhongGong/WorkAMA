-- Optional Responses semantic-cache storage. If pgvector is unavailable or the
-- database role cannot install it, the migration leaves the optional table
-- absent; Gateway runtime lookup is fail-closed in that case.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS vector;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pgvector extension is unavailable; skipping Responses semantic cache storage';
        END;
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'vector') THEN
        EXECUTE $ddl$
            CREATE TABLE IF NOT EXISTS gw_response_semantic_cache (
                cache_key TEXT PRIMARY KEY,
                completion_text TEXT NOT NULL CHECK (octet_length(completion_text) > 0 AND octet_length(completion_text) <= 65536),
                completion_tokens INTEGER NOT NULL CHECK (completion_tokens >= 0),
                workspace_id TEXT NOT NULL,
                model TEXT NOT NULL,
                provider TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                upstream_model TEXT NOT NULL,
                capability TEXT NOT NULL,
                prompt_id TEXT NOT NULL DEFAULT '',
                prompt_version INTEGER NOT NULL DEFAULT 0 CHECK (prompt_version >= 0),
                prompt_checksum TEXT NOT NULL DEFAULT '',
                guard_policy_version TEXT NOT NULL,
                data_classification TEXT NOT NULL CHECK (data_classification IN ('C0', 'C1', 'C2')),
                output_signature TEXT NOT NULL,
                region TEXT NOT NULL,
                embedding vector(128) NOT NULL,
                temperature NUMERIC(4,3) NOT NULL DEFAULT 0 CHECK (temperature = 0),
                tools_present BOOLEAN NOT NULL DEFAULT FALSE CHECK (tools_present = FALSE),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL,
                CHECK (expires_at > created_at)
            )
        $ddl$;
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_gw_response_semantic_cache_scope ON gw_response_semantic_cache (workspace_id, model, provider, capability, prompt_version, prompt_checksum, guard_policy_version, output_signature, region, expires_at)';
        BEGIN
            EXECUTE 'CREATE INDEX IF NOT EXISTS idx_gw_response_semantic_cache_embedding ON gw_response_semantic_cache USING hnsw (embedding vector_cosine_ops)';
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pgvector HNSW index unavailable; semantic cache table remains usable without the index';
        END;
    ELSE
        RAISE NOTICE 'pgvector type is unavailable; skipping Responses semantic cache table';
    END IF;
END
$$;
