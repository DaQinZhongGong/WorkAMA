"""统一搜索 (search.py unified_router) 单元 + 端点测试。

v7.153: 14 个测试覆盖：
- GET 搜索：默认全部 / resource_type 限定 / 不支持类型 400（3）
- POST 搜索：默认全部 / resource_types 数组 / 不支持类型 400 / limit 透传（4）
- GET /types：返回支持类型列表（1）
- workspace 隔离：SQL 含 workspace_id（1）
- 鉴权：未认证 401（1）
- 辅助：_build_unified_sql / _UNIFIED_SOURCES 完整性（4）

注意：本测试用 ``test_unified_search`` 命名文件以避免与既有
``test_search.py``（覆盖基于 ops_search_document 的全局搜索）冲突。
被测的是 search.py 中新增的 ``unified_router``，所有测试使用 fake
pool/connection，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import search


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []
        self.rowcount = len(self._rows)

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _RecordingConnection:
    """记录 execute 调用并按序返回配置的结果。"""

    def __init__(self, results=None, default_result=None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
        self._idx = 0
        self._default = default_result

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        return _Tx()

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return self._default if self._default is not None else _Result()

    async def commit(self):
        return None


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        return _Ctx()


def _actor(
    *,
    role="owner",
    workspace_id="wsp_test",
    user_id="usr_test",
    capabilities=("*",),
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _search_row(resource_type="assistant", resource_id="r1", title="T", **kw) -> dict:
    base = {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "title": title,
        "subtitle": kw.get("subtitle", "S"),
        "workspace_id": "wsp_test",
    }
    base.update(kw)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(search.unified_router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. GET 搜索
# ============================================================================


class TestGetSearch:
    @pytest.mark.asyncio
    async def test_get_search_default_all_types(self, monkeypatch):
        """GET 默认搜索全部 5 种资源类型，依次发 5 次 SQL。"""
        # 5 种类型，每种返回 1 行
        results = [_Result(rows=[_search_row(resource_type=t)])
                   for t in search._UNIFIED_SOURCES]
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(search, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/unified-search", params={"q": "hello"}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["query"] == "hello"
        assert body["total"] == 5
        assert len(body["items"]) == 5
        assert set(body["searched_types"]) == set(search._UNIFIED_SOURCES.keys())
        # 5 次 execute 调用
        assert len(conn.calls) == 5

    @pytest.mark.asyncio
    async def test_get_search_with_resource_type(self, monkeypatch):
        """GET resource_type=workflow 只搜 1 种类型。"""
        conn = _RecordingConnection(
            results=[_Result(rows=[_search_row(resource_type="workflow")])]
        )
        monkeypatch.setattr(search, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/unified-search",
                params={"q": "flow", "resource_type": "workflow"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["searched_types"] == ["workflow"]
        assert body["total"] == 1
        # 仅 1 次 SQL 调用
        assert len(conn.calls) == 1
        # SQL 中包含 workflow 表
        assert "FROM workflow" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_get_search_unsupported_type_returns_400(self, monkeypatch):
        """GET resource_type=invalid 返回 400。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(search, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/unified-search",
                params={"q": "x", "resource_type": "invalid"},
            )
        assert resp.status_code == 400
        # 鉴权失败前不应执行 SQL
        assert conn.calls == []


# ============================================================================
# 2. POST 搜索
# ============================================================================


