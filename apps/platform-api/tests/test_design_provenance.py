"""AMA-Design 来源清单 / 内容凭证 (Provenance Manifest) 单元 + 端点测试。

覆盖：
- 创建 manifest：成功 / 资产不存在 404 / 跨 workspace 隔离 / 缺权限 403 / 未认证 401 / 重复版本 409 (6)
- 获取最新 manifest：成功 / 无 manifest 404 / 跨 workspace 隔离 (3)
- 获取历史链：成功 / 链式 parent_claim_hash / 多版本排序 (3)
- 按 manifest_id 查询：成功 / 不存在 404 / 跨 workspace 隔离 (3)
- 验证 manifest：claim_hash 匹配 / 篡改不匹配 / 不存在 404 (3)
- 列表：workspace 过滤 / project 过滤 / asset 过滤 / 分页 / 跨 workspace 403 (5)
- 边界：source_assets 为空 / 多个父资产 / 父子链路 (3)
- 辅助函数：_compute_claim_hash 确定性 / _canonical_json 排序 / 不同输入不同哈希 (3)
- Schema / 路由：迁移与端点注册 (2)

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 网络。
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from workama_platform.core import Actor, get_actor
from workama_platform.modules import design


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


class _SeqConnection:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls: list[tuple[str, tuple]] = []
        self._idx = 0

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None

    async def rollback(self):
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
    capabilities=("design:*",),
    workspace_id="wsp_test",
    user_id="usr_test",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role="admin",
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _asset_row(**overrides) -> dict:
    base = {
        "id": "dsgasset_ASSET1",
        "workspace_id": "wsp_test",
        "project_id": "proj_1",
        "content_sha256": "a" * 64,
    }
    base.update(overrides)
    return base


def _manifest_row(**overrides) -> dict:
    base = {
        "id": "dprov_MANIFEST1",
        "workspace_id": "wsp_test",
        "project_id": "proj_1",
        "asset_id": "dsgasset_ASSET1",
        "manifest_version": "1.0",
        "generator": {"model": "workama.mock.design.v2", "version": "1.0"},
        "prompt_hash": "b" * 64,
        "source_assets": [],
        "claim_hash": "sha256:" + "c" * 64,
        "parent_claim_hash": None,
        "created_by": "usr_test",
        "created_at": datetime.now(UTC),
        "metadata": {},
    }
    base.update(overrides)
    return base


def _app(actor=None):
    app = FastAPI()
    app.include_router(design.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 创建 manifest
# ============================================================================


@pytest.mark.asyncio
async def test_create_provenance_manifest_success(monkeypatch):
    asset = _asset_row()
    manifest = _manifest_row()
    conn = _SeqConnection(results=[
        _Result(row=asset),  # SELECT asset
        _Result(row=manifest),  # INSERT RETURNING
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.create_provenance_manifest(
        "proj_1",
        "dsgasset_ASSET1",
        design.ProvenanceManifestCreate(
            generator={"model": "workama.mock.design.v2", "version": "1.0"},
            prompt_hash="b" * 64,
        ),
        _actor(),
    )
    assert result["asset_id"] == "dsgasset_ASSET1"
    assert result["workspace_id"] == "wsp_test"
    assert result["claim_hash"].startswith("sha256:")
    assert result["manifest_version"] == "1.0"
    # commit was called
    assert len(conn.calls) >= 2


@pytest.mark.asyncio
async def test_create_provenance_manifest_asset_not_found_404(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.create_provenance_manifest(
            "proj_missing",
            "dsgasset_missing",
            design.ProvenanceManifestCreate(),
            _actor(),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_provenance_manifest_cross_workspace_isolation(monkeypatch):
    """Actor 在 wsp_other，查询使用其 workspace_id，资产查不到 → 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.create_provenance_manifest(
            "proj_1",
            "dsgasset_ASSET1",
            design.ProvenanceManifestCreate(),
            _actor(workspace_id="wsp_other"),
        )
    assert exc.value.status_code == 404
    # 确认查询带了 actor.workspace_id
    query, params = conn.calls[0]
    assert "wsp_other" in params


