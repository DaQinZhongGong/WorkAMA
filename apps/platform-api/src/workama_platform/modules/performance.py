"""P2 性能优化专项模块 (performance)。

提供：
- 8 个 REST 端点（prefix=/api/v1/performance）：
    * GET  /metrics          — 当前进程关键指标（admin）
    * GET  /slow-queries     — 慢查询日志分页查询（admin）
    * POST /cache/invalidate — 按 workspace + resource 模式失效缓存（admin）
    * GET  /cache/stats      — workspace 级缓存命中率统计（admin）
    * POST /benchmark        — 触发性能基准测试，返回 operation_id（owner/admin）
    * GET  /benchmarks/{op}  — 查询基准测试结果（owner/admin）
    * GET  /health-check     — 性能健康检查（admin）
    * POST /query-explain    — EXPLAIN ANALYZE，仅允许 SELECT（admin）
- ``SlowQueryMiddleware``：ASGI 中间件，记录超过阈值（默认 200ms）的请求到
  ``ops_slow_query_log``；通过 ``register_middleware(app)`` 注册。
- ``cached_with_stats(workspace_id, resource, ttl=60)`` 装饰器：自动记录
  cache hits/misses 到 ``ops_cache_stats`` 表。

设计原则：
- 所有写操作使用 ``pool.connection()`` 上下文管理器。
- SQL 全部参数化，防注入。
- 慢查询日志 / 缓存命中统计失败时静默降级，绝不影响主流程。
- 所有端点 workspace 隔离。
- query-explain 严格白名单 SELECT，拒绝 DDL/DML。
"""
from __future__ import annotations

import functools
import hashlib
import logging
import re
import shutil
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    cache_delete_pattern,
    cache_get,
    cache_set,
    capability_allows,
    get_actor,
    json_dumps,
    new_id,
    pool,
    redis,
)

logger = logging.getLogger("workama_platform.performance")

router = APIRouter(prefix="/api/v1/performance", tags=["performance"])

# ============================================================================
# 常量
# ============================================================================

SLOW_QUERY_THRESHOLD_MS = 200
_PROCESS_START = time.monotonic()
_CACHE_STATS_KEY_PREFIX = "workama:cache"

# 禁止的 SQL 关键字（DDL/DML/事务控制等），query-explain 仅允许 SELECT
_FORBIDDEN_SQL_KEYWORDS: tuple[str, ...] = (
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE",
    "GRANT", "REVOKE", "VACUUM", "MERGE", "CALL", "COPY", "REINDEX",
    "CLUSTER", "REFRESH", "ATTACH", "DETACH", "COMMENT",
)
# SQL 行注释 / 块注释剥离正则
_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
# 首个关键字提取
_SQL_FIRST_TOKEN_RE = re.compile(r"^\s*([A-Za-z]+)")


# ============================================================================
# 进程级指标（SlowQueryMiddleware 更新）
# ============================================================================

# 进程级请求计数与累计响应时间（async 单线程，无需锁）
_request_count: int = 0
_total_response_time_ms: float = 0.0


def _record_request_metrics(duration_ms: float) -> None:
    """由 SlowQueryMiddleware 调用，记录每个请求的耗时（best-effort）。"""
    global _request_count, _total_response_time_ms
    _request_count += 1
    _total_response_time_ms += float(duration_ms)


def reset_metrics_for_tests() -> None:
    """测试辅助：重置进程级计数器。"""
    global _request_count, _total_response_time_ms
    _request_count = 0
    _total_response_time_ms = 0.0


# ============================================================================
# Pydantic 数据模型
# ============================================================================


class CacheInvalidateRequest(BaseModel):
    """按 workspace + resource 模式失效缓存。"""

    workspace_id: str = Field(min_length=1, max_length=200)
    resource: str = Field(min_length=1, max_length=200)


class QueryExplainRequest(BaseModel):
    """EXPLAIN ANALYZE 请求体。"""

    sql: str = Field(min_length=1, max_length=20000)


class BenchmarkRequest(BaseModel):
    """触发性能基准测试。"""

    scenarios: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# 鉴权辅助
# ============================================================================


def _require_admin(actor: Actor) -> None:
    """admin/owner 才能访问；其他角色返回 403。"""
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


def _require_owner_or_admin(actor: Actor) -> None:
    """owner/admin 才能访问；其他角色返回 403。"""
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Owner or admin role required")


