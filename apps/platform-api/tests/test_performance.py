"""P2 性能优化专项模块 (performance) 单元测试。

覆盖范围：
- 8 个端点的成功响应与字段类型
- 鉴权：401 未登录 / 403 普通用户访问 admin 端点
- workspace 隔离（slow-queries / cache/invalidate / benchmarks）
- 慢查询日志记录（SlowQueryMiddleware：慢请求记录 / 快请求跳过 / DB 失败静默 / 非 http 跳过）
- 缓存命中统计装饰器 cached_with_stats（hit / miss / 静默降级 / 动态 workspace_id）
- query-explain 拒绝 DDL/DML（INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/TRUNCATE）+ 注释剥离 + CTE 放行
- benchmark 异步执行（mock _run_benchmark_async）
- 健康检查各字段返回正确类型
- Pydantic 模型字段验证
- 表结构存在性检查（真实 DB to_regclass）
- 路由注册数量

所有 fake-pool 测试不依赖真实 DB / Redis / 网络；表结构测试使用真实 pool。
"""
from __future__ import annotations

import asyncio
import datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from workama_platform.core import Actor, get_actor, settings
from workama_platform.modules import performance as perf


# ============================================================================
# 测试辅助：fake pool / connection / result（参考 test_push_notification.py）
# ============================================================================


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = list(rows) if rows is not None else []
        self.rowcount = len(self._rows)

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _RecordingConnection:
    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
        self._idx = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None


class _Pool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        connection = self._connection

        class _Context:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_args):
                return False

        return _Context()


