"""Tests for the resilience module.

覆盖：
- db_with_retry: 首次成功 / 重试后成功 / 全部失败 / 不重试 IntegrityError /
  重试 PoolTimeout / 重试 ConnectionError
- GracefulShutdown: begin/end 计数 / drain 空闲立即返回 / drain 超时 /
  shutting_down 拒绝新请求 / drain 等待请求完成后返回
- GET /api/v1/system/deepz: 成功 / DB 失败 503 / Redis 失败 503 /
  shutting_down 503 / Redis skip(200)
- make_redis_client: 单节点模式 / 哨兵模式(mock) / 哨兵失败回退

所有测试使用 fake pool/connection/redis，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import psycopg.errors
import pytest
from fastapi import FastAPI
from psycopg_pool import PoolTimeout

from workama_platform.modules import resilience


# ============================================================================
# 测试辅助：fake pool / connection / redis
# ============================================================================


class _OkConn:
    """模拟可用的 psycopg 连接，execute 直接返回 None。"""

    async def execute(self, query, params=()):
        return None


class _OkPool:
    """模拟可用的连接池：connection() 返回的上下文管理器 yield 一个 _OkConn。"""

    def connection(self):
        conn = _OkConn()

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        return _Ctx()


class _FailPool:
    """模拟获取连接即失败的连接池（DB 不可用）。"""

    def connection(self):
        class _Ctx:
            async def __aenter__(self):
                raise psycopg.errors.OperationalError("db down")

            async def __aexit__(self, *_args):
                return False

        return _Ctx()


class _OkRedis:
    async def ping(self):
        return True


class _FailRedis:
    async def ping(self):
        raise ConnectionError("redis down")


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(resilience.router)
    return app


# ============================================================================
# 1. db_with_retry
# ============================================================================


class TestDbWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_try_no_retry(self, monkeypatch):
        """首次成功，不重试。"""
        monkeypatch.setattr(resilience.random, "random", lambda: 0.0)
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            return "ok"

        result = await resilience.db_with_retry(factory, max_retries=3, base_delay=0)
        assert result == "ok"
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_succeeds_after_retries(self, monkeypatch):
        """前 2 次 OperationalError，第 3 次成功。"""
        monkeypatch.setattr(resilience.random, "random", lambda: 0.0)
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            if calls["n"] < 3:
                raise psycopg.errors.OperationalError("transient")
            return "ok"

        result = await resilience.db_with_retry(factory, max_retries=3, base_delay=0)
        assert result == "ok"
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_all_failures_raise_original(self, monkeypatch):
        """max_retries 后抛出原始异常。"""
        monkeypatch.setattr(resilience.random, "random", lambda: 0.0)
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            raise psycopg.errors.OperationalError("boom")

        with pytest.raises(psycopg.errors.OperationalError) as exc:
            await resilience.db_with_retry(factory, max_retries=3, base_delay=0)
        assert "boom" in str(exc.value)
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_does_not_retry_integrity_error(self):
        """IntegrityError 属于数据修改类错误，不重试，立即抛出。"""
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            raise psycopg.errors.IntegrityError("constraint violation")

        with pytest.raises(psycopg.errors.IntegrityError):
            await resilience.db_with_retry(factory, max_retries=3, base_delay=0)
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_does_not_retry_programming_error(self):
        """ProgrammingError 不重试，立即抛出。"""
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            raise psycopg.errors.ProgrammingError("bad sql")

        with pytest.raises(psycopg.errors.ProgrammingError):
            await resilience.db_with_retry(factory, max_retries=3, base_delay=0)
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_retries_on_pool_timeout(self, monkeypatch):
        """psycopg_pool.PoolTimeout 可重试。"""
        monkeypatch.setattr(resilience.random, "random", lambda: 0.0)
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            if calls["n"] < 2:
                raise PoolTimeout("pool timeout")
            return "ok"

        result = await resilience.db_with_retry(factory, max_retries=3, base_delay=0)
        assert result == "ok"
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_retries_on_connection_error(self, monkeypatch):
        """内置 ConnectionError 可重试。"""
        monkeypatch.setattr(resilience.random, "random", lambda: 0.0)
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            if calls["n"] < 2:
                raise ConnectionError("net down")
            return "ok"

        result = await resilience.db_with_retry(factory, max_retries=3, base_delay=0)
        assert result == "ok"
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_backoff_respects_max_delay(self, monkeypatch):
        """退避时间不超过 max_delay。"""
        sleeps: list[float] = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(resilience.asyncio, "sleep", fake_sleep)
        # random 返回较大值以验证 max_delay 截断。
        monkeypatch.setattr(resilience.random, "random", lambda: 0.5)
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            if calls["n"] < 3:
                raise psycopg.errors.OperationalError("transient")
            return "ok"

        await resilience.db_with_retry(
            factory, max_retries=3, base_delay=1.0, max_delay=0.5
        )
        # attempt 0: min(1.0*1 + 0.5, 0.5) = 0.5
        # attempt 1: min(1.0*2 + 0.5, 0.5) = 0.5
        assert sleeps == [0.5, 0.5]


# ============================================================================
# 2. GracefulShutdown
# ============================================================================


class TestGracefulShutdown:
    @pytest.mark.asyncio
    async def test_begin_end_request_counting(self):
        """begin/end 正确维护进行中请求计数。"""
        gs = resilience.GracefulShutdown()
        assert gs.active_requests == 0
        assert gs.is_shutting_down is False

        assert gs.begin_request() is True
        assert gs.active_requests == 1
        assert gs.begin_request() is True
        assert gs.active_requests == 2

        gs.end_request()
        assert gs.active_requests == 1
        gs.end_request()
        assert gs.active_requests == 0
        # 不会变为负数。
        gs.end_request()
        assert gs.active_requests == 0

    @pytest.mark.asyncio
    async def test_drain_returns_immediately_when_idle(self):
        """0 请求时 drain 立即返回 True。"""
        gs = resilience.GracefulShutdown()
        result = await gs.drain(timeout=30.0)
        assert result is True
        assert gs.is_shutting_down is True

    @pytest.mark.asyncio
    async def test_drain_timeout_with_active_requests(self):
        """有进行中请求时 drain 超时返回 False。"""
        gs = resilience.GracefulShutdown()
        assert gs.begin_request() is True
        # 不调用 end_request，模拟请求未完成。
        result = await gs.drain(timeout=0.05)
        assert result is False
        assert gs.is_shutting_down is True
        assert gs.active_requests == 1

    @pytest.mark.asyncio
    async def test_drain_waits_then_returns_when_requests_complete(self):
        """drain 等待进行中请求完成后返回 True。"""
        import asyncio

        gs = resilience.GracefulShutdown()
        assert gs.begin_request() is True

        async def _finish_later():
            await asyncio.sleep(0.02)
            gs.end_request()

        asyncio.create_task(_finish_later())
        result = await gs.drain(timeout=1.0)
        assert result is True
        assert gs.active_requests == 0
        assert gs.is_shutting_down is True

    @pytest.mark.asyncio
    async def test_shutting_down_rejects_new_requests(self):
        """进入关闭状态后 begin_request 返回 False。"""
        gs = resilience.GracefulShutdown()
        assert gs.begin_request() is True
        await gs.drain(timeout=0.0)
        # 关闭中，拒绝新请求。
        assert gs.begin_request() is False
        assert gs.is_shutting_down is True


# ============================================================================
# 3. GET /api/v1/system/deepz
# ============================================================================


class TestDeepz:
    @pytest.mark.asyncio
    async def test_deepz_success_db_redis_ok(self, monkeypatch):
        """DB + Redis 均可用且未关闭时返回 200。"""
        monkeypatch.setattr(resilience, "pool", _OkPool())
        monkeypatch.setattr(resilience, "redis", _OkRedis())
        monkeypatch.setattr(resilience, "shutdown", resilience.GracefulShutdown())

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/system/deepz")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["checks"]["db"] == "ok"
        assert body["checks"]["redis"] == "ok"
        assert body["checks"]["shutdown"] == "idle"
        assert resp.headers["cache-control"] == "no-cache"

    @pytest.mark.asyncio
    async def test_deepz_db_failure_returns_503(self, monkeypatch):
        """DB 不可用时返回 503。"""
        monkeypatch.setattr(resilience, "pool", _FailPool())
        monkeypatch.setattr(resilience, "redis", _OkRedis())
        monkeypatch.setattr(resilience, "shutdown", resilience.GracefulShutdown())

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/system/deepz")

        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unready"
        assert body["checks"]["db"] == "fail"
        assert body["checks"]["redis"] == "ok"

    @pytest.mark.asyncio
    async def test_deepz_redis_failure_returns_503(self, monkeypatch):
        """Redis 不可用时返回 503。"""
        monkeypatch.setattr(resilience, "pool", _OkPool())
        monkeypatch.setattr(resilience, "redis", _FailRedis())
        monkeypatch.setattr(resilience, "shutdown", resilience.GracefulShutdown())

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/system/deepz")

        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unready"
        assert body["checks"]["db"] == "ok"
        assert body["checks"]["redis"] == "fail"

    @pytest.mark.asyncio
    async def test_deepz_shutting_down_returns_503(self, monkeypatch):
        """正在关闭时返回 503（即使 DB/Redis 可用）。"""
        monkeypatch.setattr(resilience, "pool", _OkPool())
        monkeypatch.setattr(resilience, "redis", _OkRedis())
        gs = resilience.GracefulShutdown()
        await gs.drain(timeout=0.0)  # 进入关闭状态
        monkeypatch.setattr(resilience, "shutdown", gs)

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/system/deepz")

        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unready"
        assert body["checks"]["shutdown"] == "active"
        # DB/Redis 仍然执行检查。
        assert body["checks"]["db"] == "ok"
        assert body["checks"]["redis"] == "ok"

    @pytest.mark.asyncio
    async def test_deepz_redis_skip_when_none(self, monkeypatch):
        """redis 为 None 时返回 skip，且不影响就绪（DB ok 时 200）。"""
        monkeypatch.setattr(resilience, "pool", _OkPool())
        monkeypatch.setattr(resilience, "redis", None)
        monkeypatch.setattr(resilience, "shutdown", resilience.GracefulShutdown())

        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/system/deepz")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["checks"]["redis"] == "skip"
        assert body["checks"]["db"] == "ok"


# ============================================================================
# 4. make_redis_client
# ============================================================================


class TestMakeRedisClient:
    def test_single_node_mode(self, monkeypatch):
        """未配置哨兵时走 Redis.from_url 单节点模式。"""
        captured: dict = {}

        def fake_from_url(url, decode_responses=False, **kw):
            captured["url"] = url
            captured["decode_responses"] = decode_responses
            return "single-node-client"

        monkeypatch.setattr(resilience.Redis, "from_url", fake_from_url)
        settings = SimpleNamespace(
            redis_url="redis://example:6379/0",
            redis_sentinels="",
            redis_master_name="",
        )

        client = resilience.make_redis_client(settings)
        assert client == "single-node-client"
        assert captured["url"] == "redis://example:6379/0"
        assert captured["decode_responses"] is True

    def test_sentinel_mode(self, monkeypatch):
        """配置哨兵时走 Sentinel.master_for 模式。"""
        captured: dict = {}

        class _FakeSentinel:
            def __init__(self, sentinels, **kw):
                captured["sentinels"] = sentinels
                captured["kw"] = kw

            def master_for(self, name, **kw):
                captured["master"] = name
                captured["master_kw"] = kw
                return "sentinel-master-client"

        monkeypatch.setattr(resilience, "Sentinel", _FakeSentinel)
        settings = SimpleNamespace(
            redis_url="redis://example:6379/0",
            redis_sentinels="sent1:26379, sent2:26379",
            redis_master_name="mymaster",
        )

        client = resilience.make_redis_client(settings)
        assert client == "sentinel-master-client"
        assert captured["sentinels"] == [("sent1", 26379), ("sent2", 26379)]
        assert captured["master"] == "mymaster"
        assert captured["master_kw"]["decode_responses"] is True

    def test_sentinel_init_failure_falls_back_to_single_node(self, monkeypatch):
        """哨兵初始化异常时回退到单节点模式（best-effort）。"""
        class _BoomSentinel:
            def __init__(self, *a, **kw):
                raise RuntimeError("sentinel init failed")

        monkeypatch.setattr(resilience, "Sentinel", _BoomSentinel)
        monkeypatch.setattr(
            resilience.Redis,
            "from_url",
            lambda url, decode_responses=False, **kw: "fallback-client",
        )
        settings = SimpleNamespace(
            redis_url="redis://example:6379/0",
            redis_sentinels="s1:26379",
            redis_master_name="mymaster",
        )

        assert resilience.make_redis_client(settings) == "fallback-client"

    def test_sentinels_without_master_name_falls_back_to_single_node(self, monkeypatch):
        """仅配置哨兵但未配置 master_name 时回退单节点。"""
        monkeypatch.setattr(
            resilience.Redis,
            "from_url",
            lambda url, decode_responses=False, **kw: "single-node-client",
        )
        settings = SimpleNamespace(
            redis_url="redis://example:6379/0",
            redis_sentinels="s1:26379",
            redis_master_name="",
        )

        assert resilience.make_redis_client(settings) == "single-node-client"


# ============================================================================
# 5. _parse_sentinels 辅助
# ============================================================================


class TestParseSentinels:
    def test_parse_multiple_hosts(self):
        assert resilience._parse_sentinels("h1:26379,h2:26379") == [
            ("h1", 26379),
            ("h2", 26379),
        ]

    def test_parse_host_only_defaults_port(self):
        assert resilience._parse_sentinels("h1") == [("h1", 26379)]

    def test_parse_empty_returns_empty_list(self):
        assert resilience._parse_sentinels("") == []
        assert resilience._parse_sentinels(" , ") == []

    def test_parse_invalid_port_skipped(self):
        assert resilience._parse_sentinels("h1:abc,h2:26379") == [("h2", 26379)]
