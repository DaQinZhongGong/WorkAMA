from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import agent_tools


# --- 测试辅助：模拟 psycopg 连接池与 httpx 客户端 ----------------------
# 参考 test_approvals.py / test_code.py 的内联 mock 风格：直接调用路由
# 函数并替换模块级的 pool / httpx 引用，避免触碰真实 DB 与外部 HTTP。


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _SessionConnection:
    """仅识别 ag_session 存在性查询的连接 mock。"""

    def __init__(self, *, exists: bool):
        self._exists = exists

    async def execute(self, query, params=()):
        if "ag_session" in query:
            return _Result(row={"id": "ses_test"} if self._exists else None)
        return _Result()

    async def commit(self):
        return None


class _Pool:
    """最小化的连接池上下文管理器。"""

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


def _actor() -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id="wsp_test",
        org_id="org_test",
        role="member",
        email="member@example.test",
        display_name="Member",
        onboarding_completed=True,
    )


def _install_fake_httpx(monkeypatch, *, response=None, error=None):
    """替换 agent_tools 模块中的 httpx 引用，避免真实外部 HTTP 调用。

    暴露与源码一致的 AsyncClient（异步上下文管理器）与 HTTPError，
    使 ``except httpx.HTTPError`` 分支能够正确捕获。
    """

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, headers=None, params=None):
            if error is not None:
                raise error
            return response

    fake = SimpleNamespace(AsyncClient=_AsyncClient, HTTPError=httpx.HTTPError)
    monkeypatch.setattr(agent_tools, "httpx", fake)


# --- GET /api/v1/sessions/{session_id}/sandbox --------------------------


@pytest.mark.asyncio
async def test_get_session_sandbox_returns_404_when_session_missing(monkeypatch):
    # Arrange: 数据库中不存在该 session
    monkeypatch.setattr(agent_tools, "pool", _Pool(_SessionConnection(exists=False)))

    # Act + Assert: 触发 404
    with pytest.raises(HTTPException) as exc:
        await agent_tools.get_session_sandbox("ses_missing", _actor())
    assert exc.value.status_code == 404
    assert exc.value.detail == "Session not found"


@pytest.mark.asyncio
async def test_get_session_sandbox_returns_projection_on_success(monkeypatch):
    # Arrange: session 存在，sandbox-fleet 返回 200 + 完整负载
    monkeypatch.setattr(agent_tools, "pool", _Pool(_SessionConnection(exists=True)))
    payload = {
        "id": "sbx_1",
        "status": "running",
        "runtime": "python:3.12",
        "gvisor_compliant": True,
        "meter_seconds": 42,
        "started_at": "2026-07-22T00:00:00Z",
        "last_active_at": "2026-07-22T00:01:00Z",
        # 多余字段应被投影过滤掉
        "internal_secret": "should-not-leak",
    }

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    _install_fake_httpx(monkeypatch, response=_Response())

    # Act
    result = await agent_tools.get_session_sandbox("ses_test", _actor())

    # Assert: 仅投影白名单字段
    assert result == {
        "id": "sbx_1",
        "status": "running",
        "runtime": "python:3.12",
        "gvisor_compliant": True,
        "meter_seconds": 42,
        "started_at": "2026-07-22T00:00:00Z",
        "last_active_at": "2026-07-22T00:01:00Z",
    }
    assert "internal_secret" not in result


@pytest.mark.asyncio
async def test_get_session_sandbox_returns_503_when_fleet_raises_http_error(monkeypatch):
    # Arrange: session 存在，但 sandbox-fleet 调用抛出 httpx.HTTPError
    monkeypatch.setattr(agent_tools, "pool", _Pool(_SessionConnection(exists=True)))
    _install_fake_httpx(monkeypatch, error=httpx.HTTPError("transport down"))

    # Act + Assert: HTTPError 被映射为 503
    with pytest.raises(HTTPException) as exc:
        await agent_tools.get_session_sandbox("ses_test", _actor())
    assert exc.value.status_code == 503
    assert exc.value.detail == "Sandbox fleet is unavailable"


@pytest.mark.asyncio
async def test_get_session_sandbox_returns_none_status_when_fleet_404(monkeypatch):
    # Arrange: session 存在，但 sandbox-fleet 自身返回 404（沙箱尚未创建）
    monkeypatch.setattr(agent_tools, "pool", _Pool(_SessionConnection(exists=True)))

    class _Response:
        status_code = 404

        def raise_for_status(self):
            return None

        def json(self):
            return {}

    _install_fake_httpx(monkeypatch, response=_Response())

    # Act
    result = await agent_tools.get_session_sandbox("ses_test", _actor())

    # Assert: 约定返回 {"status": "none"} 而非抛错
    assert result == {"status": "none"}


# --- GET /api/v1/tools ---------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_returns_registry_augmented_with_workspace(monkeypatch):
    # Arrange: agent-server 返回 tool registry 负载
    registry = {"tools": [{"name": "file.read"}], "version": "1"}

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return registry

    _install_fake_httpx(monkeypatch, response=_Response())

    # Act
    result = await agent_tools.list_tools(_actor())

    # Assert: 原负载合并 actor.workspace_id
    assert result["tools"] == [{"name": "file.read"}]
    assert result["version"] == "1"
    assert result["workspace_id"] == "wsp_test"


@pytest.mark.asyncio
async def test_list_tools_returns_503_when_agent_server_unavailable(monkeypatch):
    # Arrange: agent-server 调用抛出 httpx.HTTPError
    _install_fake_httpx(monkeypatch, error=httpx.HTTPError("agent server down"))

    # Act + Assert: HTTPError 被映射为 503
    with pytest.raises(HTTPException) as exc:
        await agent_tools.list_tools(_actor())
    assert exc.value.status_code == 503
    assert exc.value.detail == "Agent tool registry is unavailable"


# --- 路由契约 ------------------------------------------------------------


def test_router_exposes_sandbox_and_tools_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in agent_tools.router.routes}
    assert ("/api/v1/sessions/{session_id}/sandbox", ("GET",)) in paths
    assert ("/api/v1/tools", ("GET",)) in paths