def _actor(
    *,
    role="owner",
    workspace_id="wsp_test",
    user_id="usr_test",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=("*",) if role in {"owner", "admin"} else ("session:read",),
    )


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(perf.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


def _http_scope(path: str, method: str = "GET", headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": b"",
    }


# ============================================================================
# 表结构存在性检查（真实 DB）
# ============================================================================


async def _real_conn() -> AsyncConnection:
    """创建一条直连真实 DB 的异步连接（避免连接池跨事件循环绑定问题）。"""
    conn = await AsyncConnection.connect(settings.database_url)
    conn.row_factory = dict_row
    return conn


@pytest.mark.asyncio
async def test_tables_exist_in_db():
    """三张性能优化表在真实 DB 中存在。"""
    conn = await _real_conn()
    try:
        result = await conn.execute(
            """
            SELECT to_regclass('public.ops_slow_query_log') AS t1,
                   to_regclass('public.ops_cache_stats') AS t2,
                   to_regclass('public.ops_performance_benchmark') AS t3
            """
        )
        row = await result.fetchone()
    finally:
        await conn.close()
    assert row is not None
    assert row["t1"] == "ops_slow_query_log"
    assert row["t2"] == "ops_cache_stats"
    assert row["t3"] == "ops_performance_benchmark"


@pytest.mark.asyncio
async def test_cache_stats_unique_constraint_exists():
    """ops_cache_stats 的 UNIQUE(workspace_id, resource) 约束存在。"""
    conn = await _real_conn()
    try:
        result = await conn.execute(
            """
            SELECT COUNT(*) AS n FROM pg_constraint
            WHERE conrelid = 'public.ops_cache_stats'::regclass
              AND contype = 'u'
            """
        )
        row = await result.fetchone()
    finally:
        await conn.close()
    assert row is not None
    assert int(row["n"]) >= 1


# ============================================================================
# 路由注册
# ============================================================================


def test_router_has_eight_routes():
    """performance.router 注册了 8 个端点。"""
    paths = {(r.path, tuple(sorted(r.methods or ()))) for r in perf.router.routes}
    expected = {
        ("/api/v1/performance/metrics", ("GET",)),
        ("/api/v1/performance/slow-queries", ("GET",)),
        ("/api/v1/performance/cache/invalidate", ("POST",)),
        ("/api/v1/performance/cache/stats", ("GET",)),
        ("/api/v1/performance/benchmark", ("POST",)),
        ("/api/v1/performance/benchmarks/{operation_id}", ("GET",)),
        ("/api/v1/performance/health-check", ("GET",)),
        ("/api/v1/performance/query-explain", ("POST",)),
    }
    assert expected <= paths
    assert len(perf.router.routes) == 8


def test_register_middleware_adds_middleware():
    """register_middleware 在 app 上注册 SlowQueryMiddleware。"""
    app = FastAPI()
    before = len(app.user_middleware)
    perf.register_middleware(app, threshold_ms=150)
    assert len(app.user_middleware) == before + 1


# ============================================================================
# 1. GET /metrics
# ============================================================================


@pytest.mark.asyncio
async def test_get_metrics_admin_success(monkeypatch):
    """owner 调用 /metrics 返回关键指标字段。"""
    perf.reset_metrics_for_tests()
    conn = _RecordingConnection()
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    result = await perf.get_metrics(_actor(role="admin"))

    assert "uptime_seconds" in result
    assert isinstance(result["uptime_seconds"], float)
    assert result["request_count"] == 0
    assert isinstance(result["avg_response_time_ms"], float)
    assert "active_connections" in result
    assert "generated_at" in result


@pytest.mark.asyncio
async def test_get_metrics_rejects_member():
    """member 调用 /metrics 返回 403。"""
    with pytest.raises(HTTPException) as exc:
        await perf.get_metrics(_actor(role="member"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_metrics_unauthenticated_returns_401():
    """未登录访问 /metrics 返回 401（HTTP 层）。"""
    app = _app()  # 不 override get_actor
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/performance/metrics")
    assert resp.status_code == 401


# ============================================================================
# 2. GET /slow-queries
# ============================================================================


@pytest.mark.asyncio
async def test_list_slow_queries_returns_items(monkeypatch):
    """admin 查询当前 workspace 慢查询列表。"""
    rows = [
        {"id": 1, "workspace_id": "wsp_test", "query_text": "GET /a",
         "duration_ms": 250, "created_at": datetime.datetime.now(datetime.UTC),
         "user_agent": "ua", "path": "/a"},
    ]
    conn = _RecordingConnection(results=[_Result(rows=rows)])
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    result = await perf.list_slow_queries(_actor(role="admin"))

    assert result["count"] == 1
    assert result["workspace_id"] == "wsp_test"
    assert result["items"][0]["duration_ms"] == 250
    select_q = conn.calls[0][0]
    assert "workspace_id = %s" in select_q
    assert "ORDER BY created_at DESC" in select_q


@pytest.mark.asyncio
async def test_list_slow_queries_filters_by_since(monkeypatch):
    """传入 since 时 SQL 带 created_at >= %s 过滤。"""
    conn = _RecordingConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    since = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    await perf.list_slow_queries(_actor(role="admin"), since=since, limit=10)

    select_q, params = conn.calls[0]
    assert "created_at >= %s" in select_q
    assert params[1] == since
    assert params[2] == 10


@pytest.mark.asyncio
async def test_list_slow_queries_workspace_isolation(monkeypatch):
    """SQL 仅查询调用者所在 workspace（参数化隔离）。"""
    conn = _RecordingConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    await perf.list_slow_queries(_actor(role="admin", workspace_id="wsp_mine"))

    _, params = conn.calls[0]
    assert params[0] == "wsp_mine"


@pytest.mark.asyncio
async def test_list_slow_queries_rejects_member():
    """member 调用慢查询列表返回 403。"""
    with pytest.raises(HTTPException) as exc:
        await perf.list_slow_queries(_actor(role="member"))
    assert exc.value.status_code == 403


# ============================================================================
# 3. POST /cache/invalidate
# ============================================================================


@pytest.mark.asyncio
async def test_invalidate_cache_success(monkeypatch):
    """admin 失效本 workspace 缓存，调用 cache_delete_pattern。"""
    invalidated: list[str] = []

    async def fake_delete_pattern(pattern: str) -> None:
        invalidated.append(pattern)

    monkeypatch.setattr(perf, "cache_delete_pattern", fake_delete_pattern)

    body = perf.CacheInvalidateRequest(workspace_id="wsp_test", resource="assistant:list")
    result = await perf.invalidate_cache(body, _actor(role="admin"))

    assert result["invalidated"] is True
    assert result["workspace_id"] == "wsp_test"
    assert invalidated == ["workama:cache:wsp_test:assistant:list:*"]


@pytest.mark.asyncio
async def test_invalidate_cache_rejects_cross_workspace(monkeypatch):
    """失效其他 workspace 的缓存返回 403。"""
    body = perf.CacheInvalidateRequest(workspace_id="wsp_other", resource="x")
    with pytest.raises(HTTPException) as exc:
        await perf.invalidate_cache(body, _actor(role="admin", workspace_id="wsp_test"))
    assert exc.value.status_code == 403
    assert "another workspace" in exc.value.detail


@pytest.mark.asyncio
async def test_invalidate_cache_rejects_member(monkeypatch):
    """member 失效缓存返回 403。"""
    body = perf.CacheInvalidateRequest(workspace_id="wsp_test", resource="x")
    with pytest.raises(HTTPException) as exc:
        await perf.invalidate_cache(body, _actor(role="member"))
    assert exc.value.status_code == 403


# ============================================================================
# 4. GET /cache/stats
# ============================================================================


class _FakeRedis:
    """模拟 redis 客户端：keys() 返回预设结果或抛异常。"""

    def __init__(self, keys_result=None, raise_on_keys=False):
        self._keys_result = keys_result if keys_result is not None else []
        self._raise = raise_on_keys

    async def keys(self, pattern: str):
        if self._raise:
            raise ConnectionError("redis down")
        return list(self._keys_result)


@pytest.mark.asyncio
async def test_get_cache_stats_aggregates(monkeypatch):
    """cache/stats 聚合 hits/misses/keys 并计算 hit_rate。"""
    rows = [
        {"resource": "assistant:list", "hits": 8, "misses": 2,
         "last_hit_at": None, "last_miss_at": None},
        {"resource": "memory:recall", "hits": 3, "misses": 7,
         "last_hit_at": None, "last_miss_at": None},
    ]
    conn = _RecordingConnection(results=[_Result(rows=rows)])

    monkeypatch.setattr(perf, "pool", _Pool(conn))
    monkeypatch.setattr(perf, "redis", _FakeRedis(keys_result=["k1", "k2", "k3"]))

    result = await perf.get_cache_stats(_actor(role="admin"))

    assert result["hits"] == 11
    assert result["misses"] == 9
    assert result["keys"] == 2
    assert result["size"] == 3
    assert result["hit_rate"] == round(11 / 20, 4)


@pytest.mark.asyncio
async def test_get_cache_stats_empty(monkeypatch):
    """无缓存统计时返回 0 命中。"""
    conn = _RecordingConnection(results=[_Result(rows=[])])

    monkeypatch.setattr(perf, "pool", _Pool(conn))
    monkeypatch.setattr(perf, "redis", _FakeRedis(keys_result=[]))

    result = await perf.get_cache_stats(_actor(role="admin"))

    assert result["hits"] == 0
    assert result["misses"] == 0
    assert result["hit_rate"] == 0.0
    assert result["size"] == 0


@pytest.mark.asyncio
async def test_get_cache_stats_redis_failure_silent(monkeypatch):
    """Redis keys 失败时 size=None，不抛异常。"""
    conn = _RecordingConnection(results=[_Result(rows=[])])

    monkeypatch.setattr(perf, "pool", _Pool(conn))
    monkeypatch.setattr(perf, "redis", _FakeRedis(raise_on_keys=True))

    result = await perf.get_cache_stats(_actor(role="admin"))
    assert result["size"] is None


@pytest.mark.asyncio
async def test_get_cache_stats_rejects_member():
    """member 调用 cache/stats 返回 403。"""
    with pytest.raises(HTTPException) as exc:
        await perf.get_cache_stats(_actor(role="member"))
    assert exc.value.status_code == 403


# ============================================================================
# 5. POST /benchmark + GET /benchmarks/{operation_id}
# ============================================================================


@pytest.mark.asyncio
async def test_create_benchmark_returns_operation_id(monkeypatch):
    """触发 benchmark 返回 operation_id 并写入 running 记录。"""
    conn = _RecordingConnection(results=[_Result()])
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    called: list[tuple] = []

    async def fake_run(operation_id, workspace_id, scenarios):
        called.append((operation_id, workspace_id, scenarios))

    monkeypatch.setattr(perf, "_run_benchmark_async", fake_run)

    body = perf.BenchmarkRequest(scenarios=[{"name": "s1"}])
    result = await perf.create_benchmark(body, _actor(role="admin"))

    assert result["status"] == "running"
    assert result["operation_id"].startswith("op_")
    assert result["benchmark_id"].startswith("perf_")
    assert called and called[0][1] == "wsp_test"
    insert_q = next(q for q, _ in conn.calls if "INSERT INTO ops_performance_benchmark" in q)
    assert "'running'" in insert_q


@pytest.mark.asyncio
async def test_create_benchmark_rejects_member(monkeypatch):
    """member 触发 benchmark 返回 403。"""
    body = perf.BenchmarkRequest()
    with pytest.raises(HTTPException) as exc:
        await perf.create_benchmark(body, _actor(role="member"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_benchmark_returns_result(monkeypatch):
    """查询已完成的 benchmark 返回结果。"""
    row = {
        "id": "perf_1", "workspace_id": "wsp_test", "operation_id": "op_1",
        "status": "completed", "scenarios": [], "result": {"scenarios_run": 1},
        "created_at": datetime.datetime.now(datetime.UTC), "completed_at": None,
    }
    conn = _RecordingConnection(results=[_Result(row=row)])
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    result = await perf.get_benchmark("op_1", _actor(role="owner"))

    assert result["status"] == "completed"
    assert result["operation_id"] == "op_1"
    select_q, params = conn.calls[0]
    assert "operation_id = %s" in select_q
    assert "workspace_id = %s" in select_q
    assert params == ("op_1", "wsp_test")


@pytest.mark.asyncio
async def test_get_benchmark_404_when_missing(monkeypatch):
    """查询不存在的 benchmark 返回 404。"""
    conn = _RecordingConnection(results=[_Result(row=None)])
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await perf.get_benchmark("op_missing", _actor(role="owner"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_benchmark_workspace_isolation_returns_404(monkeypatch):
    """跨 workspace 查询他人 benchmark（workspace 过滤）返回 404。"""
    conn = _RecordingConnection(results=[_Result(row=None)])
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await perf.get_benchmark("op_other", _actor(role="owner", workspace_id="wsp_mine"))
    assert exc.value.status_code == 404
    _, params = conn.calls[0]
    assert params[1] == "wsp_mine"


@pytest.mark.asyncio
async def test_get_benchmark_rejects_member(monkeypatch):
    """member 查询 benchmark 返回 403。"""
    with pytest.raises(HTTPException) as exc:
        await perf.get_benchmark("op_1", _actor(role="member"))
    assert exc.value.status_code == 403


# ============================================================================
# 6. GET /health-check
# ============================================================================


@pytest.mark.asyncio
async def test_health_check_returns_fields_and_types(monkeypatch):
    """health-check 返回 db/redis/memory_usage_mb/disk_usage 字段且类型正确。"""
    conn = _RecordingConnection()

    class _R:
        async def ping(self):
            return True

    monkeypatch.setattr(perf, "pool", _Pool(conn))
    monkeypatch.setattr(perf, "redis", _R())

    result = await perf.health_check(_actor(role="admin"))

    assert result["status"] == "healthy"
    assert isinstance(result["db"]["status"], str)
    assert result["db"]["status"] == "ok"
    assert isinstance(result["db"]["response_ms"], float)
    assert result["redis"]["status"] == "ok"
    assert isinstance(result["redis"]["response_ms"], float)
    # memory_usage_mb 可为 float 或 None（非 POSIX），这里容器为 Linux 应为 float
    assert result["memory_usage_mb"] is None or isinstance(result["memory_usage_mb"], float)
    assert isinstance(result["disk_usage"], dict)
    assert "used_percent" in result["disk_usage"]
    assert isinstance(result["checked_at"], datetime.datetime)


@pytest.mark.asyncio
async def test_health_check_db_failure_marks_degraded(monkeypatch):
    """DB ping 失败时整体 status=degraded，db.status=error。"""
    class _FailPool:
        def connection(self):
            class _Ctx:
                async def __aenter__(self):
                    raise ConnectionError("db down")

                async def __aexit__(self, *_args):
                    return False

            return _Ctx()

    class _R:
        async def ping(self):
            return True

    monkeypatch.setattr(perf, "pool", _FailPool())
    monkeypatch.setattr(perf, "redis", _R())

    result = await perf.health_check(_actor(role="admin"))
    assert result["status"] == "degraded"
    assert result["db"]["status"] == "error"
    assert result["db"]["response_ms"] is None
    assert result["redis"]["status"] == "ok"


@pytest.mark.asyncio
async def test_health_check_rejects_member():
    """member 调用 health-check 返回 403。"""
    with pytest.raises(HTTPException) as exc:
        await perf.health_check(_actor(role="member"))
    assert exc.value.status_code == 403


# ============================================================================
# 7. POST /query-explain（SQL 白名单）
# ============================================================================


@pytest.mark.asyncio
async def test_query_explain_select_success(monkeypatch):
    """合法 SELECT 返回 EXPLAIN ANALYZE 结果。"""
    rows = [{"QUERY PLAN": "Seq Scan on t (cost=0.00..1.00)"}, {"QUERY PLAN": "Planning Time: 0.1 ms"}]
    conn = _RecordingConnection(results=[_Result(rows=rows)])
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    body = perf.QueryExplainRequest(sql="SELECT 1")
    result = await perf.query_explain(body, _actor(role="admin"))

    assert "Seq Scan" in result["plan"]
    assert result["sql"] == "SELECT 1"
    exec_q = conn.calls[0][0]
    assert exec_q.startswith("EXPLAIN ANALYZE SELECT")


@pytest.mark.asyncio
async def test_query_explain_strips_comments(monkeypatch):
    """注释被剥离后再做白名单校验。"""
    conn = _RecordingConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    body = perf.QueryExplainRequest(sql="-- comment\n/* block */ SELECT 1")
    result = await perf.query_explain(body, _actor(role="admin"))

    assert result["sql"] == "SELECT 1"
    assert conn.calls[0][0].startswith("EXPLAIN ANALYZE SELECT")


@pytest.mark.asyncio
async def test_query_explain_allows_with_cte(monkeypatch):
    """WITH ... SELECT (CTE) 被允许。"""
    conn = _RecordingConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    body = perf.QueryExplainRequest(sql="WITH t AS (SELECT 1) SELECT * FROM t")
    result = await perf.query_explain(body, _actor(role="admin"))
    assert result["sql"].startswith("WITH t AS")


@pytest.mark.asyncio
async def test_query_explain_rejects_insert():
    body = perf.QueryExplainRequest(sql="INSERT INTO t VALUES (1)")
    with pytest.raises(HTTPException) as exc:
        await perf.query_explain(body, _actor(role="admin"))
    assert exc.value.status_code == 400
    assert "INSERT" in exc.value.detail or "Only SELECT" in exc.value.detail


@pytest.mark.asyncio
async def test_query_explain_rejects_update():
    body = perf.QueryExplainRequest(sql="UPDATE t SET a=1")
    with pytest.raises(HTTPException) as exc:
        await perf.query_explain(body, _actor(role="admin"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_query_explain_rejects_delete():
    body = perf.QueryExplainRequest(sql="DELETE FROM t")
    with pytest.raises(HTTPException) as exc:
        await perf.query_explain(body, _actor(role="admin"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_query_explain_rejects_drop():
    body = perf.QueryExplainRequest(sql="DROP TABLE t")
    with pytest.raises(HTTPException) as exc:
        await perf.query_explain(body, _actor(role="admin"))
    assert exc.value.status_code == 400
    assert "DROP" in exc.value.detail or "Only SELECT" in exc.value.detail


@pytest.mark.asyncio
async def test_query_explain_rejects_create():
    body = perf.QueryExplainRequest(sql="CREATE TABLE t (id int)")
    with pytest.raises(HTTPException) as exc:
        await perf.query_explain(body, _actor(role="admin"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_query_explain_rejects_alter():
    body = perf.QueryExplainRequest(sql="ALTER TABLE t ADD COLUMN x int")
    with pytest.raises(HTTPException) as exc:
        await perf.query_explain(body, _actor(role="admin"))
    assert exc.value.status_code == 400
    assert "ALTER" in exc.value.detail or "Only SELECT" in exc.value.detail


@pytest.mark.asyncio
async def test_query_explain_rejects_truncate():
    body = perf.QueryExplainRequest(sql="TRUNCATE TABLE t")
    with pytest.raises(HTTPException) as exc:
        await perf.query_explain(body, _actor(role="admin"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_query_explain_rejects_non_select_first_token():
    """EXPLAIN / SHOW 等非 SELECT 首关键字被拒绝。"""
    body = perf.QueryExplainRequest(sql="EXPLAIN SELECT 1")
    with pytest.raises(HTTPException) as exc:
        await perf.query_explain(body, _actor(role="admin"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_query_explain_rejects_member():
    """member 调用 query-explain 返回 403。"""
    body = perf.QueryExplainRequest(sql="SELECT 1")
    with pytest.raises(HTTPException) as exc:
        await perf.query_explain(body, _actor(role="member"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_query_explain_execution_error_returns_400(monkeypatch):
    """EXPLAIN 执行抛异常时返回 400。"""
    class _ErrConn:
        async def execute(self, query, params=()):
            raise RuntimeError("syntax error")

    monkeypatch.setattr(perf, "pool", _Pool(_ErrConn()))
    body = perf.QueryExplainRequest(sql="SELECT * FROM nonexistent_table_xyz")
    with pytest.raises(HTTPException) as exc:
        await perf.query_explain(body, _actor(role="admin"))
    assert exc.value.status_code == 400


# ============================================================================
# 8. SlowQueryMiddleware
# ============================================================================


@pytest.mark.asyncio
async def test_middleware_logs_slow_query(monkeypatch):
    """超过阈值的请求被记录到 ops_slow_query_log。"""
    logged: list[tuple] = []

    async def fake_log(workspace_id, query_text, duration_ms, path, user_agent):
        logged.append((workspace_id, query_text, duration_ms, path, user_agent))

    async def slow_app(scope, receive, send):
        await asyncio.sleep(0.05)  # 50ms
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(perf, "_log_slow_query", fake_log)
    perf.reset_metrics_for_tests()

    mw = perf.SlowQueryMiddleware(slow_app, threshold_ms=20)
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    await mw(_http_scope("/slow", method="GET"), lambda: None, send)

    assert logged
    assert logged[0][2] >= 20  # duration_ms
    assert logged[0][3] == "/slow"
    assert logged[0][1] == "GET /slow"


@pytest.mark.asyncio
async def test_middleware_skips_fast_query(monkeypatch):
    """快请求不记录慢查询日志。"""
    logged: list[tuple] = []

    async def fake_log(*args, **kwargs):
        logged.append(args)

    async def fast_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(perf, "_log_slow_query", fake_log)

    mw = perf.SlowQueryMiddleware(fast_app, threshold_ms=1000)

    async def send(msg):
        return None

    await mw(_http_scope("/fast"), lambda: None, send)
    assert logged == []


@pytest.mark.asyncio
async def test_middleware_silent_on_db_failure(monkeypatch):
    """_log_slow_query 抛异常时不影响请求响应。"""
    async def failing_log(*args, **kwargs):
        raise RuntimeError("db down")

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    monkeypatch.setattr(perf, "_log_slow_query", failing_log)
    mw = perf.SlowQueryMiddleware(app, threshold_ms=0)  # 必触发日志
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    await mw(_http_scope("/x"), lambda: None, send)
    # 响应仍正常发送
    assert any(m.get("type") == "http.response.start" for m in sent)


@pytest.mark.asyncio
async def test_middleware_skips_non_http_scope():
    """非 http scope（如 lifespan）直接透传。"""
    called = {"app": False}

    async def app(scope, receive, send):
        called["app"] = True

    mw = perf.SlowQueryMiddleware(app, threshold_ms=0)
    await mw({"type": "lifespan"}, lambda: None, lambda msg: None)
    assert called["app"] is True


@pytest.mark.asyncio
async def test_record_request_metrics_updates_counters():
    """_record_request_metrics 累计计数与响应时间。"""
    perf.reset_metrics_for_tests()
    perf._record_request_metrics(10.0)
    perf._record_request_metrics(30.0)
    assert perf._request_count == 2
    assert perf._total_response_time_ms == 40.0


# ============================================================================
# 9. cached_with_stats 装饰器
# ============================================================================


@pytest.mark.asyncio
async def test_cached_with_stats_miss_then_hit(monkeypatch):
    """首次 miss 执行函数并缓存，第二次 hit 直接返回缓存。"""
    state = {"calls": 0, "cache": {}}
    hit_calls: list[tuple] = []
    miss_calls: list[tuple] = []

    async def fake_cache_get(key):
        return state["cache"].get(key)

    async def fake_cache_set(key, value, ttl):
        state["cache"][key] = value

    async def fake_hit(ws, resource):
        hit_calls.append((ws, resource))

    async def fake_miss(ws, resource):
        miss_calls.append((ws, resource))

    monkeypatch.setattr(perf, "cache_get", fake_cache_get)
    monkeypatch.setattr(perf, "cache_set", fake_cache_set)
    monkeypatch.setattr(perf, "_record_cache_hit", fake_hit)
    monkeypatch.setattr(perf, "_record_cache_miss", fake_miss)

    @perf.cached_with_stats(workspace_id="wsp_1", resource="r", ttl=30)
    async def fn(x):
        state["calls"] += 1
        return f"v{x}"

    r1 = await fn(1)
    r2 = await fn(1)

    assert r1 == "v1"
    assert r2 == "v1"
    assert state["calls"] == 1  # 函数只执行一次
    assert len(miss_calls) == 1
    assert len(hit_calls) == 1
    assert hit_calls[0] == ("wsp_1", "r")


@pytest.mark.asyncio
async def test_cached_with_stats_dynamic_workspace_id(monkeypatch):
    """workspace_id 传入可调用对象时按参数动态解析。"""
    monkeypatch.setattr(perf, "cache_get", lambda *a, **k: _none())
    monkeypatch.setattr(
        perf, "cache_set", lambda *a, **k: _noop()
    )
    monkeypatch.setattr(perf, "_record_cache_hit", lambda *a, **k: _noop())
    miss_ws: list[str] = []

    async def fake_miss(ws, resource):
        miss_ws.append(ws)

    monkeypatch.setattr(perf, "_record_cache_miss", fake_miss)

    @perf.cached_with_stats(
        workspace_id=lambda *a, **kw: kw["actor"].workspace_id,
        resource="dynamic",
        ttl=10,
    )
    async def fn(actor):
        return "ok"

    await fn(actor=_actor(workspace_id="wsp_dyn"))
    assert miss_ws == ["wsp_dyn"]


@pytest.mark.asyncio
async def test_cached_with_stats_silent_on_db_failure(monkeypatch):
    """_record_cache_miss 抛异常时不影响主流程返回值。"""
    async def failing_miss(ws, resource):
        raise RuntimeError("db down")

    async def failing_hit(ws, resource):
        raise RuntimeError("db down")

    monkeypatch.setattr(perf, "cache_get", lambda *a, **k: _none())
    monkeypatch.setattr(perf, "cache_set", lambda *a, **k: _noop())
    monkeypatch.setattr(perf, "_record_cache_hit", failing_hit)
    monkeypatch.setattr(perf, "_record_cache_miss", failing_miss)

    @perf.cached_with_stats(workspace_id="wsp_1", resource="r")
    async def fn():
        return "value"

    result = await fn()
    assert result == "value"


@pytest.mark.asyncio
async def test_record_cache_hit_silent_on_db_error(monkeypatch):
    """_record_cache_hit 内部 DB 异常静默降级（不抛出）。"""
    class _FailPool:
        def connection(self):
            class _Ctx:
                async def __aenter__(self):
                    raise ConnectionError("db down")

                async def __aexit__(self, *_args):
                    return False

            return _Ctx()

    monkeypatch.setattr(perf, "pool", _FailPool())
    # 不应抛异常
    await perf._record_cache_hit("wsp_1", "r")


@pytest.mark.asyncio
async def test_record_cache_miss_writes_upsert(monkeypatch):
    """_record_cache_miss 执行 ON CONFLICT upsert。"""
    conn = _RecordingConnection()
    monkeypatch.setattr(perf, "pool", _Pool(conn))

    await perf._record_cache_miss("wsp_1", "res")

    q, params = conn.calls[0]
    assert "INSERT INTO ops_cache_stats" in q
    assert "ON CONFLICT" in q
    assert "misses = ops_cache_stats.misses + 1" in q
    assert params == ("wsp_1", "res")


# ============================================================================
# 10. Pydantic 模型字段验证
# ============================================================================


def test_cache_invalidate_request_rejects_empty_fields():
    """workspace_id / resource 不能为空。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        perf.CacheInvalidateRequest(workspace_id="", resource="x")
    with pytest.raises(ValidationError):
        perf.CacheInvalidateRequest(workspace_id="w", resource="")


def test_query_explain_request_rejects_empty_sql():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        perf.QueryExplainRequest(sql="")


def test_benchmark_request_defaults():
    """BenchmarkRequest 默认 scenarios/metadata 为空。"""
    body = perf.BenchmarkRequest()
    assert body.scenarios == []
    assert body.metadata == {}


def test_validate_select_only_helper_returns_cleaned():
    """_validate_select_only 直接返回剥离注释后的 SQL。"""
    cleaned = perf._validate_select_only("  -- c\nSELECT 1")
    assert cleaned == "SELECT 1"


# ============================================================================
# 辅助协程
# ============================================================================


async def _none():
    return None


async def _noop():
    return None
