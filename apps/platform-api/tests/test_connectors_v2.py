"""企业知识连接器 v2 单元 + 端点测试。

覆盖 ConnectorAdapter 抽象基类、GoogleDriveAdapter / NotionAdapter mock 实现、
REST 端点 CRUD / sync / dry-run / 鉴权 / workspace 隔离。

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 外部 HTTP。
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import connectors_v2 as cv2
from workama_platform.modules.connectors_v2 import (
    GoogleDriveAdapter,
    NotionAdapter,
    _get_adapter,
)


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
    capabilities=("connector:*",),
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


def _row(**overrides) -> dict:
    base = {
        "id": "connv2_1",
        "workspace_id": "wsp_test",
        "name": "Test Connector",
        "provider": "google_drive",
        "status": "active",
        "auth_config": {"client_email": "svc@example.com"},
        "sync_root": "root",
        "last_cursor": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(cv2.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. Schema
# ============================================================================


@pytest.mark.asyncio
async def test_schema_statements_contain_expected_table_and_fields():
    """SCHEMA_STATEMENTS 包含 connector_config_v2 表与关键字段。"""
    schema = "\n".join(cv2.SCHEMA_STATEMENTS)
    assert "connector_config_v2" in schema
    for field in (
        "workspace_id",
        "provider",
        "status",
        "auth_config",
        "sync_root",
        "last_cursor",
        "updated_at",
    ):
        assert field in schema
    assert "UNIQUE(workspace_id, name)" in schema
    assert "provider IN ('google_drive','notion')" in schema
    assert "status IN ('active','pending','disabled','error')" in schema


# ============================================================================
# 2. GoogleDriveAdapter
# ============================================================================


class TestGoogleDriveAdapter:
    """Google Drive 适配器 mock 行为测试。"""

    @pytest.mark.asyncio
    async def test_authenticate_success(self):
        adapter = GoogleDriveAdapter()
        result = await adapter.authenticate({"client_email": "svc@example.com"})
        assert result["adapter"] == "google_drive"
        assert result["authenticated"] is True

    @pytest.mark.asyncio
    async def test_authenticate_rejects_invalid_email(self):
        adapter = GoogleDriveAdapter()
        with pytest.raises(ValueError):
            await adapter.authenticate({"client_email": "not-an-email"})
        with pytest.raises(ValueError):
            await adapter.authenticate({})

    @pytest.mark.asyncio
    async def test_discover_returns_mock_items(self):
        adapter = GoogleDriveAdapter()
        items = await adapter.discover({"sync_root": "my_folder"})
        assert len(items) == 2
        assert items[0]["source_id"].startswith("gdrive:my_folder:")
        assert "mime_type" in items[0]

    @pytest.mark.asyncio
    async def test_incremental_sync_returns_items_and_cursor(self):
        adapter = GoogleDriveAdapter()
        items, cursor = await adapter.incremental_sync({"sync_root": "root"}, None)
        assert len(items) == 2
        assert cursor and isinstance(cursor, str)
        # 再次用相同 cursor 应返回空
        items2, cursor2 = await adapter.incremental_sync({"sync_root": "root"}, cursor)
        assert items2 == []
        assert cursor2 == cursor

    @pytest.mark.asyncio
    async def test_map_acl(self):
        adapter = GoogleDriveAdapter()
        acl = await adapter.map_acl({}, {"source_id": "gdrive:root:f1"})
        assert acl["source_id"] == "gdrive:root:f1"
        assert "owner@example.com" in acl["allow_users"]

    @pytest.mark.asyncio
    async def test_propagate_deletion(self):
        adapter = GoogleDriveAdapter()
        tomb = await adapter.propagate_deletion({}, "gdrive:root:f1")
        assert tomb["status"] == "tombstone"
        assert tomb["source_id"] == "gdrive:root:f1"


# ============================================================================
# 3. NotionAdapter
# ============================================================================


class TestNotionAdapter:
    """Notion 适配器 mock 行为测试。"""

    @pytest.mark.asyncio
    async def test_authenticate_success(self):
        adapter = NotionAdapter()
        result = await adapter.authenticate({"integration_token": "secret_token_123"})
        assert result["adapter"] == "notion"
        assert result["authenticated"] is True

    @pytest.mark.asyncio
    async def test_authenticate_rejects_short_token(self):
        adapter = NotionAdapter()
        with pytest.raises(ValueError):
            await adapter.authenticate({"integration_token": "short"})
        with pytest.raises(ValueError):
            await adapter.authenticate({})

    @pytest.mark.asyncio
    async def test_discover_returns_mock_items(self):
        adapter = NotionAdapter()
        items = await adapter.discover({"sync_root": "my_db"})
        assert len(items) == 2
        assert items[0]["source_id"].startswith("notion:my_db:")
        assert "page_type" in items[0]

    @pytest.mark.asyncio
    async def test_incremental_sync_idempotent_cursor(self):
        adapter = NotionAdapter()
        items, cursor = await adapter.incremental_sync({"sync_root": "root"}, None)
        assert len(items) == 2
        items2, cursor2 = await adapter.incremental_sync({"sync_root": "root"}, cursor)
        assert items2 == []
        assert cursor2 == cursor

    @pytest.mark.asyncio
    async def test_map_acl(self):
        adapter = NotionAdapter()
        acl = await adapter.map_acl({}, {"source_id": "notion:root:p1"})
        assert acl["source_id"] == "notion:root:p1"
        assert "engineering" in acl["allow_groups"]

    @pytest.mark.asyncio
    async def test_propagate_deletion(self):
        adapter = NotionAdapter()
        tomb = await adapter.propagate_deletion({}, "notion:root:p1")
        assert tomb["status"] == "tombstone"


# ============================================================================
# 4. 适配器工厂
# ============================================================================


class TestAdapterFactory:
    """适配器工厂行为测试。"""

    def test_get_adapter_returns_instance(self):
        assert isinstance(_get_adapter("google_drive"), GoogleDriveAdapter)
        assert isinstance(_get_adapter("notion"), NotionAdapter)

    def test_get_adapter_raises_on_unknown_provider(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _get_adapter("unknown")  # type: ignore[arg-type]
        assert exc_info.value.status_code == 422


# ============================================================================
# 5. CRUD 端点
# ============================================================================


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_connector_v2_success(self, monkeypatch):
        row = _row()
        conn = _RecordingConnection(results=[_Result(row=None), _Result(row=row)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))
        monkeypatch.setattr(cv2, "new_id", lambda prefix: "connv2_1")

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/connectors/v2",
                json={
                    "name": "Test Connector",
                    "provider": "google_drive",
                    "auth_config": {"client_email": "svc@example.com"},
                    "sync_root": "root",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["connector"]["name"] == "Test Connector"
        assert body["connector"]["provider"] == "google_drive"
        assert body["auth_summary"]["authenticated"] is True

    @pytest.mark.asyncio
    async def test_create_connector_v2_duplicate_name_returns_409(self, monkeypatch):
        # SELECT 1 返回已有记录
        conn = _RecordingConnection(results=[_Result(row={"1": 1})])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/connectors/v2",
                json={
                    "name": "Test Connector",
                    "provider": "google_drive",
                    "auth_config": {"client_email": "svc@example.com"},
                },
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_create_connector_v2_unsupported_provider_returns_422(self):
        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/connectors/v2",
                json={"name": "Bad", "provider": "dropbox"},
            )
        assert resp.status_code == 422


class TestList:
    @pytest.mark.asyncio
    async def test_list_connectors_v2_returns_items(self, monkeypatch):
        rows = [_row(id="connv2_a"), _row(id="connv2_b", provider="notion")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/connectors/v2")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["meta"]["count"] == 2

    @pytest.mark.asyncio
    async def test_list_connectors_v2_filters_by_provider(self, monkeypatch):
        rows = [_row(id="connv2_a", provider="google_drive")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/connectors/v2?provider=google_drive")
        assert resp.status_code == 200
        body = resp.json()
        assert all(i["provider"] == "google_drive" for i in body["items"])


class TestGet:
    @pytest.mark.asyncio
    async def test_get_connector_v2_success(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=_row())])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/connectors/v2/connv2_1")
        assert resp.status_code == 200
        assert resp.json()["connector"]["id"] == "connv2_1"

    @pytest.mark.asyncio
    async def test_get_connector_v2_not_found_returns_404(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/connectors/v2/missing")
        assert resp.status_code == 404


class TestPatch:
    @pytest.mark.asyncio
    async def test_patch_connector_v2_success(self, monkeypatch):
        updated = _row(name="Renamed", status="disabled")
        conn = _RecordingConnection(results=[_Result(row=updated)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/connectors/v2/connv2_1",
                json={"name": "Renamed", "status": "disabled"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["connector"]["name"] == "Renamed"
        assert body["connector"]["status"] == "disabled"

    @pytest.mark.asyncio
    async def test_patch_connector_v2_no_changes_returns_current(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=_row())])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch("/api/v1/connectors/v2/connv2_1", json={})
        assert resp.status_code == 200
        assert resp.json()["connector"]["id"] == "connv2_1"


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_connector_v2_success(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row={"id": "connv2_1"})])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/connectors/v2/connv2_1")
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_connector_v2_not_found_returns_404(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/connectors/v2/missing")
        assert resp.status_code == 404


# ============================================================================
# 6. Sync / Dry-run
# ============================================================================


class TestSync:
    @pytest.mark.asyncio
    async def test_sync_connector_v2_success(self, monkeypatch):
        row = _row(status="active", last_cursor=None)
        conn = _RecordingConnection(
            results=[
                _Result(row=row),  # SELECT
                _Result(),  # UPDATE cursor
            ]
        )
        monkeypatch.setattr(cv2, "pool", _Pool(conn))
        monkeypatch.setattr(cv2, "new_id", lambda prefix: "op_1")

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/connectors/v2/connv2_1/sync")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["items_synced"] == 2
        assert body["operation_id"].startswith("op")
        assert body["next_cursor"] is not None

    @pytest.mark.asyncio
    async def test_sync_connector_v2_not_active_returns_409(self, monkeypatch):
        row = _row(status="pending")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/connectors/v2/connv2_1/sync")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_sync_connector_v2_not_found_returns_404(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/connectors/v2/missing/sync")
        assert resp.status_code == 404


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_connector_v2_success(self, monkeypatch):
        row = _row(status="active")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/connectors/v2/connv2_1/dry-run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["discovered_count"] == 2
        assert len(body["acl_mappings"]) == 2
        assert body["deletion_propagation"]["status"] == "tombstone"
        assert body["auth_summary"]["authenticated"] is True

    @pytest.mark.asyncio
    async def test_dry_run_connector_v2_not_found_returns_404(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/connectors/v2/missing/dry-run")
        assert resp.status_code == 404


# ============================================================================
# 7. 鉴权与隔离
# ============================================================================


class TestAuth:
    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self):
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/connectors/v2")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_insufficient_capabilities_returns_403(self):
        app = _app(actor=_actor(capabilities=(), role="viewer"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/connectors/v2",
                json={"name": "XX", "provider": "google_drive"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_list_read_only_role_can_read(self, monkeypatch):
        rows = [_row()]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor(role="viewer", capabilities=()))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/connectors/v2")
        assert resp.status_code == 200


class TestWorkspaceIsolation:
    @pytest.mark.asyncio
    async def test_workspace_isolation_prevents_cross_access(self, monkeypatch):
        # 模拟跨区查询无结果（真实数据库会因 workspace_id 不匹配返回 None）
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(cv2, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/connectors/v2/connv2_1")
        # 因为查询条件是 id=%s AND workspace_id=%s，跨区时 fetchone 为 None
        assert resp.status_code == 404
