"""Agent 技能挂载执行测试（真实 handler 调用）。

覆盖：handler 成功（同步/异步）、参数校验失败、超时、技能禁用、权限不足。
所有测试使用 fake pool/connection，不依赖真实 DB / 网络。
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import skill_market as sm


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
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
    capabilities=("skill_market:*",),
    workspace_id="wsp_test",
    user_id="usr_test",
    role="admin",
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


def _install_row(**overrides) -> dict:
    base = {
        "id": "skinst_1",
        "workspace_id": "wsp_test",
        "package_id": "pkg_test",
        "installed_version": "1.0.0",
        "config": {},
        "status": "installed",
        "enabled": True,
        "manifest_url": "https://example.com/skill/test/1.0.0",
        "package_manifest": {
            "name": "test",
            "version": "1.0.0",
            "handler": "workama_platform.modules.skill_market:_test_sync_handler",
            "input_schema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            "permissions": [],
        },
        "installed_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(sm.market_router)
    app.include_router(sm.agent_skills_router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 测试用 handler（会被 monkeypatch 注入到 skill_market 模块）
# ============================================================================


def _test_sync_handler(arguments: dict[str, Any], actor: Actor, context: dict[str, Any]) -> dict[str, Any]:
    return {"result": f"sync:{arguments.get('message')}", "handler": "sync"}


async def _test_async_handler(arguments: dict[str, Any], actor: Actor, context: dict[str, Any]) -> dict[str, Any]:
    return {"result": f"async:{arguments.get('message')}", "handler": "async"}


async def _test_slow_handler(_arguments: dict[str, Any], _actor: Actor, _context: dict[str, Any]) -> dict[str, Any]:
    await asyncio.sleep(100)
    return {"result": "too late"}


# ============================================================================
# 1. 真实 handler 调用成功
# ============================================================================


class TestRealHandlerInvoke:
    @pytest.mark.asyncio
    async def test_invoke_sync_handler_success(self, monkeypatch):
        """同步 handler 成功返回。"""
        monkeypatch.setattr(sm, "_test_sync_handler", _test_sync_handler, raising=False)
        conn = _RecordingConnection(
            results=[_Result(row=_install_row()), _Result()]
        )
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {"message": "hello"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["output"]["result"] == "sync:hello"
        assert body["output"]["handler"] == "sync"
        assert body["install_id"] == "skinst_1"

    @pytest.mark.asyncio
    async def test_invoke_async_handler_success(self, monkeypatch):
        """异步 handler 成功返回。"""
        monkeypatch.setattr(sm, "_test_async_handler", _test_async_handler, raising=False)
        row = _install_row(
            package_manifest={
                "name": "test",
                "version": "1.0.0",
                "handler": "workama_platform.modules.skill_market:_test_async_handler",
                "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
                "permissions": [],
            }
        )
        conn = _RecordingConnection(results=[_Result(row=row), _Result()])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {"message": "world"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["output"]["result"] == "async:world"
        assert body["output"]["handler"] == "async"

    @pytest.mark.asyncio
    async def test_invoke_handler_returns_non_dict(self, monkeypatch):
        """handler 返回非 dict 时自动包装。"""
        def _scalar_handler(arguments, actor, context):
            return "plain string"

        monkeypatch.setattr(sm, "_scalar_handler", _scalar_handler, raising=False)
        row = _install_row(
            package_manifest={
                "name": "test",
                "version": "1.0.0",
                "handler": "workama_platform.modules.skill_market:_scalar_handler",
                "input_schema": None,
                "permissions": [],
            }
        )
        conn = _RecordingConnection(results=[_Result(row=row), _Result()])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 200
        assert resp.json()["output"]["result"] == "plain string"


# ============================================================================
# 2. 输入参数校验
# ============================================================================


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_invoke_missing_required_field(self, monkeypatch):
        """缺少 required 字段返回 422。"""
        monkeypatch.setattr(sm, "_test_sync_handler", _test_sync_handler, raising=False)
        conn = _RecordingConnection(results=[_Result(row=_install_row())])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 422
        assert "validation" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_invoke_wrong_type(self, monkeypatch):
        """字段类型错误返回 422。"""
        monkeypatch.setattr(sm, "_test_sync_handler", _test_sync_handler, raising=False)
        conn = _RecordingConnection(results=[_Result(row=_install_row())])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {"message": 123}},
            )
        assert resp.status_code == 422


# ============================================================================
# 3. 超时
# ============================================================================


class TestTimeout:
    @pytest.mark.asyncio
    async def test_invoke_handler_timeout(self, monkeypatch):
        """handler 超时返回 500 并记录日志。"""
        monkeypatch.setattr(sm, "_test_slow_handler", _test_slow_handler, raising=False)
        row = _install_row(
            package_manifest={
                "name": "test",
                "version": "1.0.0",
                "handler": "workama_platform.modules.skill_market:_test_slow_handler",
                "input_schema": None,
                "permissions": [],
            }
        )
        conn = _RecordingConnection(results=[_Result(row=row), _Result()])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        # 缩短超时以便测试（把 asyncio.wait_for 的 timeout 参数 patch 掉不太方便，
        # 这里使用一个极短的 sleep，但 wait_for 固定 30s；为了测试可执行性，
        # 我们 monkeypatch asyncio.wait_for 直接抛出 TimeoutError）
        async def _fast_timeout(coro, timeout):
            raise asyncio.TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", _fast_timeout)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 500
        assert "timed out" in resp.json()["detail"].lower()


# ============================================================================
# 4. 技能禁用
# ============================================================================


class TestSkillDisabled:
    @pytest.mark.asyncio
    async def test_invoke_disabled_skill(self, monkeypatch):
        """enabled=False 返回 403。"""
        conn = _RecordingConnection(
            results=[_Result(row=_install_row(enabled=False))]
        )
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"].lower()


# ============================================================================
# 5. 权限不足
# ============================================================================


class TestPermissionCheck:
    @pytest.mark.asyncio
    async def test_invoke_missing_capability(self, monkeypatch):
        """Actor 缺少 manifest 要求的 capability 返回 403。"""
        row = _install_row(
            package_manifest={
                "name": "test",
                "version": "1.0.0",
                "handler": "workama_platform.modules.skill_market:_test_sync_handler",
                "input_schema": None,
                "permissions": ["skill:write"],
            }
        )
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        # Actor 只有 read 权限
        app = _app(actor=_actor(capabilities=("skill_market:read", "skill_market:write")))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 403
        assert "missing capability" in resp.json()["detail"].lower()


# ============================================================================
# 6. Handler 解析失败
# ============================================================================


class TestHandlerResolution:
    @pytest.mark.asyncio
    async def test_invoke_no_handler_in_manifest(self, monkeypatch):
        """manifest 中没有 handler 字段返回 500。"""
        row = _install_row(
            package_manifest={
                "name": "test",
                "version": "1.0.0",
                "permissions": [],
            }
        )
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 500
        assert "no handler" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_invoke_handler_bad_format(self, monkeypatch):
        """handler 格式不含冒号返回 500。"""
        row = _install_row(
            package_manifest={
                "name": "test",
                "version": "1.0.0",
                "handler": "bad_format_no_colon",
                "permissions": [],
            }
        )
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 500
        assert "format" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_invoke_handler_module_not_found(self, monkeypatch):
        """handler 模块不存在返回 500。"""
        row = _install_row(
            package_manifest={
                "name": "test",
                "version": "1.0.0",
                "handler": "nonexistent_module.handler:func",
                "permissions": [],
            }
        )
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 500
        assert "handler resolution failed" in resp.json()["detail"].lower()


# ============================================================================
# 7. mock 路径
# ============================================================================


class TestMockPath:
    @pytest.mark.asyncio
    async def test_invoke_mock_path(self, monkeypatch):
        """mock:// 路径返回确定性结果。"""
        row = _install_row(
            manifest_url="mock://skill/echo/1.0.0",
            package_manifest={
                "name": "echo",
                "version": "1.0.0",
                "handler": "workama_platform.modules.skill_market:_mock_echo_handler",
                "permissions": [],
            },
        )
        conn = _RecordingConnection(results=[_Result(row=row), _Result()])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_test/invoke",
                json={"input": {"message": "hi"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "mock-skill" in body["output"]["result"]
        assert body["output"]["method"] == "mock"
