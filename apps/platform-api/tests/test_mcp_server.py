"""MCP 服务端模块 (mcp_server) 单元 + 端点测试。

v7.147: 22 个测试覆盖：
- 注册：成功 / 重名 409 / 字段校验 / 缺写权限 403 / 未认证 401 (5)
- 列表：分页 + system 内置 / kind 过滤 / workspace 隔离 (3)
- 详情：存在 / 404 / workspace 越权 403 (3)
- 删除：成功 / 404 / 内置工具不可删 403 / 缺写权限 403 (4)
- 调用：get_current_time / echo / handler 不可解析 404 / 工具禁用 409 (4)
- schema：返回 JSON Schema (1)
- manifest：MCP list_tools 格式 / 缺读权限 403 (2)

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import mcp_server as ms


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    """模拟 psycopg 查询结果。"""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

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
    """记录 execute 调用并按序返回配置的结果。"""

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
    """模拟连接池。"""

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
    capabilities=("mcp:*",),
    workspace_id="wsp_test",
    user_id="usr_test",
    role="admin",
    email="admin@workama.example.com",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email=email,
        display_name="Admin",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _row(**overrides) -> dict:
    base = {
        "id": "mcp_1",
        "workspace_id": "wsp_test",
        "name": "my_tool",
        "description": "a test tool",
        "input_schema": {"type": "object"},
        "output_schema": None,
        "kind": "function",
        "handler": "workama_platform.modules.mcp_server.builtin_get_current_time",
        "status": "active",
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(ms.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 注册工具
# ============================================================================


class TestRegister:
    """POST /api/v1/mcp/tools 注册工具。"""

    @pytest.mark.asyncio
    async def test_register_tool_success(self, monkeypatch):
        """POST 成功返回 201。"""
        row = _row(id="mcp_new", name="my_tool")
        conn = _RecordingConnection(results=[_Result(row=None), _Result(row=row)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/mcp/tools",
                json={
                    "name": "my_tool",
                    "description": "a test tool",
                    "input_schema": {"type": "object"},
                    "handler": "workama_platform.modules.mcp_server.builtin_echo",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "my_tool"
        assert body["status"] == "active"
        # 第一条 SQL 为重名检查 SELECT，第二条为 INSERT RETURNING
        assert "SELECT 1 FROM mcp_tool" in conn.calls[0][0]
        assert "INSERT INTO mcp_tool" in conn.calls[1][0]

    @pytest.mark.asyncio
    async def test_register_tool_duplicate_name_returns_409(self, monkeypatch):
        """POST 重名返回 409。"""
        conn = _RecordingConnection(results=[_Result(row={"1": 1})])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/mcp/tools",
                json={"name": "my_tool", "handler": "x.y.z"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_register_tool_field_validation(self):
        """POST 空 name 触发 422。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/mcp/tools",
                json={"name": "", "handler": "x.y.z"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_register_tool_requires_write_capability(self, monkeypatch):
        """member 角色（仅 mcp:read）注册工具返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member", capabilities=("mcp:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/mcp/tools",
                json={"name": "my_tool", "handler": "x.y.z"},
            )
        assert resp.status_code == 403
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_register_tool_requires_authentication(self):
        """未认证请求返回 401。"""
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/mcp/tools",
                json={"name": "my_tool", "handler": "x.y.z"},
            )
        assert resp.status_code == 401


# ============================================================================
# 2. 列表工具
# ============================================================================


class TestList:
    """GET /api/v1/mcp/tools 列表工具。"""

    @pytest.mark.asyncio
    async def test_list_tools_pagination_and_system_inclusion(self, monkeypatch):
        """GET 分页返回工具，SQL 含 system + actor.workspace_id。"""
        rows = [_row(id="mcp_1"), _row(id="mcp_sys", workspace_id="system")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test", role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/mcp/tools?limit=10&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["limit"] == 10
        # SQL 使用 ANY + 含 system 与 wsp_test
        query, params = conn.calls[0]
        assert "workspace_id = ANY(%s)" in query
        assert "system" in params[0]
        assert "wsp_test" in params[0]

    @pytest.mark.asyncio
    async def test_list_tools_kind_filter(self, monkeypatch):
        """GET ?kind=function 仅返回 function 工具。"""
        rows = [_row(id="mcp_1", kind="function")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/mcp/tools?kind=function")
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "AND kind = %s" in query
        assert "function" in params

    @pytest.mark.asyncio
    async def test_list_tools_workspace_isolation(self, monkeypatch):
        """GET 工具列表仅含 system + actor.workspace_id，不含其他 workspace。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_mine", role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/mcp/tools")
        assert resp.status_code == 200
        _, params = conn.calls[0]
        # ANY 参数应包含 system 与 wsp_mine，不含 wsp_other
        assert "system" in params[0]
        assert "wsp_mine" in params[0]
        assert "wsp_other" not in params[0]