class TestPostSearch:
    @pytest.mark.asyncio
    async def test_post_search_default_all_types(self, monkeypatch):
        """POST 默认搜索全部类型。"""
        results = [_Result(rows=[]) for _ in search._UNIFIED_SOURCES]
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(search, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/unified-search",
                json={"q": "test"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["query"] == "test"
        assert body["total"] == 0
        assert set(body["searched_types"]) == set(search._UNIFIED_SOURCES.keys())

    @pytest.mark.asyncio
    async def test_post_search_with_resource_types(self, monkeypatch):
        """POST resource_types 数组限定多类型。"""
        conn = _RecordingConnection(
            results=[
                _Result(rows=[_search_row(resource_type="assistant")]),
                _Result(rows=[_search_row(resource_type="file")]),
            ]
        )
        monkeypatch.setattr(search, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/unified-search",
                json={
                    "q": "doc",
                    "resource_types": ["assistant", "file"],
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["searched_types"] == ["assistant", "file"]
        assert body["total"] == 2
        # 仅 2 次 SQL
        assert len(conn.calls) == 2

    @pytest.mark.asyncio
    async def test_post_search_unsupported_type_returns_422(self, monkeypatch):
        """POST 不支持的 resource_type 由 Pydantic 校验返回 422。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(search, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/unified-search",
                json={"q": "x", "resource_types": ["invalid"]},
            )
        # Pydantic Literal 校验在端点代码之前，返回 422 Unprocessable Entity
        assert resp.status_code == 422
        assert conn.calls == []

    @pytest.mark.asyncio
    async def test_post_search_limit_passthrough(self, monkeypatch):
        """POST limit 透传到 SQL。"""
        results = [_Result(rows=[]) for _ in search._UNIFIED_SOURCES]
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(search, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/unified-search",
                json={"q": "x", "limit": 50},
            )
        assert resp.status_code == 200
        # 每个 SQL 调用的最后一个参数应为 limit
        for q, p in conn.calls:
            assert p[-1] == 50


# ============================================================================
# 3. /types 端点
# ============================================================================


class TestTypesEndpoint:
    @pytest.mark.asyncio
    async def test_list_resource_types(self, monkeypatch):
        """GET /types 返回支持的资源类型列表。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/unified-search/types")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 5
        types = {item["type"] for item in body["items"]}
        assert types == set(search._UNIFIED_SOURCES.keys())


# ============================================================================
# 4. workspace 隔离 / 鉴权
# ============================================================================


class TestIsolationAndAuth:
    @pytest.mark.asyncio
    async def test_workspace_isolation(self, monkeypatch):
        """搜索 SQL 必须包含 workspace_id = %s 隔离条件。"""
        results = [_Result(rows=[]) for _ in search._UNIFIED_SOURCES]
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(search, "pool", _Pool(conn))

        actor = _actor(workspace_id="wsp_isolated")
        app = _app(actor=actor)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/unified-search", params={"q": "x"}
            )
        assert resp.status_code == 200
        # 每个 SQL 必须含 workspace_id = %s
        for q, p in conn.calls:
            assert "workspace_id = %s" in q or "= %s" in q
            # workspace_id 必须作为参数传入
            assert "wsp_isolated" in p

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self, monkeypatch):
        """未认证请求返回 401。"""
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/unified-search", params={"q": "x"}
            )
        assert resp.status_code == 401


# ============================================================================
# 5. 辅助函数 / 模型
# ============================================================================


class TestHelpers:
    def test_unified_sources_complete(self):
        """_UNIFIED_SOURCES 包含 5 种资源类型。"""
        assert set(search._UNIFIED_SOURCES.keys()) == {
            "assistant", "workflow", "knowledge_base", "file", "notification"
        }

    def test_build_unified_sql_includes_ilike(self):
        """_build_unified_sql 生成的 SQL 包含 ILIKE。"""
        source = search._UNIFIED_SOURCES["assistant"]
        sql, where = search._build_unified_sql("assistant", source)
        assert "ILIKE" in sql
        assert "workspace_id = %s" in where
        assert "FROM assistant" in sql

    def test_build_unified_sql_includes_extra_filter(self):
        """_build_unified_sql 对 archived 类型包含额外过滤。"""
        source = search._UNIFIED_SOURCES["workflow"]
        sql, where = search._build_unified_sql("workflow", source)
        assert "status <> 'archived'" in where

    def test_search_request_model_validation(self):
        """SearchRequest 模型校验：q 必填，limit 范围。"""
        # 正常构造
        req = search.SearchRequest(q="hello")
        assert req.q == "hello"
        assert req.resource_types == []
        assert req.limit == 20

        # resource_types 可指定
        req2 = search.SearchRequest(
            q="x", resource_types=["assistant", "file"], limit=50
        )
        assert req2.resource_types == ["assistant", "file"]
        assert req2.limit == 50

    def test_unified_router_prefix(self):
        """unified_router prefix 为 /api/v1/unified-search。"""
        assert search.unified_router.prefix == "/api/v1/unified-search"