# ============================================================================
# SQL 白名单校验（query-explain 专用）
# ============================================================================


def _strip_sql_comments(sql: str) -> str:
    """剥离 SQL 行注释（-- ...）与块注释（/* ... */）。"""
    no_block = _SQL_BLOCK_COMMENT_RE.sub(" ", sql)
    return _SQL_LINE_COMMENT_RE.sub("", no_block)


def _validate_select_only(sql: str) -> str:
    """严格白名单：仅允许 SELECT（含 WITH ... SELECT）语句。

    - 剥离注释后取首个关键字，必须为 SELECT 或 WITH。
    - 任意位置出现 DDL/DML 关键字（作为独立单词）即拒绝。
    - WITH 必须最终落到 SELECT（粗粒度校验：语句需包含 SELECT 关键字）。
    返回剥离注释后的 SQL 供 EXPLAIN 使用。
    """
    cleaned = _strip_sql_comments(sql).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="SQL statement is empty")
    first = _SQL_FIRST_TOKEN_RE.match(cleaned)
    if not first:
        raise HTTPException(status_code=400, detail="Cannot parse SQL statement")
    first_kw = first.group(1).upper()
    if first_kw not in {"SELECT", "WITH"}:
        raise HTTPException(
            status_code=400,
            detail=f"Only SELECT statements are allowed (got {first_kw})",
        )
    if first_kw == "WITH" and not re.search(r"\bSELECT\b", cleaned, re.IGNORECASE):
        raise HTTPException(
            status_code=400,
            detail="WITH statement must contain a SELECT clause",
        )
    # 任意位置出现禁止关键字（独立单词）即拒绝
    upper = cleaned.upper()
    for kw in _FORBIDDEN_SQL_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", upper):
            raise HTTPException(
                status_code=400,
                detail=f"Forbidden SQL keyword: {kw}",
            )
    return cleaned


# ============================================================================
# 缓存命中统计（写 ops_cache_stats，失败静默）
# ============================================================================


async def _record_cache_hit(workspace_id: str, resource: str) -> None:
    """记录一次缓存命中，best-effort，失败静默降级。"""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO ops_cache_stats(workspace_id, resource, hits, last_hit_at)
                VALUES (%s, %s, 1, now())
                ON CONFLICT (workspace_id, resource)
                DO UPDATE SET hits = ops_cache_stats.hits + 1,
                              last_hit_at = now()
                """,
                (workspace_id, resource),
            )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("record_cache_hit failed (silent): %s", exc)


async def _record_cache_miss(workspace_id: str, resource: str) -> None:
    """记录一次缓存未命中，best-effort，失败静默降级。"""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO ops_cache_stats(workspace_id, resource, misses, last_miss_at)
                VALUES (%s, %s, 1, now())
                ON CONFLICT (workspace_id, resource)
                DO UPDATE SET misses = ops_cache_stats.misses + 1,
                              last_miss_at = now()
                """,
                (workspace_id, resource),
            )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("record_cache_miss failed (silent): %s", exc)