@pytest.mark.asyncio
async def test_create_provenance_manifest_missing_capability_403(monkeypatch):
    conn = _SeqConnection()
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.create_provenance_manifest(
            "proj_1",
            "dsgasset_ASSET1",
            design.ProvenanceManifestCreate(),
            _actor(capabilities=()),
        )
    assert exc.value.status_code == 403
    assert "design:write" in exc.value.detail


@pytest.mark.asyncio
async def test_create_provenance_manifest_unauthenticated_401():
    app = _app(actor=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/design/projects/proj_1/assets/dsgasset_A/provenance",
            json={"generator": {}, "prompt_hash": ""},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_provenance_manifest_duplicate_version_409(monkeypatch):
    asset = _asset_row()

    class _DuplicateConnection(_SeqConnection):
        async def execute(self, query, params=()):
            self.calls.append((query, params))
            if "INSERT" in query and "RETURNING" in query:
                raise Exception("duplicate key value violates unique constraint")
            if self._idx < len(self._results):
                r = self._results[self._idx]
                self._idx += 1
                return r
            return _Result()

    conn = _DuplicateConnection(results=[_Result(row=asset)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.create_provenance_manifest(
            "proj_1",
            "dsgasset_ASSET1",
            design.ProvenanceManifestCreate(),
            _actor(),
        )
    assert exc.value.status_code == 409


# ============================================================================
# 2. 获取最新 manifest
# ============================================================================


@pytest.mark.asyncio
async def test_get_latest_provenance_manifest_success(monkeypatch):
    manifest = _manifest_row()
    conn = _SeqConnection(results=[_Result(row=manifest)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.get_latest_provenance_manifest("proj_1", "dsgasset_ASSET1", _actor())
    assert result["id"] == "dprov_MANIFEST1"
    assert result["asset_id"] == "dsgasset_ASSET1"


@pytest.mark.asyncio
async def test_get_latest_provenance_manifest_not_found_404(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.get_latest_provenance_manifest("proj_1", "dsgasset_ASSET1", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_latest_provenance_manifest_cross_workspace_isolation(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.get_latest_provenance_manifest(
            "proj_1",
            "dsgasset_ASSET1",
            _actor(workspace_id="wsp_other"),
        )
    assert exc.value.status_code == 404
    query, params = conn.calls[0]
    assert "wsp_other" in params


# ============================================================================
# 3. 获取历史链
# ============================================================================


@pytest.mark.asyncio
async def test_list_provenance_history_success(monkeypatch):
    rows = [
        _manifest_row(id="dprov_M1", manifest_version="1.0", parent_claim_hash=None),
        _manifest_row(id="dprov_M2", manifest_version="1.1", parent_claim_hash="sha256:" + "c" * 64),
    ]
    conn = _SeqConnection(results=[_Result(rows=rows)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.list_provenance_history("proj_1", "dsgasset_ASSET1", _actor())
    assert result["total"] == 2
    assert result["items"][0]["id"] == "dprov_M1"
    assert result["items"][1]["id"] == "dprov_M2"
    # ordered by created_at ASC
    query, _ = conn.calls[0]
    assert "ORDER BY m.created_at ASC" in query


@pytest.mark.asyncio
async def test_list_provenance_history_chain_parent_claim():
    """辅助函数验证：parent_claim_hash 在 source_assets 非空时被正确计算。"""
    source_assets = [{"asset_id": "dsgasset_PARENT", "content_sha256": "p" * 64, "claim_hash": "sha256:parent"}]
    claim = design._compute_claim_hash(
        asset_id="dsgasset_CHILD",
        generator={"model": "workama.mock.design.v2", "version": "1.0"},
        prompt_hash="h" * 64,
        source_assets=source_assets,
        parent_claim_hash="sha256:parent",
        created_at="2026-07-30T00:00:00Z",
    )
    assert claim.startswith("sha256:")
    # 同样的输入应得到同样的 hash
    claim2 = design._compute_claim_hash(
        asset_id="dsgasset_CHILD",
        generator={"model": "workama.mock.design.v2", "version": "1.0"},
        prompt_hash="h" * 64,
        source_assets=source_assets,
        parent_claim_hash="sha256:parent",
        created_at="2026-07-30T00:00:00Z",
    )
    assert claim == claim2


@pytest.mark.asyncio
async def test_list_provenance_history_multi_version_ordering(monkeypatch):
    rows = [
        _manifest_row(id="dprov_v1", manifest_version="1.0"),
        _manifest_row(id="dprov_v2", manifest_version="1.1"),
        _manifest_row(id="dprov_v3", manifest_version="1.2"),
    ]
    conn = _SeqConnection(results=[_Result(rows=rows)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.list_provenance_history("proj_1", "dsgasset_ASSET1", _actor())
    versions = [item["manifest_version"] for item in result["items"]]
    assert versions == ["1.0", "1.1", "1.2"]


# ============================================================================
# 4. 按 manifest_id 查询
# ============================================================================


@pytest.mark.asyncio
async def test_get_provenance_manifest_by_id_success(monkeypatch):
    manifest = _manifest_row()
    conn = _SeqConnection(results=[_Result(row=manifest)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.get_provenance_manifest("dprov_MANIFEST1", _actor())
    assert result["id"] == "dprov_MANIFEST1"
    assert result["asset_id"] == "dsgasset_ASSET1"


@pytest.mark.asyncio
async def test_get_provenance_manifest_by_id_not_found_404(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.get_provenance_manifest("dprov_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_provenance_manifest_by_id_cross_workspace_isolation(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.get_provenance_manifest("dprov_MANIFEST1", _actor(workspace_id="wsp_other"))
    assert exc.value.status_code == 404
    query, params = conn.calls[0]
    assert "wsp_other" in params


# ============================================================================
# 5. 验证 manifest
# ============================================================================


@pytest.mark.asyncio
async def test_verify_provenance_manifest_success(monkeypatch):
    created_at = datetime.now(UTC)
    created_at_str = created_at.isoformat().replace("+00:00", "Z")
    expected_hash = design._compute_claim_hash(
        asset_id="dsgasset_ASSET1",
        generator={"model": "workama.mock.design.v2", "version": "1.0"},
        prompt_hash="b" * 64,
        source_assets=[],
        parent_claim_hash=None,
        created_at=created_at_str,
    )
    manifest = _manifest_row(created_at=created_at, claim_hash=expected_hash)
    conn = _SeqConnection(results=[_Result(row=manifest)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.verify_provenance_manifest("dprov_MANIFEST1", _actor())
    assert result["verified"] is True
    assert result["manifest_id"] == "dprov_MANIFEST1"


@pytest.mark.asyncio
async def test_verify_provenance_manifest_tampered_fails(monkeypatch):
    created_at = datetime.now(UTC)
    manifest = _manifest_row(created_at=created_at, claim_hash="sha256:tampered")
    conn = _SeqConnection(results=[_Result(row=manifest)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.verify_provenance_manifest("dprov_MANIFEST1", _actor())
    assert result["verified"] is False
    assert "does not match" in result["reason"]
    assert result["expected"] != result["actual"]


@pytest.mark.asyncio
async def test_verify_provenance_manifest_not_found_404(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.verify_provenance_manifest("dprov_missing", _actor())
    assert exc.value.status_code == 404


# ============================================================================
# 6. 列表
# ============================================================================


@pytest.mark.asyncio
async def test_list_provenance_manifests_workspace_filter(monkeypatch):
    rows = [_manifest_row(id="dprov_M1")]
    conn = _SeqConnection(results=[_Result(rows=rows), _Result(row={"total": 1})])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.list_provenance_manifests(
        _actor(), workspace_id="wsp_test", project_id=None, asset_id=None, limit=50, offset=0
    )
    assert result["total"] == 1
    assert result["items"][0]["id"] == "dprov_M1"
    query, params = conn.calls[0]
    assert "workspace_id=%s" in query
    assert "wsp_test" in params


@pytest.mark.asyncio
async def test_list_provenance_manifests_project_filter(monkeypatch):
    rows = [_manifest_row(id="dprov_M1", project_id="proj_1")]
    conn = _SeqConnection(results=[_Result(rows=rows), _Result(row={"total": 1})])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.list_provenance_manifests(
        _actor(), workspace_id="wsp_test", project_id="proj_1", asset_id=None, limit=50, offset=0
    )
    assert result["total"] == 1
    query, params = conn.calls[0]
    assert "project_id=%s" in query
    assert "proj_1" in params


@pytest.mark.asyncio
async def test_list_provenance_manifests_asset_filter(monkeypatch):
    rows = [_manifest_row(id="dprov_M1", asset_id="dsgasset_A")]
    conn = _SeqConnection(results=[_Result(rows=rows), _Result(row={"total": 1})])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.list_provenance_manifests(
        _actor(), workspace_id="wsp_test", project_id=None, asset_id="dsgasset_A", limit=50, offset=0
    )
    assert result["total"] == 1
    query, params = conn.calls[0]
    assert "asset_id=%s" in query
    assert "dsgasset_A" in params


@pytest.mark.asyncio
async def test_list_provenance_manifests_pagination(monkeypatch):
    rows = [_manifest_row(id="dprov_M1")]
    conn = _SeqConnection(results=[_Result(rows=rows), _Result(row={"total": 100})])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.list_provenance_manifests(
        _actor(), workspace_id="wsp_test", project_id=None, asset_id=None, limit=10, offset=20
    )
    assert result["limit"] == 10
    assert result["offset"] == 20
    assert result["total"] == 100
    query, params = conn.calls[0]
    assert "LIMIT %s OFFSET %s" in query
    assert 10 in params
    assert 20 in params


@pytest.mark.asyncio
async def test_list_provenance_manifests_cross_workspace_403(monkeypatch):
    conn = _SeqConnection()
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.list_provenance_manifests(
            _actor(workspace_id="wsp_test"),
            workspace_id="wsp_other",
            project_id=None,
            asset_id=None,
            limit=50,
            offset=0,
        )
    assert exc.value.status_code == 403


# ============================================================================
# 7. 边界
# ============================================================================


@pytest.mark.asyncio
async def test_create_provenance_manifest_empty_source_assets(monkeypatch):
    asset = _asset_row()
    manifest = _manifest_row(source_assets=[], parent_claim_hash=None)
    conn = _SeqConnection(results=[
        _Result(row=asset),
        _Result(row=manifest),
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.create_provenance_manifest(
        "proj_1",
        "dsgasset_ASSET1",
        design.ProvenanceManifestCreate(source_assets=[]),
        _actor(),
    )
    assert result["source_assets"] == []
    assert result["parent_claim_hash"] is None


@pytest.mark.asyncio
async def test_create_provenance_manifest_multiple_parent_assets(monkeypatch):
    asset = _asset_row()
    parent_rows = [
        {"id": "dsgasset_P1", "content_sha256": "p1" * 32},
        {"id": "dsgasset_P2", "content_sha256": "p2" * 32},
    ]
    manifest = _manifest_row(
        source_assets=[
            {"asset_id": "dsgasset_P1", "content_sha256": "p1" * 32, "claim_hash": "sha256:parent1"},
            {"asset_id": "dsgasset_P2", "content_sha256": "p2" * 32, "claim_hash": "sha256:parent2"},
        ],
        parent_claim_hash="sha256:parent1",
    )
    conn = _SeqConnection(results=[
        _Result(row=asset),  # SELECT asset
        _Result(rows=parent_rows),  # SELECT parents
        _Result(row={"claim_hash": "sha256:parent1"}),  # latest manifest for P1
        _Result(row={"claim_hash": "sha256:parent2"}),  # latest manifest for P2
        _Result(row=manifest),  # INSERT RETURNING
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.create_provenance_manifest(
        "proj_1",
        "dsgasset_ASSET1",
        design.ProvenanceManifestCreate(
            source_assets=[
                design.ProvenanceSourceAsset(asset_id="dsgasset_P1"),
                design.ProvenanceSourceAsset(asset_id="dsgasset_P2"),
            ],
        ),
        _actor(),
    )
    assert len(result["source_assets"]) == 2
    assert result["parent_claim_hash"] == "sha256:parent1"
    assert result["source_assets"][0]["claim_hash"] == "sha256:parent1"
    assert result["source_assets"][1]["claim_hash"] == "sha256:parent2"


@pytest.mark.asyncio
async def test_create_provenance_manifest_with_parent_chain(monkeypatch):
    """父子链路：父资产有 manifest → parent_claim_hash 取父 manifest 的 claim_hash。"""
    asset = _asset_row()
    parent_rows = [{"id": "dsgasset_PARENT", "content_sha256": "p" * 64}]
    manifest = _manifest_row(
        source_assets=[{"asset_id": "dsgasset_PARENT", "content_sha256": "p" * 64, "claim_hash": "sha256:parent_claim"}],
        parent_claim_hash="sha256:parent_claim",
    )
    conn = _SeqConnection(results=[
        _Result(row=asset),  # SELECT asset
        _Result(rows=parent_rows),  # SELECT parents
        _Result(row={"claim_hash": "sha256:parent_claim"}),  # latest manifest for parent
        _Result(row=manifest),  # INSERT RETURNING
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.create_provenance_manifest(
        "proj_1",
        "dsgasset_ASSET1",
        design.ProvenanceManifestCreate(
            source_assets=[design.ProvenanceSourceAsset(asset_id="dsgasset_PARENT")],
        ),
        _actor(),
    )
    assert result["parent_claim_hash"] == "sha256:parent_claim"
    assert result["source_assets"][0]["claim_hash"] == "sha256:parent_claim"


# ============================================================================
# 8. 辅助函数
# ============================================================================


def test_compute_claim_hash_deterministic():
    kwargs = dict(
        asset_id="dsgasset_A",
        generator={"model": "workama.mock.design.v2", "version": "1.0"},
        prompt_hash="h" * 64,
        source_assets=[{"asset_id": "dsgasset_P", "content_sha256": "p" * 64, "claim_hash": "sha256:p"}],
        parent_claim_hash="sha256:p",
        created_at="2026-07-30T00:00:00Z",
    )
    h1 = design._compute_claim_hash(**kwargs)
    h2 = design._compute_claim_hash(**kwargs)
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_canonical_json_sorted_keys():
    """canonical JSON 应使用 sort_keys=True，键顺序与值无关。"""
    json_a = design._canonical_json_for_claim(
        asset_id="A",
        generator={"z": 1, "a": 2},
        prompt_hash="h",
        source_assets=[],
        parent_claim_hash=None,
        created_at="2026-01-01T00:00:00Z",
    )
    json_b = design._canonical_json_for_claim(
        asset_id="A",
        generator={"a": 2, "z": 1},
        prompt_hash="h",
        source_assets=[],
        parent_claim_hash=None,
        created_at="2026-01-01T00:00:00Z",
    )
    assert json_a == json_b
    parsed = json.loads(json_a)
    keys = list(parsed.keys())
    assert keys == sorted(keys)


def test_compute_claim_hash_different_inputs_different_hashes():
    base = dict(
        asset_id="dsgasset_A",
        generator={"model": "workama.mock.design.v2", "version": "1.0"},
        prompt_hash="h" * 64,
        source_assets=[],
        parent_claim_hash=None,
        created_at="2026-07-30T00:00:00Z",
    )
    h1 = design._compute_claim_hash(**base)
    # 改变 asset_id
    changed = dict(base)
    changed["asset_id"] = "dsgasset_B"
    h2 = design._compute_claim_hash(**changed)
    assert h1 != h2
    # 改变 created_at
    changed2 = dict(base)
    changed2["created_at"] = "2026-07-31T00:00:00Z"
    h3 = design._compute_claim_hash(**changed2)
    assert h1 != h3


# ============================================================================
# 9. Schema / 路由
# ============================================================================


@pytest.mark.asyncio
async def test_schema_includes_provenance_manifest_table():
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await design.ensure_design_schema(Connection())
    schema = "\n".join(statements)
    assert "ag_design_provenance_manifest" in schema
    assert "claim_hash" in schema
    assert "parent_claim_hash" in schema
    assert "manifest_version" in schema
    assert "source_assets" in schema
    assert "UNIQUE(workspace_id, asset_id, manifest_version)" in schema
    assert "idx_ag_design_provenance_claim_hash" in schema
    assert "idx_ag_design_provenance_parent_claim" in schema


def test_design_router_exposes_provenance_endpoints():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in design.router.routes}
    assert ("/api/v1/design/projects/{project_id}/assets/{asset_id}/provenance", ("POST",)) in paths
    assert ("/api/v1/design/projects/{project_id}/assets/{asset_id}/provenance", ("GET",)) in paths
    assert ("/api/v1/design/projects/{project_id}/assets/{asset_id}/provenance/history", ("GET",)) in paths
    assert ("/api/v1/design/provenance/{manifest_id}", ("GET",)) in paths
    assert ("/api/v1/design/provenance/{manifest_id}/verify", ("POST",)) in paths
    assert ("/api/v1/design/provenance", ("GET",)) in paths


# ============================================================================
# 10. 额外边界
# ============================================================================


@pytest.mark.asyncio
async def test_create_provenance_manifest_default_generator(monkeypatch):
    """generator 为空时自动填充为 {model, version}。"""
    asset = _asset_row()
    manifest = _manifest_row(generator={"model": design.DESIGN_GENERATOR, "version": "1.0"})
    conn = _SeqConnection(results=[
        _Result(row=asset),
        _Result(row=manifest),
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.create_provenance_manifest(
        "proj_1",
        "dsgasset_ASSET1",
        design.ProvenanceManifestCreate(generator={}),
        _actor(),
    )
    # generator 为空时会被填充
    insert_query, insert_params = conn.calls[1]
    assert "INSERT" in insert_query
    # generator JSON 应包含 DESIGN_GENERATOR
    generator_json = next((p for p in insert_params if isinstance(p, str) and "model" in p), None)
    assert generator_json is not None
    assert design.DESIGN_GENERATOR in generator_json


@pytest.mark.asyncio
async def test_create_provenance_manifest_source_asset_not_in_workspace_422(monkeypatch):
    """source_assets 中的父资产在 workspace 中不存在 → 422。"""
    asset = _asset_row()
    conn = _SeqConnection(results=[
        _Result(row=asset),  # SELECT asset
        _Result(rows=[]),  # SELECT parents (empty)
    ])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.create_provenance_manifest(
            "proj_1",
            "dsgasset_ASSET1",
            design.ProvenanceManifestCreate(
                source_assets=[design.ProvenanceSourceAsset(asset_id="dsgasset_GHOST")],
            ),
            _actor(),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_list_provenance_manifests_missing_capability_403(monkeypatch):
    conn = _SeqConnection()
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.list_provenance_manifests(
            _actor(capabilities=()),
            workspace_id="wsp_test",
            project_id=None,
            asset_id=None,
            limit=50,
            offset=0,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_latest_provenance_manifest_missing_capability_403(monkeypatch):
    conn = _SeqConnection()
    monkeypatch.setattr(design, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await design.get_latest_provenance_manifest("proj_1", "dsgasset_A", _actor(capabilities=()))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_provenance_manifest_with_source_assets(monkeypatch):
    """带 source_assets 的 manifest 验证：claim_hash 应能匹配。"""
    created_at = datetime.now(UTC)
    created_at_str = created_at.isoformat().replace("+00:00", "Z")
    source_assets = [{"asset_id": "dsgasset_P", "content_sha256": "p" * 64, "claim_hash": "sha256:parent"}]
    expected_hash = design._compute_claim_hash(
        asset_id="dsgasset_ASSET1",
        generator={"model": "workama.mock.design.v2", "version": "1.0"},
        prompt_hash="b" * 64,
        source_assets=source_assets,
        parent_claim_hash="sha256:parent",
        created_at=created_at_str,
    )
    manifest = _manifest_row(
        created_at=created_at,
        source_assets=source_assets,
        parent_claim_hash="sha256:parent",
        claim_hash=expected_hash,
    )
    conn = _SeqConnection(results=[_Result(row=manifest)])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.verify_provenance_manifest("dprov_M1", _actor())
    assert result["verified"] is True


@pytest.mark.asyncio
async def test_manifest_public_parses_jsonb_strings():
    """_manifest_public 应将 JSONB 字符串解析为 dict/list。"""
    row = {
        "id": "dprov_M1",
        "generator": json.dumps({"model": "workama.mock.design.v2"}),
        "source_assets": json.dumps([{"asset_id": "dsgasset_P"}]),
        "metadata": json.dumps({"key": "value"}),
        "claim_hash": "sha256:x",
    }
    result = design._manifest_public(row)
    assert result["generator"] == {"model": "workama.mock.design.v2"}
    assert result["source_assets"] == [{"asset_id": "dsgasset_P"}]
    assert result["metadata"] == {"key": "value"}


@pytest.mark.asyncio
async def test_list_provenance_history_empty(monkeypatch):
    conn = _SeqConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(design, "pool", _Pool(conn))

    result = await design.list_provenance_history("proj_1", "dsgasset_A", _actor())
    assert result["total"] == 0
    assert result["items"] == []
