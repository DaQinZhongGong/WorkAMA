"""技能市场与 Agent 技能挂载模块测试。

v7.162: 新增签名验证与 Agent 挂载执行测试：
- 创建包：成功 / 带签名 / 无效签名 / 无权限（4）
- 更新包：成功 / 不存在 404 / 无权限 403（3）
- 安装签名验证：有效签名 / 无效签名不阻止安装（2）
- Agent 调用：真实 handler / handler 404 / schema 校验 422（3）

所有测试使用 fake pool/connection，不依赖真实 DB / 网络。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import skill_market as sm
from workama_platform.modules.skill_market import (
    parse_manifest,
    validate_manifest,
)


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
        "package_id": "pkg_mock_echo",
        "installed_version": "1.0.0",
        "config": {},
        "status": "installed",
        "enabled": True,
        "manifest_url": "mock://skill/echo/1.0.0",
        "package_manifest": {
            "name": "echo",
            "version": "1.0.0",
            "handler": "workama_platform.modules.skill_market:_mock_echo_handler",
            "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}},
            "permissions": [],
        },
        "installed_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _log_row(**overrides) -> dict:
    base = {
        "id": "sklog_1",
        "install_id": "skinst_1",
        "input": {"query": "hello"},
        "output": {"result": "hi"},
        "tokens_used": 0,
        "duration_ms": 120,
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _package_row(**overrides) -> dict:
    base = {
        "id": "pkg_test",
        "name": "test",
        "version": "1.0.0",
        "description": "",
        "manifest_url": "http://example.com/test",
        "author": "",
        "tags": [],
        "downloads": 0,
        "rating": 0.0,
        "status": "draft",
        "manifest": {},
        "signature": "",
        "public_key": "",
        "public_key_hash": "",
        "verified_at": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _generate_signed_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    """生成带有有效 Ed25519 签名的 manifest。返回 (manifest, signature_b64, public_key_b64)。"""
    import base64
    import hashlib
    import json

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    pub_bytes = public_key.public_bytes_raw()
    pub_b64 = base64.b64encode(pub_bytes).decode()

    canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    sig = private_key.sign(digest)
    sig_b64 = base64.b64encode(sig).decode()

    return manifest, sig_b64, pub_b64


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(sm.market_router)
    app.include_router(sm.agent_skills_router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 市场列表
# ============================================================================


class TestMarketList:
    @pytest.mark.asyncio
    async def test_list_market_packages_success(self):
        """GET /api/v1/skills/market 返回 mock 包列表。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/skills/market")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 3
        assert any(p["name"] == "echo" for p in body["items"])

    @pytest.mark.asyncio
    async def test_search_by_keyword(self):
        """q 参数过滤名称和描述。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/skills/market?q=echo")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["items"][0]["name"] == "echo"

    @pytest.mark.asyncio
    async def test_filter_by_tag(self):
        """tag 参数按标签过滤。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/skills/market?tag=math")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["items"][0]["name"] == "math"

    @pytest.mark.asyncio
    async def test_pagination(self):
        """limit / offset 分页。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/skills/market?limit=1&offset=1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["total"] == 3


# ============================================================================
# 2. 包详情
# ============================================================================


class TestPackageDetail:
    @pytest.mark.asyncio
    async def test_get_package_detail(self):
        """GET /api/v1/skills/market/{package_id} 成功。"""
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/skills/market/pkg_mock_echo")
        assert resp.status_code == 200
        assert resp.json()["package"]["name"] == "echo"

    @pytest.mark.asyncio
    async def test_get_package_not_found(self, monkeypatch):
        """不存在的 package_id 返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/skills/market/pkg_missing")
        assert resp.status_code == 404


# ============================================================================
# 2.5 创建包
# ============================================================================