def cached_with_stats(
    workspace_id: str | Callable[..., str],
    resource: str,
    ttl: int = 60,
) -> Callable:
    """装饰器：缓存函数返回值并记录 hits/misses 到 ops_cache_stats。

    用法示例::

        @cached_with_stats(workspace_id="wsp_abc", resource="assistant:list", ttl=60)
        async def list_assistants(actor: Actor):
            ...  # 实际查 DB

        # workspace_id 动态取自参数（如 actor）时传入可调用对象：
        @cached_with_stats(
            workspace_id=lambda *a, **kw: kw["actor"].workspace_id,
            resource="memory:recall",
            ttl=30,
        )
        async def recall(actor: Actor, query: str):
            ...

    缓存 key 由 workspace_id + resource + 参数哈希构成，命中走 cache_get 并
    记录 hit；未命中执行函数、cache_set 写回并记录 miss。统计写入失败静默降级。
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            ws = workspace_id(*args, **kwargs) if callable(workspace_id) else workspace_id
            key_hash = hashlib.sha256(
                json_dumps({"args": list(args), "kwargs": kwargs}).encode()
            ).hexdigest()[:32]
            cache_key = f"{_CACHE_STATS_KEY_PREFIX}:{ws}:{resource}:{key_hash}"
            cached = await cache_get(cache_key)
            if cached is not None:
                # 缓存命中统计失败时静默降级，不影响主流程
                try:
                    await _record_cache_hit(ws, resource)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("record_cache_hit failed (silent): %s", exc)
                return cached
            try:
                await _record_cache_miss(ws, resource)
            except Exception as exc:  # noqa: BLE001
                logger.debug("record_cache_miss failed (silent): %s", exc)
            result = await func(*args, **kwargs)
            serialized = result if isinstance(result, str) else json_dumps(result)
            await cache_set(cache_key, serialized, ttl)
            return result

        return wrapper

    return decorator


# ============================================================================
# SlowQueryMiddleware（ASGI 中间件）
# ============================================================================


async def _extract_workspace_id(scope: dict[str, Any]) -> str | None:
    """从 ASGI scope 的 Authorization 头解析 workspace_id（best-effort）。"""
    headers = scope.get("headers") or []
    auth_header: str | None = None
    for name, value in headers:
        if name == b"authorization":
            try:
                auth_header = value.decode("latin-1")
            except UnicodeDecodeError:
                return None
            break
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token:
        return None
    # 复用 core 的 JWT 解码缓存（不抛异常，失败返回 None）
    try:
        from workama_platform.core import decode_token_cached

        payload = await decode_token_cached(token)
        return payload.get("ws")
    except Exception:  # noqa: BLE001
        return None


async def _log_slow_query(
    workspace_id: str | None,
    query_text: str,
    duration_ms: int,
    path: str | None,
    user_agent: str | None,
) -> None:
    """将慢查询记录写入 ops_slow_query_log，失败静默降级。"""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO ops_slow_query_log(
                    workspace_id, query_text, duration_ms, created_at, user_agent, path)
                VALUES (%s, %s, %s, now(), %s, %s)
                """,
                (workspace_id, query_text, duration_ms, user_agent, path),
            )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("log_slow_query failed (silent): %s", exc)


