"""Tests for the data_residency module.

覆盖：
- RegionRoutingMiddleware: 跳过非 /api/v1/ 路径 / 跳过 /api/v1/system/ /
  无 JWT 跳过 / residency_required 不匹配 403 / 合规放行 / 缓存生效
- require_region_compliance: 成功 / 403
- get_region_policy: 返回策略 + 合规状态
- check_region_compliance: 允许 / 拒绝
- log_cross_border_transfer: 写审计链 + 返回 transfer_id

所有测试使用 fake pool/connection + httpx ASGITransport，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, create_access_token, get_actor
from workama_platform.modules import data_residency as dr


# ============================================================================
# 测试辅助：fake pool / connection / result（复用 _RecordingConnection 模式）
# ============================================================================


class _Result:
    def __init__(self, row: dict[str, Any] | None = None, rows: list | None = None, rowcount: int = 0):
        self._row = row
        self._rows = rows if rows is not None else []
        self.rowcount = rowcount

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
    def __init__(self, results: list[_Result] | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
        self._idx = 0
        self.commits = 0

    def transaction(self):
        return _Transaction()

    async def commit(self):
        self.commits += 1

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()


class _Pool:
    def __init__(self, conn: _RecordingConnection):
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
    workspace_id: str = "wsp_test",
    user_id: str = "usr_test",
    role: str = "admin",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
    )


def _policy_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "reg_test",
        "org_id": "org_test",
        "workspace_id": "wsp_test",
        "home_region": "us-east-1",
        "allowed_regions": ["us-east-1", "us-west-2"],
        "provider_regions": ["us-east-1", "us-west-2"],
        "cross_border_mode": "deny",
        "residency_required": True,
        "version": 1,
        "status": "active",
    }
    base.update(overrides)
    return base


def _app_with_middleware(*, include_router: bool = True, actor: Actor | None = None) -> FastAPI:
    """Build a minimal FastAPI app wrapped with RegionRoutingMiddleware."""
    app = FastAPI()

    @app.get("/api/v1/system/healthz")
    async def _healthz():
        return {"status": "ok"}

    @app.get("/api/v1/data/test")
    async def _data_test():
        return {"ok": True}

    @app.get("/public/health")
    async def _public_health():
        return {"status": "ok"}

    if include_router:
        app.include_router(dr.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    # Wrap with the middleware under test.
    return dr.RegionRoutingMiddleware(app)


def _token(workspace_id: str = "wsp_test", role: str = "admin") -> str:
    return create_access_token("usr_test", workspace_id, role)


# ============================================================================
# 1. RegionRoutingMiddleware
# ============================================================================


class TestRegionRoutingMiddleware:
    @pytest.mark.asyncio
    async def test_skips_non_api_v1_path(self, monkeypatch):
        """非 /api/v1/ 路径直接放行（不走策略校验）。"""
        # 即使 pool 会抛错，也不应被触发（路径不匹配，提前放行）。
        conn = _RecordingConnection()
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        wrapped = _app_with_middleware()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
        ) as client:
            resp = await client.get("/public/health")

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        # 中间件不应查询 DB。
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_skips_system_path(self, monkeypatch):
        """/api/v1/system/ 健康检查路径直接放行。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        wrapped = _app_with_middleware()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/system/healthz",
                headers={"Authorization": f"Bearer {_token()}"},
            )

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_skips_when_no_jwt(self, monkeypatch):
        """无 Authorization header 的 /api/v1/ 请求直接放行（公开端点）。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        wrapped = _app_with_middleware()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/data/test")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_residency_required_mismatch_returns_403(self, monkeypatch):
        """residency_required=True 且请求来源区域 != home_region → 403。"""
        dr.invalidate_region_policy_cache("wsp_test")
        policy = _policy_row(home_region="us-east-1", residency_required=True)
        conn = _RecordingConnection(results=[_Result(row=policy)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        wrapped = _app_with_middleware()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/data/test",
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "X-WorkAMA-Region": "eu-west-1",
                },
            )

        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"] == "region not allowed"
        assert body["requested_region"] == "eu-west-1"
        assert body["home_region"] == "us-east-1"

    @pytest.mark.asyncio
    async def test_compliant_request_passes_through(self, monkeypatch):
        """请求来源区域 == home_region 且 residency_required=True → 放行。"""
        dr.invalidate_region_policy_cache("wsp_test")
        policy = _policy_row(home_region="us-east-1", residency_required=True)
        conn = _RecordingConnection(results=[_Result(row=policy)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        wrapped = _app_with_middleware()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/data/test",
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "X-WorkAMA-Region": "us-east-1",
                },
            )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_second_db_query(self, monkeypatch):
        """第二次请求命中缓存，不再查询 DB。"""
        dr.invalidate_region_policy_cache("wsp_test")
        policy = _policy_row(home_region="us-east-1", residency_required=True)
        conn = _RecordingConnection(results=[_Result(row=policy)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        wrapped = _app_with_middleware()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
        ) as client:
            r1 = await client.get(
                "/api/v1/data/test",
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "X-WorkAMA-Region": "us-east-1",
                },
            )
            r2 = await client.get(
                "/api/v1/data/test",
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "X-WorkAMA-Region": "us-east-1",
                },
            )

        assert r1.status_code == 200
        assert r2.status_code == 200
        # 只有第一次请求触发 DB 查询（1 次 execute）。
        assert len(conn.calls) == 1


# ============================================================================
# 2. require_region_compliance dependency
# ============================================================================


class TestRequireRegionCompliance:
    @pytest.mark.asyncio
    async def test_compliance_success_when_region_allowed(self, monkeypatch):
        """策略允许请求区域 → 返回 allowed=True。"""
        dr.invalidate_region_policy_cache("wsp_test")
        policy = _policy_row(home_region="us-east-1", residency_required=False)
        conn = _RecordingConnection(results=[_Result(row=policy)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = FastAPI()
        app.include_router(dr.router)
        app.dependency_overrides[get_actor] = lambda: _actor()

        # 用一个测试端点挂载依赖。
        from fastapi import Depends

        @app.get("/api/v1/_test_export")
        async def _export(
            compliance: dict = Depends(dr.require_region_compliance),
        ):
            return compliance

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/_test_export",
                params={"requested_region": "us-east-1"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["allowed"] is True
        assert body["requested_region"] == "us-east-1"

    @pytest.mark.asyncio
    async def test_compliance_403_when_region_denied(self, monkeypatch):
        """策略拒绝请求区域 → 403。"""
        dr.invalidate_region_policy_cache("wsp_test")
        policy = _policy_row(home_region="us-east-1", residency_required=True)
        conn = _RecordingConnection(results=[_Result(row=policy)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = FastAPI()
        app.include_router(dr.router)
        app.dependency_overrides[get_actor] = lambda: _actor()

        from fastapi import Depends

        @app.get("/api/v1/_test_export")
        async def _export(
            compliance: dict = Depends(dr.require_region_compliance),
        ):
            return compliance

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/_test_export",
                params={"requested_region": "eu-west-1"},
            )

        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"]["detail"] == "region not allowed"
        assert body["detail"]["requested_region"] == "eu-west-1"


# ============================================================================
# 3. get_region_policy endpoint
# ============================================================================


class TestGetRegionPolicy:
    @pytest.mark.asyncio
    async def test_returns_policy_and_compliance_status(self, monkeypatch):
        """GET /region-policy 返回策略 + 请求区域 + 合规状态。"""
        dr.invalidate_region_policy_cache("wsp_test")
        policy = _policy_row(home_region="us-east-1", residency_required=True)
        conn = _RecordingConnection(results=[_Result(row=policy)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = FastAPI()
        app.include_router(dr.router)
        app.dependency_overrides[get_actor] = lambda: _actor()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/enterprise/compliance/region-policy")

        assert resp.status_code == 200
        body = resp.json()
        assert body["policy"]["home_region"] == "us-east-1"
        assert body["status"] == "active"
        assert body["compliant"] is True
        assert "requested_region" in body
        assert "provider_region" in body

    @pytest.mark.asyncio
    async def test_returns_missing_when_no_policy(self, monkeypatch):
        """无策略时返回 status=missing 且 compliant=True。"""
        dr.invalidate_region_policy_cache("wsp_test")
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = FastAPI()
        app.include_router(dr.router)
        app.dependency_overrides[get_actor] = lambda: _actor()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/enterprise/compliance/region-policy")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "missing"
        assert body["policy"] is None
        assert body["compliant"] is True


# ============================================================================
# 4. check_region_compliance endpoint
# ============================================================================


class TestCheckRegionCompliance:
    @pytest.mark.asyncio
    async def test_check_allows_compliant_region(self, monkeypatch):
        """POST /region-policy/check 允许合规区域。"""
        dr.invalidate_region_policy_cache("wsp_test")
        policy = _policy_row(
            home_region="us-east-1",
            allowed_regions=["us-east-1", "us-west-2"],
            provider_regions=["us-east-1", "us-west-2"],
            residency_required=False,
            cross_border_mode="allowlist",
        )
        conn = _RecordingConnection(results=[_Result(row=policy)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = FastAPI()
        app.include_router(dr.router)
        app.dependency_overrides[get_actor] = lambda: _actor()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/enterprise/compliance/region-policy/check",
                json={"requested_region": "us-west-2", "provider_region": "us-west-2"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["allowed"] is True
        assert body["home_region"] == "us-east-1"

    @pytest.mark.asyncio
    async def test_check_denies_non_compliant_region(self, monkeypatch):
        """POST /region-policy/check 拒绝非合规区域。"""
        dr.invalidate_region_policy_cache("wsp_test")
        policy = _policy_row(
            home_region="us-east-1",
            allowed_regions=["us-east-1"],
            residency_required=True,
        )
        conn = _RecordingConnection(results=[_Result(row=policy)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = FastAPI()
        app.include_router(dr.router)
        app.dependency_overrides[get_actor] = lambda: _actor()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/enterprise/compliance/region-policy/check",
                json={"requested_region": "eu-west-1", "provider_region": "eu-west-1"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["allowed"] is False
        assert body["home_region"] == "us-east-1"


# ============================================================================
# 5. log_cross_border_transfer endpoint
# ============================================================================


class TestLogCrossBorderTransfer:
    @pytest.mark.asyncio
    async def test_writes_audit_chain_and_returns_transfer_id(self, monkeypatch):
        """POST /cross-border-transfer 写入审计链并返回 transfer_id。"""
        dr.invalidate_region_policy_cache("wsp_test")
        # append_audit_chain 执行 3 次 execute：
        # 1) pg_advisory_xact_lock → _Result() (fetchone=None 也可，返回值不用)
        # 2) SELECT sequence,record_hash → _Result(row=None) (首条记录 sequence=1)
        # 3) INSERT ... ON CONFLICT → _Result()
        conn = _RecordingConnection(
            results=[_Result(), _Result(row=None), _Result()]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = FastAPI()
        app.include_router(dr.router)
        app.dependency_overrides[get_actor] = lambda: _actor()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/enterprise/compliance/cross-border-transfer",
                json={
                    "destination_region": "eu-west-1",
                    "data_type": "user_export",
                    "data_volume_mb": 128,
                    "legal_basis": "gdpr_art_44",
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "recorded"
        assert body["transfer_id"].startswith("cbt_")

        # 验证审计链写入：至少有 INSERT INTO sec_audit_chain 的调用。
        insert_calls = [
            c for c in conn.calls if "INSERT INTO sec_audit_chain" in c[0]
        ]
        assert len(insert_calls) == 1
        # 参数中应包含 action 和 destination_region（出现在 details JSON 中）。
        params = insert_calls[0][1]
        assert "compliance.cross_border_transfer" in params  # action 字段
        # destination_region 序列化在 details JSON 字符串中，检查任意参数的子串。
        assert any("eu-west-1" in str(p) for p in params)


# ============================================================================
# P3: 海外区部署选项 + GDPR 数据本地化
# 测试覆盖：Region 枚举 / SCHEMA_STATEMENTS / 辅助函数 / compliance_router 端点
# 所有测试使用 fake connection 模式 mock PostgreSQL，不依赖真实 DB。
# ============================================================================


def _policy_p3_row(**overrides) -> dict[str, Any]:
    """data_residency_policy 表行（P3 版本，区别于 sec_region_policy）。"""
    base = {
        "workspace_id": "wsp_test",
        "region": "EU",
        "data_localization_enforced": True,
        "cross_region_allowed": False,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _dsar_row(**overrides) -> dict[str, Any]:
    """dsar_request 表行。"""
    base = {
        "id": "dsar_1",
        "workspace_id": "wsp_test",
        "user_id": "usr_test",
        "request_type": "access",
        "status": "pending",
        "payload": {},
        "result_url": None,
        "created_at": "2026-01-01T00:00:00Z",
        "completed_at": None,
    }
    base.update(overrides)
    return base


def _audit_row(**overrides) -> dict[str, Any]:
    """cross_region_access_audit 表行。"""
    base = {
        "id": "cra_1",
        "workspace_id": "wsp_test",
        "user_id": "usr_test",
        "region_from": "EU",
        "region_to": "US",
        "resource_type": "user_export",
        "resource_id": "res_1",
        "audit_reason": "gdpr_art_44",
        "created_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _compliance_app(actor: Actor | None = None) -> FastAPI:
    """Build a FastAPI app with compliance_router mounted (P3 端点）。"""
    app = FastAPI()
    app.include_router(dr.compliance_router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


def _audit_chain_results() -> list[_Result]:
    """3 个 _Result，对应 append_audit_chain 的 3 次 execute（lock/select/insert）。"""
    return [_Result(), _Result(row=None), _Result()]


def _internal_token() -> str:
    """获取默认内部 token（与 settings.internal_token 一致）。"""
    return str(dr.settings.internal_token)


# ============================================================================
# 6. Region 常量与元数据
# ============================================================================


class TestRegionConstants:
    def test_region_enum_has_four_values(self):
        """Region 枚举包含 CN/EU/US/SG 四个值。"""
        assert dr.Region.CN.value == "CN"
        assert dr.Region.EU.value == "EU"
        assert dr.Region.US.value == "US"
        assert dr.Region.SG.value == "SG"

    def test_region_regulation_has_all_regions(self):
        """REGION_REGULATION 包含所有 4 个区域的元数据。"""
        for region in dr.Region:
            assert region.value in dr.REGION_REGULATION

    def test_cn_region_no_cross_border_default(self):
        """CN 区 cross_border_default=False（PIPL 数据不得跨境）。"""
        meta = dr.REGION_REGULATION[dr.Region.CN.value]
        assert meta["cross_border_default"] is False
        assert meta["localization_default"] is True
        assert meta["regulation"] == "PIPL"

    def test_eu_region_gdpr(self):
        """EU 区适用 GDPR。"""
        meta = dr.REGION_REGULATION[dr.Region.EU.value]
        assert meta["regulation"] == "GDPR"

    def test_us_region_ccpa(self):
        """US 区适用 CCPA。"""
        meta = dr.REGION_REGULATION[dr.Region.US.value]
        assert meta["regulation"] == "CCPA"
        assert meta["localization_default"] is False

    def test_sg_region_pdpa(self):
        """SG 区适用 PDPA。"""
        meta = dr.REGION_REGULATION[dr.Region.SG.value]
        assert meta["regulation"] == "PDPA"

    def test_valid_regions_matches_enum(self):
        """VALID_REGIONS 与 Region 枚举值一致。"""
        assert dr.VALID_REGIONS == frozenset({r.value for r in dr.Region})

    def test_erasure_propagation_tables_count(self):
        """删除传播矩阵包含 5 张表。"""
        assert len(dr.ERASURE_PROPAGATION_TABLES) == 5

    def test_erasure_propagation_tables_format(self):
        """删除传播矩阵每项为 (table_name, user_column) 二元组。"""
        for entry in dr.ERASURE_PROPAGATION_TABLES:
            assert isinstance(entry, tuple)
            assert len(entry) == 2
            assert isinstance(entry[0], str)
            assert isinstance(entry[1], str)


# ============================================================================
# 7. ensure_data_residency_schema
# ============================================================================


class TestEnsureDataResidencySchema:
    @pytest.mark.asyncio
    async def test_executes_all_statements(self):
        """ensure_data_residency_schema 执行所有 SCHEMA_STATEMENTS。"""
        conn = _RecordingConnection()
        await dr.ensure_data_residency_schema(conn)
        assert len(conn.calls) == len(dr.SCHEMA_STATEMENTS)

    @pytest.mark.asyncio
    async def test_idempotent_multiple_calls(self):
        """多次调用 ensure_data_residency_schema 幂等（每次执行相同语句）。"""
        conn = _RecordingConnection()
        await dr.ensure_data_residency_schema(conn)
        await dr.ensure_data_residency_schema(conn)
        assert len(conn.calls) == len(dr.SCHEMA_STATEMENTS) * 2


# ============================================================================
# 8. get_workspace_region 辅助函数
# ============================================================================


class TestGetWorkspaceRegion:
    @pytest.mark.asyncio
    async def test_returns_bound_region(self):
        """workspace 已绑定 region 时返回绑定值。"""
        conn = _RecordingConnection(results=[_Result(row={"region": "EU"})])
        region = await dr.get_workspace_region(conn, "wsp_test")
        assert region == "EU"

    @pytest.mark.asyncio
    async def test_returns_default_when_region_null(self):
        """region 字段为 NULL 时返回 settings.default_region。"""
        conn = _RecordingConnection(results=[_Result(row={"region": None})])
        region = await dr.get_workspace_region(conn, "wsp_test")
        assert region == str(dr.settings.default_region)

    @pytest.mark.asyncio
    async def test_returns_default_when_row_missing(self):
        """workspace 行不存在时返回 settings.default_region。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        region = await dr.get_workspace_region(conn, "wsp_test")
        assert region == str(dr.settings.default_region)

    @pytest.mark.asyncio
    async def test_returns_CN_fallback(self):
        """settings.default_region 默认为 CN。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        region = await dr.get_workspace_region(conn, "wsp_test")
        assert region == "CN"


# ============================================================================
# 9. assert_data_localization 辅助函数
# ============================================================================


class TestAssertDataLocalization:
    @pytest.mark.asyncio
    async def test_same_region_allowed(self):
        """访问相同区域 → True（不查策略表）。"""
        conn = _RecordingConnection(results=[_Result(row={"region": "EU"})])
        assert await dr.assert_data_localization(conn, "wsp_test", "EU") is True
        assert len(conn.calls) == 1  # 只查 id_workspace

    @pytest.mark.asyncio
    async def test_cn_cross_border_denied(self):
        """CN 区跨域访问 → False（PIPL 禁止跨境）。"""
        conn = _RecordingConnection(results=[_Result(row={"region": "CN"})])
        assert await dr.assert_data_localization(conn, "wsp_test", "US") is False
        assert len(conn.calls) == 1  # 只查 id_workspace，不查策略表

    @pytest.mark.asyncio
    async def test_eu_no_policy_uses_region_default(self):
        """EU 区无策略时使用 REGION_REGULATION 默认值（cross_border_default=True）。"""
        conn = _RecordingConnection(
            results=[_Result(row={"region": "EU"}), _Result(row=None)]
        )
        assert await dr.assert_data_localization(conn, "wsp_test", "US") is True

    @pytest.mark.asyncio
    async def test_us_no_policy_uses_region_default(self):
        """US 区无策略时使用 REGION_REGULATION 默认值。"""
        conn = _RecordingConnection(
            results=[_Result(row={"region": "US"}), _Result(row=None)]
        )
        assert await dr.assert_data_localization(conn, "wsp_test", "EU") is True

    @pytest.mark.asyncio
    async def test_policy_enforced_denies_cross_region(self):
        """策略 data_localization_enforced=True 时拒绝跨域。"""
        conn = _RecordingConnection(
            results=[
                _Result(row={"region": "EU"}),
                _Result(row={"cross_region_allowed": False, "data_localization_enforced": True}),
            ]
        )
        assert await dr.assert_data_localization(conn, "wsp_test", "US") is False

    @pytest.mark.asyncio
    async def test_policy_cross_allowed_permits(self):
        """策略 cross_region_allowed=True 且 localization 未强制 → 允许跨域。"""
        conn = _RecordingConnection(
            results=[
                _Result(row={"region": "EU"}),
                _Result(row={"cross_region_allowed": True, "data_localization_enforced": False}),
            ]
        )
        assert await dr.assert_data_localization(conn, "wsp_test", "US") is True


# ============================================================================
# 10. trigger_erasure_propagation 辅助函数
# ============================================================================


class TestTriggerErasurePropagation:
    @pytest.mark.asyncio
    async def test_deletes_from_all_tables(self):
        """删除传播对所有 5 张表执行 DELETE。"""
        results = [_Result(rowcount=n) for n in [3, 5, 2, 1, 0]]
        conn = _RecordingConnection(results=results)
        summary = await dr.trigger_erasure_propagation(conn, "wsp_test", "usr_target")
        assert len(summary["deleted_counts"]) == 5
        assert summary["total_tables"] == 5

    @pytest.mark.asyncio
    async def test_returns_correct_counts(self):
        """删除传播返回每表正确的删除行数。"""
        results = [_Result(rowcount=n) for n in [3, 5, 2, 1, 0]]
        conn = _RecordingConnection(results=results)
        summary = await dr.trigger_erasure_propagation(conn, "wsp_test", "usr_target")
        counts = summary["deleted_counts"]
        assert counts["ag_session"] == 3
        assert counts["ag_memory"] == 5
        assert counts["id_notification"] == 2
        assert counts["ag_approval"] == 1
        assert counts["ops_product_event"] == 0

    @pytest.mark.asyncio
    async def test_zero_counts_when_no_rows(self):
        """无数据时所有表删除行数为 0。"""
        results = [_Result(rowcount=0) for _ in range(5)]
        conn = _RecordingConnection(results=results)
        summary = await dr.trigger_erasure_propagation(conn, "wsp_test", "usr_target")
        assert all(v == 0 for v in summary["deleted_counts"].values())

    @pytest.mark.asyncio
    async def test_query_params_include_workspace_and_user(self):
        """DELETE 查询参数包含 workspace_id 和 user_id。"""
        conn = _RecordingConnection(results=[_Result(rowcount=0) for _ in range(5)])
        await dr.trigger_erasure_propagation(conn, "wsp_a", "usr_b")
        for query, params in conn.calls:
            assert "wsp_a" in params
            assert "usr_b" in params


# ============================================================================
# 11. _build_user_data_inventory 辅助函数
# ============================================================================


class TestBuildUserDataInventory:
    @pytest.mark.asyncio
    async def test_counts_per_table(self):
        """库存统计返回每表正确的数据量。"""
        results = [_Result(row={"cnt": n}) for n in [3, 5, 2, 1, 0]]
        conn = _RecordingConnection(results=results)
        inventory = await dr._build_user_data_inventory(conn, "wsp_test", "usr_target")
        assert inventory["ag_session"] == 3
        assert inventory["ag_memory"] == 5
        assert inventory["ops_product_event"] == 0

    @pytest.mark.asyncio
    async def test_zero_when_no_data(self):
        """无数据时所有表计数为 0。"""
        results = [_Result(row={"cnt": 0}) for _ in range(5)]
        conn = _RecordingConnection(results=results)
        inventory = await dr._build_user_data_inventory(conn, "wsp_test", "usr_target")
        assert all(v == 0 for v in inventory.values())


# ============================================================================
# 12. GET /api/v1/compliance/region
# ============================================================================


class TestGetWorkspaceDataResidency:
    @pytest.mark.asyncio
    async def test_returns_configured_policy(self, monkeypatch):
        """已配置策略时返回策略详情。"""
        policy = _policy_p3_row(region="EU", cross_region_allowed=True)
        conn = _RecordingConnection(results=[_Result(row=policy)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/region")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "configured"
        assert body["region"] == "EU"
        assert body["cross_region_allowed"] is True

    @pytest.mark.asyncio
    async def test_returns_default_when_no_policy(self, monkeypatch):
        """无策略时返回区域默认值。"""
        conn = _RecordingConnection(
            results=[_Result(row=None), _Result(row={"region": "EU"})]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/region")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "default"
        assert body["region"] == "EU"
        assert body["regulation"] == "GDPR"

    @pytest.mark.asyncio
    async def test_workspace_isolation(self, monkeypatch):
        """查询参数包含 actor.workspace_id（workspace 隔离）。"""
        policy = _policy_p3_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=policy)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(workspace_id="wsp_other"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/api/v1/compliance/region")

        # 验证 SELECT 查询参数包含正确的 workspace_id
        select_calls = [c for c in conn.calls if "SELECT" in c[0] and "data_residency_policy" in c[0]]
        assert len(select_calls) >= 1
        assert "wsp_other" in select_calls[0][1]


# ============================================================================
# 13. PATCH /api/v1/compliance/region
# ============================================================================


class TestUpdateWorkspaceDataResidency:
    @pytest.mark.asyncio
    async def test_owner_updates_existing(self, monkeypatch):
        """owner 更新已存在策略的 cross_region_allowed。"""
        dr.invalidate_region_policy_cache("wsp_test")
        existing = _policy_p3_row(cross_region_allowed=False)
        updated = _policy_p3_row(cross_region_allowed=True)
        conn = _RecordingConnection(results=[_Result(row=existing), _Result(row=updated)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="owner"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/compliance/region", json={"cross_region_allowed": True}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["cross_region_allowed"] is True
        assert conn.commits == 1

    @pytest.mark.asyncio
    async def test_owner_creates_new_policy(self, monkeypatch):
        """owner 为无策略 workspace 创建新策略。"""
        dr.invalidate_region_policy_cache("wsp_test")
        new_policy = _policy_p3_row(region="EU", cross_region_allowed=True)
        conn = _RecordingConnection(
            results=[
                _Result(row=None),  # SELECT existing → none
                _Result(row={"region": "EU"}),  # get_workspace_region
                _Result(row=new_policy),  # INSERT RETURNING *
            ]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="owner"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/compliance/region", json={"cross_region_allowed": True}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["region"] == "EU"
        assert body["cross_region_allowed"] is True

    @pytest.mark.asyncio
    async def test_non_owner_403(self, monkeypatch):
        """非 owner 角色 → 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/compliance/region", json={"cross_region_allowed": True}
            )

        assert resp.status_code == 403
        assert len(conn.calls) == 0  # 不应访问 DB

    @pytest.mark.asyncio
    async def test_member_403(self, monkeypatch):
        """member 角色 → 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/compliance/region", json={"cross_region_allowed": True}
            )

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_invalid_region_422(self, monkeypatch):
        """workspace 绑定的 region 不在 VALID_REGIONS 中 → 422。"""
        dr.invalidate_region_policy_cache("wsp_test")
        conn = _RecordingConnection(
            results=[
                _Result(row=None),  # SELECT existing → none
                _Result(row={"region": "INVALID"}),  # get_workspace_region
            ]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="owner"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/compliance/region", json={"cross_region_allowed": True}
            )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalidates_cache(self, monkeypatch):
        """更新策略后调用 invalidate_region_policy_cache。"""
        dr.invalidate_region_policy_cache("wsp_test")
        dr._cache_set("wsp_test", {"some": "policy"})
        existing = _policy_p3_row()
        updated = _policy_p3_row(cross_region_allowed=True)
        conn = _RecordingConnection(results=[_Result(row=existing), _Result(row=updated)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="owner"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/compliance/region", json={"cross_region_allowed": True}
            )

        assert resp.status_code == 200
        # 缓存应被清除
        policy, hit = dr._cache_get("wsp_test")
        assert hit is False


# ============================================================================
# 14. POST /api/v1/compliance/dsar
# ============================================================================


class TestCreateDsarRequest:
    @pytest.mark.asyncio
    async def test_create_access_request(self, monkeypatch):
        """创建 access 类型 DSAR 请求。"""
        dsar = _dsar_row(request_type="access", status="pending")
        conn = _RecordingConnection(
            results=[_Result(), *_audit_chain_results(), _Result(row=dsar)]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/compliance/dsar", json={"request_type": "access"}
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["request_type"] == "access"
        assert body["status"] == "pending"

    @pytest.mark.asyncio
    async def test_create_erasure_request(self, monkeypatch):
        """创建 erasure 类型 DSAR 请求。"""
        dsar = _dsar_row(request_type="erasure")
        conn = _RecordingConnection(
            results=[_Result(), *_audit_chain_results(), _Result(row=dsar)]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/compliance/dsar", json={"request_type": "erasure"}
            )

        assert resp.status_code == 201
        assert resp.json()["request_type"] == "erasure"

    @pytest.mark.asyncio
    async def test_create_portability_request(self, monkeypatch):
        """创建 portability 类型 DSAR 请求。"""
        dsar = _dsar_row(request_type="portability")
        conn = _RecordingConnection(
            results=[_Result(), *_audit_chain_results(), _Result(row=dsar)]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/compliance/dsar", json={"request_type": "portability"}
            )

        assert resp.status_code == 201
        assert resp.json()["request_type"] == "portability"

    @pytest.mark.asyncio
    async def test_invalid_request_type_422(self, monkeypatch):
        """无效 request_type → 422（Pydantic 校验）。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/compliance/dsar", json={"request_type": "invalid"}
            )

        assert resp.status_code == 422
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_writes_audit_chain(self, monkeypatch):
        """创建 DSAR 时写入审计链。"""
        dsar = _dsar_row()
        conn = _RecordingConnection(
            results=[_Result(), *_audit_chain_results(), _Result(row=dsar)]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/v1/compliance/dsar", json={"request_type": "access"})

        insert_calls = [c for c in conn.calls if "INSERT INTO sec_audit_chain" in c[0]]
        assert len(insert_calls) == 1
        assert "compliance.dsar.created" in insert_calls[0][1]


# ============================================================================
# 15. GET /api/v1/compliance/dsar
# ============================================================================


class TestListDsarRequests:
    @pytest.mark.asyncio
    async def test_admin_sees_all_workspace_requests(self, monkeypatch):
        """admin 可看 workspace 全部 DSAR 请求。"""
        rows = [_dsar_row(id="dsar_1", user_id="usr_a"), _dsar_row(id="dsar_2", user_id="usr_b")]
        conn = _RecordingConnection(
            results=[_Result(rows=rows), _Result(row={"total": 2})]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin", user_id="usr_admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/dsar")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_member_sees_own_only(self, monkeypatch):
        """member 只能看自己的 DSAR 请求。"""
        rows = [_dsar_row(id="dsar_1", user_id="usr_test")]
        conn = _RecordingConnection(
            results=[_Result(rows=rows), _Result(row={"total": 1})]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="member", user_id="usr_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/dsar")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        # 验证 SQL 包含 user_id 过滤
        select_calls = [c for c in conn.calls if "SELECT" in c[0] and "dsar_request" in c[0] and "COUNT" not in c[0]]
        assert any("usr_test" in c[1] for c in select_calls)

    @pytest.mark.asyncio
    async def test_pagination(self, monkeypatch):
        """分页参数 limit/offset 传递到 SQL。"""
        conn = _RecordingConnection(
            results=[_Result(rows=[]), _Result(row={"total": 0})]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/dsar?limit=10&offset=20")

        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 10
        assert body["offset"] == 20

    @pytest.mark.asyncio
    async def test_workspace_isolation(self, monkeypatch):
        """列表查询包含 workspace_id 过滤。"""
        conn = _RecordingConnection(
            results=[_Result(rows=[]), _Result(row={"total": 0})]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(workspace_id="wsp_iso"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/dsar")

        assert resp.status_code == 200
        for query, params in conn.calls:
            if "dsar_request" in query:
                assert "wsp_iso" in params

    @pytest.mark.asyncio
    async def test_invalid_limit_422(self, monkeypatch):
        """limit 超出范围 → 422。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/dsar?limit=999")

        assert resp.status_code == 422


# ============================================================================
# 16. GET /api/v1/compliance/dsar/{request_id}
# ============================================================================


class TestGetDsarRequest:
    @pytest.mark.asyncio
    async def test_found(self, monkeypatch):
        """查询存在的 DSAR 请求。"""
        dsar = _dsar_row(id="dsar_1")
        conn = _RecordingConnection(results=[_Result(row=dsar)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/dsar/dsar_1")

        assert resp.status_code == 200
        assert resp.json()["id"] == "dsar_1"

    @pytest.mark.asyncio
    async def test_not_found_404(self, monkeypatch):
        """查询不存在的 DSAR 请求 → 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/dsar/dsar_404")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_cross_user_404_for_member(self, monkeypatch):
        """member 查看他人 DSAR → 404（不泄露存在性）。"""
        dsar = _dsar_row(id="dsar_1", user_id="usr_other")
        conn = _RecordingConnection(results=[_Result(row=dsar)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="member", user_id="usr_self"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/dsar/dsar_1")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_admin_sees_other_users(self, monkeypatch):
        """admin 可查看 workspace 内任何用户的 DSAR。"""
        dsar = _dsar_row(id="dsar_1", user_id="usr_other")
        conn = _RecordingConnection(results=[_Result(row=dsar)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin", user_id="usr_admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/dsar/dsar_1")

        assert resp.status_code == 200
        assert resp.json()["user_id"] == "usr_other"

    @pytest.mark.asyncio
    async def test_workspace_isolation(self, monkeypatch):
        """查询参数包含 workspace_id（隔离）。"""
        dsar = _dsar_row(id="dsar_1", workspace_id="wsp_iso")
        conn = _RecordingConnection(results=[_Result(row=dsar)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(workspace_id="wsp_iso"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/dsar/dsar_1")

        assert resp.status_code == 200
        select_calls = [c for c in conn.calls if "SELECT" in c[0] and "dsar_request" in c[0]]
        assert "wsp_iso" in select_calls[0][1]


# ============================================================================
# 17. POST /api/v1/compliance/dsar/{request_id}/process
# ============================================================================


class TestProcessDsarRequest:
    @pytest.mark.asyncio
    async def test_process_erasure(self, monkeypatch):
        """处理 erasure 请求：触发删除传播。"""
        dsar = _dsar_row(id="dsar_1", request_type="erasure", status="pending", user_id="usr_target")
        final = _dsar_row(id="dsar_1", request_type="erasure", status="completed", result_url=None)
        # SELECT FOR UPDATE, UPDATE processing, 5x DELETE, UPDATE completed, 3x audit, SELECT final
        results = [
            _Result(row=dsar),
            _Result(),
            *[_Result(rowcount=n) for n in [3, 5, 2, 1, 0]],
            _Result(),
            *_audit_chain_results(),
            _Result(row=final),
        ]
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/compliance/dsar/dsar_1/process")

        assert resp.status_code == 200
        body = resp.json()
        assert body["request"]["status"] == "completed"
        assert "propagation" in body["summary"]
        assert body["summary"]["propagation"]["total_tables"] == 5

    @pytest.mark.asyncio
    async def test_process_access(self, monkeypatch):
        """处理 access 请求：生成用户数据清单。"""
        dsar = _dsar_row(id="dsar_1", request_type="access", status="pending", user_id="usr_target")
        final = _dsar_row(id="dsar_1", request_type="access", status="completed",
                          result_url="workama://dsar/dsar_1/access-manifest")
        results = [
            _Result(row=dsar),
            _Result(),
            *[_Result(row={"cnt": n}) for n in [3, 5, 2, 1, 0]],
            _Result(),
            *_audit_chain_results(),
            _Result(row=final),
        ]
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/compliance/dsar/dsar_1/process")

        assert resp.status_code == 200
        body = resp.json()
        assert body["request"]["status"] == "completed"
        assert "inventory" in body["summary"]
        assert body["summary"]["inventory"]["ag_session"] == 3

    @pytest.mark.asyncio
    async def test_process_portability(self, monkeypatch):
        """处理 portability 请求：生成 JSON 导出 URL。"""
        dsar = _dsar_row(id="dsar_1", request_type="portability", status="pending")
        final = _dsar_row(id="dsar_1", request_type="portability", status="completed",
                          result_url="workama://dsar/dsar_1/export.json")
        results = [
            _Result(row=dsar),
            _Result(),
            _Result(),
            *_audit_chain_results(),
            _Result(row=final),
        ]
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/compliance/dsar/dsar_1/process")

        assert resp.status_code == 200
        body = resp.json()
        assert body["request"]["status"] == "completed"
        assert body["summary"]["format"] == "json"

    @pytest.mark.asyncio
    async def test_not_found_404(self, monkeypatch):
        """处理不存在的 DSAR → 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/compliance/dsar/dsar_404/process")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_already_completed_409(self, monkeypatch):
        """处理已完成的 DSAR → 409。"""
        dsar = _dsar_row(id="dsar_1", status="completed")
        conn = _RecordingConnection(results=[_Result(row=dsar)])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/compliance/dsar/dsar_1/process")

        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_non_admin_403(self, monkeypatch):
        """非 admin 角色 → 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/compliance/dsar/dsar_1/process")

        assert resp.status_code == 403
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_workspace_isolation(self, monkeypatch):
        """处理查询包含 workspace_id（隔离）。"""
        dsar = _dsar_row(id="dsar_1", request_type="portability", status="pending", workspace_id="wsp_iso")
        final = _dsar_row(id="dsar_1", status="completed", workspace_id="wsp_iso")
        results = [
            _Result(row=dsar),
            _Result(),
            _Result(),
            *_audit_chain_results(),
            _Result(row=final),
        ]
        conn = _RecordingConnection(results=results)
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin", workspace_id="wsp_iso"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/compliance/dsar/dsar_1/process")

        assert resp.status_code == 200
        select_calls = [c for c in conn.calls if "SELECT" in c[0] and "dsar_request" in c[0] and "FOR UPDATE" in c[0]]
        assert "wsp_iso" in select_calls[0][1]


# ============================================================================
# 18. GET /api/v1/compliance/cross-region-audit
# ============================================================================


class TestListCrossRegionAudit:
    @pytest.mark.asyncio
    async def test_admin_sees_audit_list(self, monkeypatch):
        """admin 可查看跨区域审计列表。"""
        rows = [_audit_row(id="cra_1"), _audit_row(id="cra_2")]
        conn = _RecordingConnection(
            results=[_Result(rows=rows), _Result(row={"total": 2})]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/cross-region-audit")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_pagination(self, monkeypatch):
        """分页参数传递。"""
        conn = _RecordingConnection(
            results=[_Result(rows=[]), _Result(row={"total": 0})]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/cross-region-audit?limit=5&offset=10")

        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 5
        assert body["offset"] == 10

    @pytest.mark.asyncio
    async def test_workspace_isolation(self, monkeypatch):
        """查询参数包含 workspace_id（隔离）。"""
        conn = _RecordingConnection(
            results=[_Result(rows=[]), _Result(row={"total": 0})]
        )
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="admin", workspace_id="wsp_iso"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/cross-region-audit")

        assert resp.status_code == 200
        for query, params in conn.calls:
            if "cross_region_access_audit" in query:
                assert "wsp_iso" in params

    @pytest.mark.asyncio
    async def test_non_admin_403(self, monkeypatch):
        """非 admin 角色 → 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app(_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/compliance/cross-region-audit")

        assert resp.status_code == 403
        assert len(conn.calls) == 0


# ============================================================================
# 19. POST /api/v1/compliance/cross-region-audit
# ============================================================================


class TestRecordCrossRegionAudit:
    @pytest.mark.asyncio
    async def test_success(self, monkeypatch):
        """内部 API 成功记录跨区域访问。"""
        conn = _RecordingConnection(results=[_Result()])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/compliance/cross-region-audit",
                json={
                    "workspace_id": "wsp_test",
                    "user_id": "usr_test",
                    "region_from": "EU",
                    "region_to": "US",
                    "resource_type": "user_export",
                    "resource_id": "res_1",
                    "audit_reason": "gdpr_art_44",
                },
                headers={"X-Internal-Token": _internal_token()},
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "recorded"
        assert body["audit_id"].startswith("cra_")
        assert conn.commits == 1

    @pytest.mark.asyncio
    async def test_no_token_401(self, monkeypatch):
        """无 X-Internal-Token → 401。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/compliance/cross-region-audit",
                json={
                    "workspace_id": "wsp_test",
                    "region_from": "EU",
                    "region_to": "US",
                    "resource_type": "user_export",
                    "resource_id": "res_1",
                },
            )

        assert resp.status_code == 401
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_wrong_token_401(self, monkeypatch):
        """错误的 X-Internal-Token → 401。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/compliance/cross-region-audit",
                json={
                    "workspace_id": "wsp_test",
                    "region_from": "EU",
                    "region_to": "US",
                    "resource_type": "user_export",
                    "resource_id": "res_1",
                },
                headers={"X-Internal-Token": "wrong-token"},
            )

        assert resp.status_code == 401
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_inserts_correct_fields(self, monkeypatch):
        """INSERT 包含所有字段。"""
        conn = _RecordingConnection(results=[_Result()])
        monkeypatch.setattr(dr, "pool", _Pool(conn))

        app = _compliance_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/compliance/cross-region-audit",
                json={
                    "workspace_id": "wsp_test",
                    "user_id": "usr_test",
                    "region_from": "EU",
                    "region_to": "US",
                    "resource_type": "user_export",
                    "resource_id": "res_1",
                    "audit_reason": "gdpr_art_44",
                },
                headers={"X-Internal-Token": _internal_token()},
            )

        assert resp.status_code == 201
        insert_calls = [c for c in conn.calls if "INSERT INTO cross_region_access_audit" in c[0]]
        assert len(insert_calls) == 1
        params = insert_calls[0][1]
        assert "wsp_test" in params
        assert "EU" in params
        assert "US" in params
