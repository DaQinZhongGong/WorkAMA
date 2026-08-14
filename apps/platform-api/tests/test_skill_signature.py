"""技能包签名验证测试。

覆盖：签名成功/失败/非法公钥/mock跳过/安装端点签名门禁。
所有测试使用 fake pool/connection，不依赖真实 DB / 网络。
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
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


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(sm.market_router)
    app.include_router(sm.agent_skills_router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


def _generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    pub_b64 = base64.b64encode(public_key.public_bytes_raw()).decode()
    return private_key, pub_b64


def _sign_manifest(private_key: Ed25519PrivateKey, manifest: dict[str, Any]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    signature = private_key.sign(digest)
    return base64.b64encode(signature).decode()


# ============================================================================
# 1. verify_skill_signature 单元测试
# ============================================================================


class TestVerifySkillSignature:
    def test_verify_success(self):
        priv, pub = _generate_keypair()
        manifest = {"name": "test", "version": "1.0.0"}
        sig = _sign_manifest(priv, manifest)
        assert sm.verify_skill_signature(manifest, sig, pub) is True

    def test_verify_failure_wrong_manifest(self):
        priv, pub = _generate_keypair()
        manifest = {"name": "test", "version": "1.0.0"}
        sig = _sign_manifest(priv, manifest)
        assert sm.verify_skill_signature({"name": "tampered"}, sig, pub) is False

    def test_verify_failure_invalid_public_key(self):
        manifest = {"name": "test"}
        assert sm.verify_skill_signature(manifest, "aGVsbG8=", "invalid-key!!!") is False

    def test_verify_empty_signature_returns_false(self):
        priv, pub = _generate_keypair()
        manifest = {"name": "test"}
        assert sm.verify_skill_signature(manifest, "", pub) is False

    def test_verify_empty_public_key_returns_false(self):
        manifest = {"name": "test"}
        assert sm.verify_skill_signature(manifest, "sig", "") is False


# ============================================================================
# 2. 安装端点签名门禁
# ============================================================================


class TestInstallSignatureGate:
    @pytest.mark.asyncio
    async def test_install_mock_skips_signature(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
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

    @pytest.mark.asyncio
    async def test_install_local_skips_signature(self, monkeypatch):
        pkg = {
            "id": "pkg_local",
            "name": "local",
            "version": "1.0.0",
            "manifest_url": "local://artifact/abc123",
            "manifest": {"name": "local"},
            "signature": "",
            "public_key": "",
        }
        monkeypatch.setattr(sm, "_mock_packages", lambda: [pkg])
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market/pkg_local/install",
                json={"package_id": "pkg_local"},
            )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_install_signed_package_success(self, monkeypatch):
        priv, pub = _generate_keypair()
        manifest = {"name": "signed", "version": "1.0.0"}
        sig = _sign_manifest(priv, manifest)
        pkg = {
            "id": "pkg_signed",
            "name": "signed",
            "version": "1.0.0",
            "manifest_url": "https://example.com/signed",
            "manifest": manifest,
            "signature": sig,
            "public_key": pub,
        }
        monkeypatch.setattr(sm, "_mock_packages", lambda: [pkg])
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market/pkg_signed/install",
                json={"package_id": "pkg_signed"},
            )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_install_signed_package_bad_signature_returns_400(self, monkeypatch):
        _, pub = _generate_keypair()
        manifest = {"name": "signed", "version": "1.0.0"}
        pkg = {
            "id": "pkg_signed",
            "name": "signed",
            "version": "1.0.0",
            "manifest_url": "https://example.com/signed",
            "manifest": manifest,
            "signature": "invalidsig",
            "public_key": pub,
        }
        monkeypatch.setattr(sm, "_mock_packages", lambda: [pkg])
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(sm, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/skills/market/pkg_signed/install",
                json={"package_id": "pkg_signed"},
            )
        assert resp.status_code == 400
        assert "signature" in resp.json()["detail"].lower()