# ============================================================================
# 3. 单工具详情
# ============================================================================


class TestGetTool:
    """GET /api/v1/mcp/tools/{tool_id} 工具详情。"""

    @pytest.mark.asyncio
    async def test_get_tool_exists(self, monkeypatch):
        """GET /{tool_id} 返回工具详情。"""
        row = _row(id="mcp_1")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/mcp/tools/mcp_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "mcp_1"
        assert body["name"] == "my_tool"

    @pytest.mark.asyncio
    async def test_get_tool_returns_404_when_missing(self, monkeypatch):
        """GET /{tool_id} 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/mcp/tools/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_tool_returns_403_cross_workspace(self, monkeypatch):
        """GET /{tool_id} 工具属于其他 workspace 返回 403。"""
        row = _row(id="mcp_1", workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test", role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/mcp/tools/mcp_1")
        assert resp.status_code == 403


# ============================================================================
# 4. 注销工具
# ============================================================================


class TestDeleteTool:
    """DELETE /api/v1/mcp/tools/{tool_id} 注销工具。"""

    @pytest.mark.asyncio
    async def test_delete_tool_success(self, monkeypatch):
        """DELETE 成功返回 204。"""
        existing = _row(id="mcp_1", workspace_id="wsp_test")
        conn = _RecordingConnection(
            results=[_Result(row=existing), _Result(row={"id": "mcp_1"})]
        )
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/mcp/tools/mcp_1")
        assert resp.status_code == 204
        assert "DELETE FROM mcp_tool" in conn.calls[1][0]

    @pytest.mark.asyncio
    async def test_delete_tool_returns_404_when_missing(self, monkeypatch):
        """DELETE 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/mcp/tools/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_builtin_tool_returns_403(self, monkeypatch):
        """DELETE 内置工具（system workspace）返回 403。"""
        existing = _row(id="mcp_1", workspace_id="system")
        conn = _RecordingConnection(results=[_Result(row=existing)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/mcp/tools/mcp_1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_tool_requires_write(self, monkeypatch):
        """member 角色删除工具返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member", capabilities=("mcp:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/mcp/tools/mcp_1")
        assert resp.status_code == 403
        assert len(conn.calls) == 0


# ============================================================================
# 5. 调用工具
# ============================================================================


class TestInvokeTool:
    """POST /api/v1/mcp/tools/{tool_id}/invoke 调用工具。"""

    @pytest.mark.asyncio
    async def test_invoke_get_current_time(self, monkeypatch):
        """invoke get_current_time 工具返回 UTC 时间。"""
        row = _row(
            id="mcp_1",
            name="get_current_time",
            handler="workama_platform.modules.mcp_server.builtin_get_current_time",
        )
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/mcp/tools/mcp_1/invoke",
                json={"arguments": {}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tool_id"] == "mcp_1"
        assert body["tool_name"] == "get_current_time"
        assert body["is_error"] is False
        assert "iso" in body["result"]
        assert body["result"]["timezone"] == "UTC"

    @pytest.mark.asyncio
    async def test_invoke_echo(self, monkeypatch):
        """invoke echo 工具回显参数。"""
        row = _row(
            id="mcp_2",
            name="echo",
            handler="workama_platform.modules.mcp_server.builtin_echo",
        )
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/mcp/tools/mcp_2/invoke",
                json={"arguments": {"message": "hello"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"]["echo"] == {"message": "hello"}

    @pytest.mark.asyncio
    async def test_invoke_handler_not_resolvable_returns_404(self, monkeypatch):
        """invoke handler 不可解析返回 404。"""
        row = _row(
            id="mcp_3",
            name="bad",
            handler="workama_platform.modules.mcp_server.nonexistent_func_xyz",
        )
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/mcp/tools/mcp_3/invoke",
                json={"arguments": {}},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_invoke_disabled_tool_returns_409(self, monkeypatch):
        """invoke 已禁用工具返回 409。"""
        row = _row(id="mcp_4", name="off", status="disabled")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/mcp/tools/mcp_4/invoke",
                json={"arguments": {}},
            )
        assert resp.status_code == 409


# ============================================================================
# 6. Schema
# ============================================================================


class TestSchema:
    """GET /api/v1/mcp/tools/{tool_id}/schema 工具 JSON Schema。"""

    @pytest.mark.asyncio
    async def test_get_tool_schema(self, monkeypatch):
        """GET /schema 返回 MCP 协议兼容的 JSON Schema。"""
        row = _row(
            id="mcp_1",
            name="echo",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        )
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/mcp/tools/mcp_1/schema")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "echo"
        assert body["inputSchema"]["type"] == "object"
        assert "msg" in body["inputSchema"]["properties"]
        assert body["kind"] == "function"


# ============================================================================
# 7. Manifest
# ============================================================================


class TestManifest:
    """GET /api/v1/mcp/manifest MCP 服务清单。"""

    @pytest.mark.asyncio
    async def test_get_manifest_mcp_format(self, monkeypatch):
        """GET /manifest 返回 MCP list_tools 响应格式。"""
        rows = [
            {"name": "get_current_time", "description": "time", "input_schema": {"type": "object"}},
            {"name": "echo", "description": "echo", "input_schema": {"type": "object"}},
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/mcp/manifest")
        assert resp.status_code == 200
        body = resp.json()
        assert "tools" in body
        assert len(body["tools"]) == 2
        assert body["tools"][0]["name"] == "get_current_time"
        assert body["tools"][0]["inputSchema"] == {"type": "object"}
        assert body["protocolVersion"] == "2025-06-18"

    @pytest.mark.asyncio
    async def test_get_manifest_requires_read(self, monkeypatch):
        """无 mcp:read 能力的 actor 访问 manifest 返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        app = _app(
            actor=Actor(
                user_id="usr_x",
                workspace_id="wsp_test",
                org_id="org_test",
                role="external",
                email="ext@example.com",
                display_name="Ext",
                onboarding_completed=True,
                capabilities=(),
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/mcp/manifest")
        assert resp.status_code == 403
        assert len(conn.calls) == 0


# ============================================================================
# 8. 辅助函数与内置工具
# ============================================================================


class TestHelpersAndBuiltins:
    """辅助函数与内置工具测试。"""

    def test_resolve_handler_loads_builtin(self):
        """_resolve_handler 能解析内置 handler 函数。"""
        handler = ms._resolve_handler(
            "workama_platform.modules.mcp_server.builtin_get_current_time"
        )
        assert handler is ms.builtin_get_current_time

    def test_resolve_handler_returns_none_for_missing(self):
        """_resolve_handler 不存在的函数返回 None。"""
        assert ms._resolve_handler(
            "workama_platform.modules.mcp_server.no_such_func"
        ) is None
        assert ms._resolve_handler("no_module.no_func") is None
        assert ms._resolve_handler(None) is None
        assert ms._resolve_handler("no_dot_path") is None

    def test_builtin_get_current_time_returns_iso(self):
        """builtin_get_current_time 返回 ISO 时间。"""
        result = ms.builtin_get_current_time({}, _actor())
        assert "iso" in result
        assert result["timezone"] == "UTC"
        assert "epoch" in result

    def test_builtin_echo_returns_arguments(self):
        """builtin_echo 回显参数。"""
        result = ms.builtin_echo({"msg": "hi"}, _actor())
        assert result["echo"] == {"msg": "hi"}

    def test_builtin_get_workspace_info_returns_actor(self):
        """builtin_get_workspace_info 返回 actor workspace 信息。"""
        result = ms.builtin_get_workspace_info({}, _actor(workspace_id="wsp_x"))
        assert result["workspace_id"] == "wsp_x"
        assert result["role"] == "admin"

    def test_builtin_tools_count_is_three(self):
        """BUILTIN_TOOLS 共 3 个工具。"""
        names = [t["name"] for t in ms.BUILTIN_TOOLS]
        assert set(names) == {"get_current_time", "echo", "get_workspace_info"}

    @pytest.mark.asyncio
    async def test_ensure_builtin_mcp_tools_idempotent(self, monkeypatch):
        """ensure_builtin_mcp_tools 幂等：已存在的工具跳过 INSERT。"""
        # 3 次存在检查均返回已有 → 不应触发 INSERT
        conn = _RecordingConnection(
            results=[_Result(row={"id": "x"}), _Result(row={"id": "x"}), _Result(row={"id": "x"})]
        )
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        await ms.ensure_builtin_mcp_tools()
        # 仅 3 次 SELECT，无 INSERT
        assert len(conn.calls) == 3
        for query, _ in conn.calls:
            assert "SELECT id FROM mcp_tool" in query

    @pytest.mark.asyncio
    async def test_ensure_builtin_mcp_tools_inserts_when_missing(self, monkeypatch):
        """ensure_builtin_mcp_tools 不存在时执行 INSERT。"""
        # 3 次存在检查均返回 None → 3 次 INSERT
        conn = _RecordingConnection(
            results=[_Result(row=None), _Result(row=None), _Result(row=None)]
        )
        monkeypatch.setattr(ms, "pool", _Pool(conn))

        await ms.ensure_builtin_mcp_tools()
        # 3 次 SELECT + 3 次 INSERT
        assert len(conn.calls) == 6
        insert_calls = [q for q, _ in conn.calls if "INSERT INTO mcp_tool" in q]
        assert len(insert_calls) == 3
