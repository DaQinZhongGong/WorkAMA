-- 078_performance_optimization.sql
-- P2 性能优化专项：慢查询日志 / 缓存命中统计 / 性能基准测试三张表。
-- 配合 apps/platform-api/src/workama_platform/modules/performance.py 使用。

-- ============================================================================
-- 1. 慢查询日志表（SlowQueryMiddleware 写入，超过阈值默认 200ms 的请求）
-- ============================================================================
CREATE TABLE IF NOT EXISTS ops_slow_query_log (
    id BIGSERIAL PRIMARY KEY,
    workspace_id TEXT,
    query_text TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_agent TEXT,
    path TEXT
);
-- 复合索引：按 workspace + 时间倒序 + 耗时倒序过滤/排序（列表主路径）
CREATE INDEX IF NOT EXISTS idx_ops_slow_query_log_workspace_created_duration
    ON ops_slow_query_log (workspace_id, created_at DESC, duration_ms DESC);
-- 全局耗时倒序索引：跨工作区 TOP-N 慢查询分析
CREATE INDEX IF NOT EXISTS idx_ops_slow_query_log_duration
    ON ops_slow_query_log (duration_ms DESC);

-- ============================================================================
-- 2. 缓存命中统计表（cached_with_stats 装饰器写入）
-- ============================================================================
CREATE TABLE IF NOT EXISTS ops_cache_stats (
    workspace_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    hits BIGINT NOT NULL DEFAULT 0 CHECK (hits >= 0),
    misses BIGINT NOT NULL DEFAULT 0 CHECK (misses >= 0),
    last_hit_at TIMESTAMPTZ,
    last_miss_at TIMESTAMPTZ,
    UNIQUE (workspace_id, resource)
);

-- ============================================================================
-- 3. 性能基准测试表（POST /benchmark 异步执行，GET /benchmarks/{id} 查询）
-- ============================================================================
CREATE TABLE IF NOT EXISTS ops_performance_benchmark (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    operation_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed')),
    scenarios JSONB NOT NULL DEFAULT '[]'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ops_performance_benchmark_workspace_created
    ON ops_performance_benchmark (workspace_id, created_at DESC);