class TestCreatePackage:
    @pytest.mark.asyncio
    async def test_create_package_success(self, monkeypatch):
        """POST /api/v1/skills/market 创建包成功。"""
        conn = _RecordingConnection(results=[_Result()])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market",
                json={"name": "test", "version": "1.0.0"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["package"]["name"] == "test"
        assert body["package"]["status"] == "draft"
        assert body["package"]["verified_at"] is None

    @pytest.mark.asyncio
    async def test_create_package_with_signature(self, monkeypatch):
        """创建包时提供有效签名，verified_at 被设置。"""
        manifest, sig, pub = _generate_signed_manifest({"name": "test", "version": "1.0.0"})
        conn = _RecordingConnection(results=[_Result()])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market",
                json={
                    "name": "test",
                    "version": "1.0.0",
                    "manifest": manifest,
                    "signature": sig,
                    "public_key": pub,
                },
            )
        assert resp.status_code == 201
        assert resp.json()["package"]["verified_at"] is not None

    @pytest.mark.asyncio
    async def test_create_package_invalid_signature(self, monkeypatch):
        """创建包时提供无效签名，verified_at 为 None 但不报错。"""
        conn = _RecordingConnection(results=[_Result()])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market",
                json={
                    "name": "test",
                    "version": "1.0.0",
                    "manifest": {"name": "test"},
                    "signature": "bad",
                    "public_key": "bad",
                },
            )
        assert resp.status_code == 201
        assert resp.json()["package"]["verified_at"] is None

    @pytest.mark.asyncio
    async def test_create_no_permission(self):
        """无 write 权限返回 403。"""
        app = _app(actor=_actor(capabilities=("skill_market:read",), role="viewer"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market",
                json={"name": "test", "version": "1.0.0"},
            )
        assert resp.status_code == 403


# ============================================================================
# 2.6 更新包
# ============================================================================


class TestUpdatePackage:
    @pytest.mark.asyncio
    async def test_update_package_success(self, monkeypatch):
        """PATCH /api/v1/skills/market/{id} 更新成功。"""
        pkg = _package_row()
        updated = _package_row(description="updated")
        conn = _RecordingConnection(
            results=[_Result(row=pkg), _Result(), _Result(row=updated)]
        )
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                f"/api/v1/skills/market/{pkg['id']}",
                json={"description": "updated"},
            )
        assert resp.status_code == 200
        assert resp.json()["package"]["description"] == "updated"

    @pytest.mark.asyncio
    async def test_update_package_not_found(self, monkeypatch):
        """更新不存在的包返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/skills/market/pkg_missing",
                json={"description": "updated"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_no_permission(self):
        """无 write 权限返回 403。"""
        app = _app(actor=_actor(capabilities=("skill_market:read",), role="viewer"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/skills/market/pkg_test",
                json={"description": "updated"},
            )
        assert resp.status_code == 403


# ============================================================================
# 3. 安装
# ============================================================================


class TestInstall:
    @pytest.mark.asyncio
    async def test_install_package_success(self, monkeypatch):
        """POST /api/v1/skills/market/{package_id}/install 成功。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market/pkg_mock_echo/install",
                json={"package_id": "pkg_mock_echo", "config": {"key": "val"}},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["install"]["package_id"] == "pkg_mock_echo"
        assert body["install"]["status"] == "installed"
        assert body["deduplicated"] is False

    @pytest.mark.asyncio
    async def test_install_package_idempotent(self, monkeypatch):
        """重复安装返回已有记录。"""
        existing = _install_row()
        conn = _RecordingConnection(
            results=[_Result(row=None), _Result(row=existing)]
        )
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market/pkg_mock_echo/install",
                json={"package_id": "pkg_mock_echo"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["deduplicated"] is True

    @pytest.mark.asyncio
    async def test_install_package_not_found(self, monkeypatch):
        """安装不存在的包 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market/pkg_missing/install",
                json={"package_id": "pkg_missing"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_install_no_permission(self):
        """无 install 权限返回 403。"""
        app = _app(actor=_actor(capabilities=("skill_market:read",), role="viewer"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market/pkg_mock_echo/install",
                json={"package_id": "pkg_mock_echo"},
            )
        assert resp.status_code == 403


# ============================================================================
# 3.5 安装签名验证
# ============================================================================


class TestInstallSignature:
    @pytest.mark.asyncio
    async def test_install_real_package_signature_valid(self, monkeypatch):
        """安装真实包且签名有效时允许安装。"""
        manifest, sig, pub = _generate_signed_manifest({"name": "real", "version": "1.0.0"})
        pkg = _package_row(
            id="pkg_real",
            manifest_url="http://example.com/real",
            manifest=manifest,
            signature=sig,
            public_key=pub,
        )
        conn = _RecordingConnection(
            results=[
                _Result(row=pkg),   # SELECT skill_package
                _Result(row=None),  # SELECT skill_install (幂等)
                _Result(),          # INSERT skill_install
            ]
        )
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market/pkg_real/install",
                json={"package_id": "pkg_real"},
            )
        assert resp.status_code == 201
        assert resp.json()["install"]["package_id"] == "pkg_real"

    @pytest.mark.asyncio
    async def test_install_real_package_signature_invalid(self, monkeypatch):
        """安装真实包签名无效时阻止安装，返回 400。"""
        manifest = {"name": "real", "version": "1.0.0"}
        pkg = _package_row(
            id="pkg_real",
            manifest_url="http://example.com/real",
            manifest=manifest,
            signature="bad",
            public_key="bad",
        )
        conn = _RecordingConnection(
            results=[
                _Result(row=pkg),
                _Result(row=None),
                _Result(),
            ]
        )
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market/pkg_real/install",
                json={"package_id": "pkg_real"},
            )
        assert resp.status_code == 400


# ============================================================================
# 4. 已安装列表
# ============================================================================


class TestInstalledList:
    @pytest.mark.asyncio
    async def test_list_installed(self, monkeypatch):
        """GET /api/v1/skills/market/installed 成功。"""
        rows = [_install_row(id="a"), _install_row(id="b")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/skills/market/installed")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2

    @pytest.mark.asyncio
    async def test_list_installed_empty(self, monkeypatch):
        """空列表。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/skills/market/installed")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


# ============================================================================
# 5. 卸载
# ============================================================================


class TestUninstall:
    @pytest.mark.asyncio
    async def test_uninstall_success(self, monkeypatch):
        """DELETE /api/v1/skills/market/{install_id} 成功。"""
        conn = _RecordingConnection(
            results=[_Result(row=_install_row()), _Result()]
        )
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/skills/market/skinst_1")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    @pytest.mark.asyncio
    async def test_uninstall_not_found(self, monkeypatch):
        """卸载不存在的记录 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/skills/market/skinst_missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_uninstall_no_permission(self):
        """无权限返回 403。"""
        app = _app(actor=_actor(capabilities=("skill_market:read",), role="viewer"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/skills/market/skinst_1")
        assert resp.status_code == 403


# ============================================================================
# 6. 调用日志
# ============================================================================


class TestInvocationLogs:
    @pytest.mark.asyncio
    async def test_list_logs_success(self, monkeypatch):
        """GET /api/v1/skills/market/{install_id}/logs 成功。"""
        conn = _RecordingConnection(
            results=[_Result(row=_install_row()), _Result(rows=[_log_row()])]
        )
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/skills/market/skinst_1/logs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["items"][0]["install_id"] == "skinst_1"

    @pytest.mark.asyncio
    async def test_list_logs_empty(self, monkeypatch):
        """无日志返回空列表。"""
        conn = _RecordingConnection(
            results=[_Result(row=_install_row()), _Result(rows=[])]
        )
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/skills/market/skinst_1/logs")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_list_logs_install_not_found(self, monkeypatch):
        """安装记录不存在 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/skills/market/skinst_missing/logs")
        assert resp.status_code == 404


# ============================================================================
# 7. Agent 技能列表
# ============================================================================


class TestAgentSkillsList:
    @pytest.mark.asyncio
    async def test_list_agent_skills(self, monkeypatch):
        """GET /api/v1/agent/skills 成功。"""
        rows = [_install_row(id="a"), _install_row(id="b")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/agent/skills")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2


# ============================================================================
# 8. Agent 技能注册
# ============================================================================


class TestAgentSkillRegister:
    @pytest.mark.asyncio
    async def test_register_success(self, monkeypatch):
        """POST /api/v1/agent/skills 注册成功。"""
        conn = _RecordingConnection(results=[_Result(row=_install_row())])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills",
                json={"skill_id": "pkg_mock_echo", "name": "Echo", "config": {}},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["registered"] is True
        assert body["skill_id"] == "pkg_mock_echo"

    @pytest.mark.asyncio
    async def test_register_not_installed(self, monkeypatch):
        """注册未安装的技能 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills",
                json={"skill_id": "pkg_missing", "name": "Missing", "config": {}},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_register_no_permission(self):
        """无 write 权限返回 403。"""
        app = _app(actor=_actor(capabilities=("skill_market:read",), role="viewer"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills",
                json={"skill_id": "pkg_mock_echo", "name": "Echo", "config": {}},
            )
        assert resp.status_code == 403


# ============================================================================
# 9. Agent 技能调用
# ============================================================================


class TestAgentSkillInvoke:
    @pytest.mark.asyncio
    async def test_invoke_success(self, monkeypatch):
        """POST /api/v1/agent/skills/{skill_id}/invoke 成功。"""
        conn = _RecordingConnection(
            results=[_Result(row=_install_row()), _Result()]
        )
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_mock_echo/invoke",
                json={"input": {"query": "hi"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "mock-skill" in body["output"]["result"]
        assert body["install_id"] == "skinst_1"

    @pytest.mark.asyncio
    async def test_invoke_not_installed(self, monkeypatch):
        """调用未安装的技能 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_missing/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_invoke_no_permission(self):
        """无 write 权限返回 403。"""
        app = _app(actor=_actor(capabilities=("skill_market:read",), role="viewer"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_mock_echo/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_invoke_real_handler(self, monkeypatch):
        """调用真实 handler（非 mock 路径）。"""
        row = _install_row(
            manifest_url="http://example.com/echo",
            package_manifest={
                "name": "echo",
                "version": "1.0.0",
                "handler": "workama_platform.modules.skill_market:_mock_echo_handler",
                "input_schema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                },
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
                "/api/v1/agent/skills/pkg_mock_echo/invoke",
                json={"input": {"message": "hello"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["output"]["result"] == "hello"
        assert body["output"]["handler"] == "echo"
        assert body["install_id"] == "skinst_1"

    @pytest.mark.asyncio
    async def test_invoke_handler_not_found(self, monkeypatch):
        """handler 不可解析返回 500。"""
        row = _install_row(
            manifest_url="http://example.com/bad",
            package_manifest={
                "handler": "nonexistent.module:bad_func",
                "input_schema": {},
                "permissions": [],
            },
        )
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_mock_echo/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_invoke_input_validation_failed(self, monkeypatch):
        """输入 schema 校验失败返回 422。"""
        row = _install_row(
            manifest_url="http://example.com/echo",
            package_manifest={
                "handler": "workama_platform.modules.skill_market:_mock_echo_handler",
                "input_schema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                "permissions": [],
            },
        )
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/agent/skills/pkg_mock_echo/invoke",
                json={"input": {}},
            )
        assert resp.status_code == 422


# ============================================================================
# 10. Agent 技能注销
# ============================================================================


class TestAgentSkillUnregister:
    @pytest.mark.asyncio
    async def test_unregister_success(self, monkeypatch):
        """DELETE /api/v1/agent/skills/{skill_id} 成功。"""
        conn = _RecordingConnection(results=[_Result(row=_install_row())])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/agent/skills/pkg_mock_echo")
        assert resp.status_code == 200
        assert resp.json()["unregistered"] is True

    @pytest.mark.asyncio
    async def test_unregister_not_found(self, monkeypatch):
        """注销不存在的技能 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/agent/skills/pkg_missing")
        assert resp.status_code == 404


# ============================================================================
# 11. Manifest 解析
# ============================================================================


class TestManifestParsing:
    def test_parse_manifest_json(self):
        raw = '{"name": "x", "version": "1.0.0", "entrypoint": "main.py", "tools": [{"name": "t1"}]}'
        data = parse_manifest(raw)
        assert data["name"] == "x"

    def test_parse_manifest_yaml(self):
        raw = "name: y\nversion: 1.0.0\nentrypoint: main.py\ntools:\n  - name: t1"
        data = parse_manifest(raw)
        assert data["name"] == "y"

    def test_validate_manifest_missing_fields(self):
        with pytest.raises(ValueError, match="missing required fields"):
            validate_manifest({"name": "x"})

    def test_validate_manifest_empty_name(self):
        with pytest.raises(ValueError, match="non-empty"):
            validate_manifest({
                "name": "", "version": "1.0.0", "entrypoint": "main.py", "tools": []
            })

    def test_validate_manifest_tools_must_be_list(self):
        with pytest.raises(ValueError, match="tools must be a list"):
            validate_manifest({
                "name": "x", "version": "1.0.0", "entrypoint": "main.py", "tools": "bad"
            })

    def test_validate_manifest_tool_missing_name(self):
        with pytest.raises(ValueError, match="each tool must have a name"):
            validate_manifest({
                "name": "x", "version": "1.0.0", "entrypoint": "main.py", "tools": [{}]
            })

    def test_parse_manifest_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            parse_manifest("")

    def test_parse_manifest_invalid_json_raises(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            parse_manifest("{bad")


# ============================================================================
# 12. 权限矩阵
# ============================================================================


class TestRequire:
    def test_require_read_with_viewer_role(self):
        actor = _actor(role="viewer", capabilities=())
        sm._require(actor, "read")  # 不抛异常

    def test_require_write_with_viewer_role_raises(self):
        actor = _actor(role="viewer", capabilities=())
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            sm._require(actor, "write")
        assert exc_info.value.status_code == 403