class SlowQueryMiddleware:
    """ASGI 中间件：记录耗时超过阈值的请求到 ops_slow_query_log。

    - 仅处理 http 类型 scope。
    - 测量从请求开始到响应完成的耗时。
    - 超过 ``threshold_ms``（默认 200ms）时静默写入慢查询日志。
    - 同时累计进程级请求指标（计数 / 总响应时间）。
    - DB 写入失败不影响请求本身。
    """

    def __init__(self, app: Any, threshold_ms: int = SLOW_QUERY_THRESHOLD_MS) -> None:
        self.app = app
        self.threshold_ms = threshold_ms

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        path = scope.get("path", "") or ""

        async def send_wrapper(message: dict[str, Any]) -> None:
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            _record_request_metrics(float(duration_ms))
            if duration_ms >= self.threshold_ms:
                user_agent = _scope_header(scope, "user-agent")
                # query_text 用 path + method 作为可读标识（不记录真实 SQL 文本）
                method = scope.get("method", "")
                query_text = f"{method} {path}"
                # 慢查询日志记录失败时静默降级，绝不影响请求本身
                try:
                    workspace_id = await _extract_workspace_id(scope)
                    await _log_slow_query(
                        workspace_id, query_text, duration_ms, path, user_agent
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("slow query log failed (silent): %s", exc)


def _scope_header(scope: dict[str, Any], name: str) -> str | None:
    """从 ASGI scope 读取指定头（小写匹配），返回解码字符串或 None。"""
    target = name.lower().encode("latin-1")
    for header_name, header_value in scope.get("headers") or []:
        if header_name == target:
            try:
                return header_value.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


def register_middleware(app: Any, threshold_ms: int = SLOW_QUERY_THRESHOLD_MS) -> None:
    """在 FastAPI 应用上注册 SlowQueryMiddleware。

    用法（main.py 入口）::

        from workama_platform.modules.performance import register_middleware
        register_middleware(app)
    """
    app.add_middleware(SlowQueryMiddleware, threshold_ms=threshold_ms)


# ============================================================================
# REST 端点
# ============================================================================


def _pool_stats() -> dict[str, Any] | None:
    """读取 psycopg 连接池统计，失败返回 None。"""
    try:
        stats = pool.get_stats()
        if isinstance(stats, dict):
            return stats
    except Exception:  # noqa: BLE001
        return None
    return None


@router.get("/metrics")
async def get_metrics(actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    """返回当前进程关键指标（admin）。"""
    _require_admin(actor)
    uptime_seconds = time.monotonic() - _PROCESS_START
    avg_response_ms = (
        _total_response_time_ms / _request_count if _request_count else 0.0
    )
    stats = _pool_stats()
    active_connections = stats.get("requests_waiting", 0) if stats else 0
    return {
        "uptime_seconds": round(uptime_seconds, 3),
        "request_count": _request_count,
        "avg_response_time_ms": round(avg_response_ms, 3),
        "active_connections": active_connections,
        "pool_stats": stats,
        "generated_at": datetime.now(UTC),
    }


@router.get("/slow-queries")
async def list_slow_queries(
    actor: Annotated[Actor, Depends(get_actor)],
    since: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """分页查询当前 workspace 的慢查询记录（admin）。"""
    _require_admin(actor)
    async with pool.connection() as conn:
        if since is not None:
            result = await conn.execute(
                """
                SELECT id, workspace_id, query_text, duration_ms, created_at, user_agent, path
                FROM ops_slow_query_log
                WHERE workspace_id = %s AND created_at >= %s
                ORDER BY created_at DESC, duration_ms DESC
                LIMIT %s
                """,
                (actor.workspace_id, since, limit),
            )
        else:
            result = await conn.execute(
                """
                SELECT id, workspace_id, query_text, duration_ms, created_at, user_agent, path
                FROM ops_slow_query_log
                WHERE workspace_id = %s
                ORDER BY created_at DESC, duration_ms DESC
                LIMIT %s
                """,
                (actor.workspace_id, limit),
            )
        rows = await result.fetchall()
    return {
        "items": rows,
        "count": len(rows),
        "limit": limit,
        "workspace_id": actor.workspace_id,
    }


@router.post("/cache/invalidate")
async def invalidate_cache(
    body: CacheInvalidateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """按 workspace_id + resource 模式失效缓存（admin）。

    失效范围限定在调用者所在 workspace，禁止跨工作区失效。
    """
    _require_admin(actor)
    if body.workspace_id != actor.workspace_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot invalidate cache for another workspace",
        )
    pattern = f"{_CACHE_STATS_KEY_PREFIX}:{actor.workspace_id}:{body.resource}:*"
    await cache_delete_pattern(pattern)
    return {
        "invalidated": True,
        "workspace_id": actor.workspace_id,
        "resource": body.resource,
        "pattern": pattern,
    }


@router.get("/cache/stats")
async def get_cache_stats(actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    """返回 workspace 级缓存命中率统计（admin）。"""
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT resource, hits, misses, last_hit_at, last_miss_at
            FROM ops_cache_stats
            WHERE workspace_id = %s
            ORDER BY (hits + misses) DESC
            """,
            (actor.workspace_id,),
        )
        rows = await result.fetchall()
    total_hits = sum(int(r.get("hits", 0)) for r in rows)
    total_misses = sum(int(r.get("misses", 0)) for r in rows)
    total_keys = len(rows)
    # Redis 端实际 key 数量（best-effort）
    size: int | None = None
    try:
        keys = await redis.keys(f"{_CACHE_STATS_KEY_PREFIX}:{actor.workspace_id}:*")
        size = len(keys) if keys else 0
    except Exception:  # noqa: BLE001
        size = None
    hit_rate = (
        round(total_hits / (total_hits + total_misses), 4)
        if (total_hits + total_misses)
        else 0.0
    )
    return {
        "workspace_id": actor.workspace_id,
        "hits": total_hits,
        "misses": total_misses,
        "keys": total_keys,
        "size": size,
        "hit_rate": hit_rate,
        "resources": rows,
    }


@router.post("/benchmark", status_code=status.HTTP_202_ACCEPTED)
async def create_benchmark(
    body: BenchmarkRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """触发一次性能基准测试（异步执行，返回 operation_id）（owner/admin）。"""
    _require_owner_or_admin(actor)
    benchmark_id = new_id("perf")
    operation_id = new_id("op")
    scenarios_payload = json_dumps(body.scenarios)
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO ops_performance_benchmark(
                    id, workspace_id, operation_id, status, scenarios, created_at)
                VALUES (%s, %s, %s, 'running', %s::jsonb, now())
                """,
                (
                    benchmark_id,
                    actor.workspace_id,
                    operation_id,
                    scenarios_payload,
                ),
            )
    # 异步执行钩子：默认 no-op，由外部 worker / 测试 monkeypatch 接管。
    await _run_benchmark_async(operation_id, actor.workspace_id, body.scenarios)
    return {
        "operation_id": operation_id,
        "benchmark_id": benchmark_id,
        "status": "running",
        "workspace_id": actor.workspace_id,
    }


async def _run_benchmark_async(
    operation_id: str,
    workspace_id: str,
    scenarios: list[dict[str, Any]],
) -> None:
    """默认占位实现：立即标记为 completed 并写入空结果。

    生产环境可由 platform-worker 接管真实基准测试；测试可通过 monkeypatch
    覆盖本函数以模拟异步执行。
    """
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE ops_performance_benchmark
                    SET status = 'completed',
                        result = %s::jsonb,
                        completed_at = now()
                    WHERE operation_id = %s AND workspace_id = %s
                    """,
                    (json_dumps({"scenarios_run": len(scenarios), "mock": True}),
                     operation_id, workspace_id),
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("run_benchmark_async failed (silent): %s", exc)


@router.get("/benchmarks/{operation_id}")
async def get_benchmark(
    operation_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """查询基准测试结果（owner/admin）。workspace 隔离。"""
    _require_owner_or_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, workspace_id, operation_id, status, scenarios, result,
                   created_at, completed_at
            FROM ops_performance_benchmark
            WHERE operation_id = %s AND workspace_id = %s
            """,
            (operation_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Benchmark not found")
    return dict(row)


@router.get("/health-check")
async def health_check(actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    """性能健康检查（admin）：DB / Redis 响应时间 + 内存 / 磁盘使用。"""
    _require_admin(actor)
    # DB 响应时间
    db_response_ms: float | None = None
    db_status = "ok"
    try:
        start = time.monotonic()
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        db_response_ms = round((time.monotonic() - start) * 1000, 3)
    except Exception as exc:  # noqa: BLE001
        db_status = "error"
        logger.debug("health-check db ping failed: %s", exc)

    # Redis 响应时间
    redis_response_ms: float | None = None
    redis_status = "ok"
    try:
        start = time.monotonic()
        await redis.ping()
        redis_response_ms = round((time.monotonic() - start) * 1000, 3)
    except Exception as exc:  # noqa: BLE001
        redis_status = "error"
        logger.debug("health-check redis ping failed: %s", exc)

    # 内存使用（RSS），best-effort
    memory_usage_mb: float | None = None
    try:
        import resource  # POSIX stdlib（容器内可用）

        # ru_maxrss 单位：Linux KB / macOS bytes；容器为 Linux
        memory_usage_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3)
    except Exception:  # noqa: BLE001
        memory_usage_mb = None

    # 磁盘使用（项目所在分区），best-effort
    disk_usage: dict[str, Any] | None = None
    try:
        usage = shutil.disk_usage("/")
        disk_usage = {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round(usage.used / usage.total * 100, 2) if usage.total else 0.0,
        }
    except Exception:  # noqa: BLE001
        disk_usage = None

    overall = "healthy" if (db_status == "ok" and redis_status == "ok") else "degraded"
    return {
        "status": overall,
        "db": {"status": db_status, "response_ms": db_response_ms},
        "redis": {"status": redis_status, "response_ms": redis_response_ms},
        "memory_usage_mb": memory_usage_mb,
        "disk_usage": disk_usage,
        "checked_at": datetime.now(UTC),
    }


@router.post("/query-explain")
async def query_explain(
    body: QueryExplainRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """对 SQL 执行 EXPLAIN ANALYZE，返回结果（admin）。仅允许 SELECT。"""
    _require_admin(actor)
    cleaned_sql = _validate_select_only(body.sql)
    # 二次防护：EXPLAIN 关键字前缀，确保只读执行计划
    explain_sql = f"EXPLAIN ANALYZE {cleaned_sql}"
    try:
        async with pool.connection() as conn:
            result = await conn.execute(explain_sql)
            rows = await result.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("query-explain execution failed: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=f"EXPLAIN ANALYZE failed: {exc}",
        ) from exc
    plan_lines = [row[list(row.keys())[0]] if row else "" for row in rows]
    return {
        "plan": "\n".join(str(line) for line in plan_lines),
        "rows": [str(row) for row in rows],
        "sql": cleaned_sql,
        "workspace_id": actor.workspace_id,
    }


__all__ = [
    "BenchmarkRequest",
    "CacheInvalidateRequest",
    "QueryExplainRequest",
    "SlowQueryMiddleware",
    "cached_with_stats",
    "register_middleware",
    "router",
]
