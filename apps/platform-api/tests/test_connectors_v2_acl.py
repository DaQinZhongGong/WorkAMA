"""企业知识连接器 v2 - ACL 增量同步落库 / 同步游标 / ACL 审计 测试。

覆盖：
- Schema：4 张新表与索引存在 (4)
- 辅助函数：_sync_run_view / _acl_mapping_view / _sync_cursor_view / _acl_audit_view (4)
- 同步历史列表：空列表 / 分页 / 倒序 / workspace 隔离 (4)
- 同步详情：成功 / 不存在 404 / 跨 workspace 403 (3)
- ACL 映射列表：空列表 / 分页 / source_entity 过滤 / permission 过滤 / workspace 隔离 (5)
- ACL 映射应用：pending->applied / 已 applied 409 / 已 rejected 409 / 不存在 404 / 跨 workspace 403 / applied_by / 审计 (7)
- ACL 映射拒绝：pending->rejected / 带 reason / 已 applied 409 / 不存在 404 / reject_reason / 审计 (6)
- 同步游标：获取成功 / 不存在 404 / 重置成功 / 重置写审计 / next_page_token 清空 / total_synced=0 (6)
- 同步流程集成：落库 sync_run / 落库 acl_mapping / 更新 cursor / 写 audit (4)
- 边界：UNIQUE 冲突跳过 / connector 不存在 (2)

所有测试使用 fake pool/connection，不依赖真实 DB / Redis / 外部 HTTP。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from workama_platform.core import Actor, get_actor
from workama_platform.modules import connectors_v2 as cv2


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


def _connector_row(**overrides) -> dict:
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


def _sync_run_row(**overrides) -> dict:
    base = {
        "id": "syncrun_1",
        "connector_id": "connv2_1",
        "workspace_id": "wsp_test",
        "status": "completed",
        "items_synced": 2,
        "acl_mappings_count": 2,
        "duration_ms": 42,
        "error": None,
        "started_at": datetime.now(UTC),
        "finished_at": datetime.now(UTC),
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _acl_mapping_row(**overrides) -> dict:
    base = {
        "id": "aclmap_1",
        "connector_id": "connv2_1",
        "workspace_id": "wsp_test",
        "source_entity": "gdrive:root:file_1",
        "source_permission": "owner@example.com",
        "workama_resource_type": "document",
        "workama_resource_id": "resource:gdrive:root:file_1",
        "workama_permission": "admin,member,owner",
        "mapping_status": "pending",
        "applied_by": None,
        "applied_at": None,
        "reject_reason": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _cursor_row(**overrides) -> dict:
    base = {
        "connector_id": "connv2_1",
        "workspace_id": "wsp_test",
        "last_synced_at": datetime.now(UTC),
        "next_page_token": "cursor_abc",
        "total_synced": 10,
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _audit_row(**overrides) -> dict:
    base = {
        "id": "aclaud_1",
        "connector_id": "connv2_1",
        "workspace_id": "wsp_test",
        "mapping_id": "aclmap_1",
        "action": "mapped",
        "actor_id": "usr_test",
        "details": {"source_entity": "gdrive:root:file_1"},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(cv2.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


class _MockAdapter(cv2.ConnectorAdapter):
    """可控 mock adapter，返回预设的 items 与 ACL 映射。"""

    def __init__(self, items=None, next_cursor="cursor_mock", acl_payloads=None):
        self._items = items if items is not None else [
            {"source_id": "mock:item:1", "title": "Item 1"},
            {"source_id": "mock:item:2", "title": "Item 2"},
        ]
        self._next_cursor = next_cursor
        self._acl_payloads = acl_payloads or {}

    async def authenticate(self, config):
        return {"adapter": "mock", "authenticated": True}

    async def discover(self, config):
        return list(self._items)

    async def incremental_sync(self, config, cursor):
        return list(self._items), self._next_cursor

    async def map_acl(self, config, source_item):
        sid = source_item.get("source_id")
        if sid in self._acl_payloads:
            return self._acl_payloads[sid]
        return {
            "source_id": sid,
            "allow_users": ["owner@example.com"],
            "allow_groups": [],
            "allow_roles": ["reader"],
        }

    async def propagate_deletion(self, config, source_id):
        return {"source_id": source_id, "status": "tombstone"}


# ============================================================================
# 1. Schema
# ============================================================================


@pytest.mark.asyncio
async def test_schema_includes_sync_run_table():
    schema = "\n".join(cv2.SCHEMA_STATEMENTS)
    assert "connector_sync_run" in schema
    assert "items_synced" in schema
    assert "acl_mappings_count" in schema
    assert "duration_ms" in schema
    assert "started_at" in schema
    assert "finished_at" in schema
    assert "status IN ('running','completed','failed')" in schema
    assert "idx_connector_sync_run_connector_time" in schema


@pytest.mark.asyncio
async def test_schema_includes_acl_mapping_table():
    schema = "\n".join(cv2.SCHEMA_STATEMENTS)
    assert "connector_acl_mapping" in schema
    assert "source_entity" in schema
    assert "source_permission" in schema
    assert "workama_resource_id" in schema
    assert "workama_permission" in schema
    assert "mapping_status IN ('pending','applied','rejected')" in schema
    assert "reject_reason" in schema
    assert "UNIQUE(connector_id, source_entity, workama_resource_id)" in schema
    assert "idx_connector_acl_mapping_connector_status" in schema


@pytest.mark.asyncio
async def test_schema_includes_sync_cursor_table():
    schema = "\n".join(cv2.SCHEMA_STATEMENTS)
    assert "connector_sync_cursor" in schema
    assert "next_page_token" in schema
    assert "total_synced" in schema
    assert "last_synced_at" in schema
    # connector_id 作为主键
    assert "connector_id TEXT PRIMARY KEY" in schema


@pytest.mark.asyncio
async def test_schema_includes_acl_audit_table():
    schema = "\n".join(cv2.SCHEMA_STATEMENTS)
    assert "connector_acl_audit" in schema
    assert "action" in schema
    assert "actor_id" in schema
    assert "details" in schema
    assert "action IN ('mapped','applied','rejected','reset_cursor')" in schema
    assert "idx_connector_acl_audit_connector_time" in schema


# ============================================================================
# 2. 辅助函数
# ============================================================================


def test_sync_run_view_serializes_fields():
    row = _sync_run_row()
    view = cv2._sync_run_view(row)
    assert view["id"] == "syncrun_1"
    assert view["connector_id"] == "connv2_1"
    assert view["status"] == "completed"
    assert view["items_synced"] == 2
    assert view["acl_mappings_count"] == 2
    assert view["duration_ms"] == 42
    assert view["error"] is None
    assert view["started_at"] is not None
    assert view["finished_at"] is not None


def test_acl_mapping_view_serializes_fields():
    row = _acl_mapping_row(
        mapping_status="applied",
        applied_by="usr_admin",
        applied_at=datetime.now(UTC),
    )
    view = cv2._acl_mapping_view(row)
    assert view["id"] == "aclmap_1"
    assert view["source_entity"] == "gdrive:root:file_1"
    assert view["workama_resource_id"] == "resource:gdrive:root:file_1"
    assert view["mapping_status"] == "applied"
    assert view["applied_by"] == "usr_admin"
    assert view["applied_at"] is not None


def test_sync_cursor_view_serializes_fields():
    row = _cursor_row()
    view = cv2._sync_cursor_view(row)
    assert view["connector_id"] == "connv2_1"
    assert view["next_page_token"] == "cursor_abc"
    assert view["total_synced"] == 10
    assert view["last_synced_at"] is not None
    assert view["updated_at"] is not None


def test_acl_audit_view_parses_jsonb_string():
    """_acl_audit_view 应将 JSONB 字符串解析为 dict。"""
    row = _audit_row(details=json.dumps({"key": "value"}))
    view = cv2._acl_audit_view(row)
    assert view["details"] == {"key": "value"}
    assert view["action"] == "mapped"
    assert view["actor_id"] == "usr_test"
    # dict 形式 details 应原样保留
    row2 = _audit_row(details={"key2": "value2"})
    view2 = cv2._acl_audit_view(row2)
    assert view2["details"] == {"key2": "value2"}


# ============================================================================
# 3. 同步历史列表
# ============================================================================


@pytest.mark.asyncio
async def test_list_sync_history_empty(monkeypatch):
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),  # _assert_connector_in_workspace
        _Result(rows=[]),  # SELECT sync_runs
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/sync-history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["has_more"] is False
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_sync_history_pagination(monkeypatch):
    rows = [_sync_run_row(id=f"run_{i}") for i in range(3)]
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(rows=rows[:2]),  # 第一页 limit=2
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/sync-history?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    # next_cursor 应为最后一项的 created_at
    assert body["next_cursor"] is not None
    # SQL 应包含 LIMIT
    select_query, select_params = conn.calls[1]
    assert "ORDER BY created_at DESC" in select_query
    assert "LIMIT %s" in select_query
    assert 2 in select_params


@pytest.mark.asyncio
async def test_list_sync_history_descending_by_created_at(monkeypatch):
    """同步历史应按 created_at 倒序排列。"""
    rows = [_sync_run_row(id="run_old"), _sync_run_row(id="run_new")]
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(rows=rows),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/sync-history")
    assert resp.status_code == 200
    select_query, _ = conn.calls[1]
    assert "ORDER BY created_at DESC" in select_query


@pytest.mark.asyncio
async def test_list_sync_history_workspace_isolation(monkeypatch):
    """跨 workspace 访问 connector 应 404。"""
    conn = _RecordingConnection(results=[_Result(row=None)])  # connector not found
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor(workspace_id="wsp_other"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/sync-history")
    assert resp.status_code == 404
    query, params = conn.calls[0]
    assert "wsp_other" in params


# ============================================================================
# 4. 同步详情
# ============================================================================


@pytest.mark.asyncio
async def test_get_sync_history_detail_success(monkeypatch):
    run_row = _sync_run_row()
    mapping_rows = [_acl_mapping_row()]
    audit_rows = [_audit_row()]
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),  # _assert_connector
        _Result(row=run_row),  # sync_run
        _Result(rows=mapping_rows),  # mappings
        _Result(rows=audit_rows),  # audits
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/sync-history/syncrun_1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sync_run"]["id"] == "syncrun_1"
    assert body["sync_run"]["status"] == "completed"
    assert body["sync_run"]["items_synced"] == 2
    assert body["sync_run"]["acl_mappings_count"] == 2
    assert len(body["acl_mappings"]) == 1
    assert body["acl_mappings"][0]["source_entity"] == "gdrive:root:file_1"
    assert len(body["audit_logs"]) == 1


@pytest.mark.asyncio
async def test_get_sync_history_detail_not_found_404(monkeypatch):
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=None),  # sync_run not found
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/sync-history/missing")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_sync_history_detail_cross_workspace_403(monkeypatch):
    """sync_run 属于另一个 workspace → 403。"""
    run_row = _sync_run_row(workspace_id="wsp_other")  # 不同 workspace
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),  # connector in wsp_test (passes)
        _Result(row=run_row),  # sync_run in wsp_other (mismatch)
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor(workspace_id="wsp_test"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/sync-history/syncrun_1")
    assert resp.status_code == 403


# ============================================================================
# 5. ACL 映射列表
# ============================================================================


@pytest.mark.asyncio
async def test_list_acl_mappings_empty(monkeypatch):
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(rows=[]),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/acl-mappings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["has_more"] is False


@pytest.mark.asyncio
async def test_list_acl_mappings_pagination(monkeypatch):
    rows = [_acl_mapping_row(id=f"map_{i}") for i in range(5)]
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(rows=rows[:3]),  # limit=3
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/acl-mappings?limit=3")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
    assert body["has_more"] is True
    assert body["next_cursor"] is not None
    select_query, select_params = conn.calls[1]
    assert "ORDER BY created_at DESC" in select_query
    assert "LIMIT %s" in select_query


@pytest.mark.asyncio
async def test_list_acl_mappings_filter_by_source_entity(monkeypatch):
    rows = [_acl_mapping_row(source_entity="user:alice")]
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(rows=rows),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/connectors/v2/connv2_1/acl-mappings?source_entity=user:alice"
        )
    assert resp.status_code == 200
    select_query, select_params = conn.calls[1]
    assert "source_entity=%s" in select_query
    assert "user:alice" in select_params


@pytest.mark.asyncio
async def test_list_acl_mappings_filter_by_permission(monkeypatch):
    rows = [_acl_mapping_row(workama_permission="admin")]
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(rows=rows),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/v1/connectors/v2/connv2_1/acl-mappings?permission=admin"
        )
    assert resp.status_code == 200
    select_query, select_params = conn.calls[1]
    assert "ILIKE" in select_query
    assert any("admin" in str(p) for p in select_params)


@pytest.mark.asyncio
async def test_list_acl_mappings_workspace_isolation(monkeypatch):
    """跨 workspace 应 404（connector 不属于当前 workspace）。"""
    conn = _RecordingConnection(results=[_Result(row=None)])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor(workspace_id="wsp_other"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/acl-mappings")
    assert resp.status_code == 404


# ============================================================================
# 6. ACL 映射应用
# ============================================================================


@pytest.mark.asyncio
async def test_apply_acl_mapping_pending_to_applied(monkeypatch):
    mapping = _acl_mapping_row(mapping_status="pending")
    updated = _acl_mapping_row(mapping_status="applied", applied_by="usr_test")
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),  # _assert_connector
        _Result(row=mapping),  # SELECT mapping
        _Result(row=updated),  # UPDATE RETURNING
        _Result(),  # INSERT audit
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "aclaud_1")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/apply"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["mapping"]["mapping_status"] == "applied"
    assert body["mapping"]["applied_by"] == "usr_test"


@pytest.mark.asyncio
async def test_apply_acl_mapping_already_applied_409(monkeypatch):
    mapping = _acl_mapping_row(mapping_status="applied")
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=mapping),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/apply"
        )
    assert resp.status_code == 409
    assert "applied" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_apply_acl_mapping_already_rejected_409(monkeypatch):
    mapping = _acl_mapping_row(mapping_status="rejected")
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=mapping),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/apply"
        )
    assert resp.status_code == 409
    assert "rejected" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_apply_acl_mapping_not_found_404(monkeypatch):
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=None),  # mapping not found
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/missing/apply"
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_apply_acl_mapping_cross_workspace_403(monkeypatch):
    """mapping 属于另一个 workspace → 403。"""
    mapping = _acl_mapping_row(workspace_id="wsp_other")  # 跨 workspace
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),  # connector in wsp_test (passes)
        _Result(row=mapping),  # mapping in wsp_other (mismatch)
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor(workspace_id="wsp_test"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/apply"
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_apply_acl_mapping_sets_applied_by(monkeypatch):
    """应用映射后 applied_by 应为当前 actor.user_id。"""
    mapping = _acl_mapping_row(mapping_status="pending")
    updated = _acl_mapping_row(mapping_status="applied", applied_by="usr_admin_x")
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=mapping),
        _Result(row=updated),
        _Result(),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "aclaud_x")

    app = _app(actor=_actor(user_id="usr_admin_x"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/apply"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mapping"]["applied_by"] == "usr_admin_x"
    # UPDATE SQL 应包含 applied_by 参数
    update_query, update_params = conn.calls[2]
    assert "applied_by" in update_query
    assert "usr_admin_x" in update_params


@pytest.mark.asyncio
async def test_apply_acl_mapping_writes_audit(monkeypatch):
    """应用映射应写一条 action='applied' 的审计。"""
    mapping = _acl_mapping_row(mapping_status="pending")
    updated = _acl_mapping_row(mapping_status="applied", applied_by="usr_test")
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=mapping),
        _Result(row=updated),
        _Result(),  # INSERT audit
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "aclaud_audit_1")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/apply"
        )
    assert resp.status_code == 200
    # 第 4 个 execute 应是 INSERT INTO connector_acl_audit
    audit_query, audit_params = conn.calls[3]
    assert "INSERT INTO connector_acl_audit" in audit_query
    assert "applied" in audit_params
    assert "usr_test" in audit_params
    assert "aclmap_1" in audit_params


# ============================================================================
# 7. ACL 映射拒绝
# ============================================================================


@pytest.mark.asyncio
async def test_reject_acl_mapping_pending_to_rejected(monkeypatch):
    mapping = _acl_mapping_row(mapping_status="pending")
    updated = _acl_mapping_row(mapping_status="rejected", reject_reason="duplicate")
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=mapping),
        _Result(row=updated),
        _Result(),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "aclaud_r1")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/reject",
            json={"reason": "duplicate"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rejected"] is True
    assert body["mapping"]["mapping_status"] == "rejected"


@pytest.mark.asyncio
async def test_reject_acl_mapping_with_reason(monkeypatch):
    """拒绝请求必须带 reason 字段。"""
    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 缺少 reason → 422
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/reject",
            json={},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_reject_acl_mapping_already_applied_409(monkeypatch):
    mapping = _acl_mapping_row(mapping_status="applied")
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=mapping),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/reject",
            json={"reason": "nope"},
        )
    assert resp.status_code == 409
    assert "applied" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reject_acl_mapping_not_found_404(monkeypatch):
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=None),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/missing/reject",
            json={"reason": "missing"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reject_acl_mapping_stores_reject_reason(monkeypatch):
    """reject_reason 应被持久化（在 UPDATE 参数中可见）。"""
    mapping = _acl_mapping_row(mapping_status="pending")
    updated = _acl_mapping_row(
        mapping_status="rejected", reject_reason="policy violation"
    )
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=mapping),
        _Result(row=updated),
        _Result(),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "aclaud_rr")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/reject",
            json={"reason": "policy violation"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mapping"]["reject_reason"] == "policy violation"
    # UPDATE SQL 应包含 reject_reason 参数
    update_query, update_params = conn.calls[2]
    assert "reject_reason" in update_query
    assert "policy violation" in update_params


@pytest.mark.asyncio
async def test_reject_acl_mapping_writes_audit(monkeypatch):
    """拒绝映射应写一条 action='rejected' 的审计。"""
    mapping = _acl_mapping_row(mapping_status="pending")
    updated = _acl_mapping_row(mapping_status="rejected", reject_reason="bad")
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=mapping),
        _Result(row=updated),
        _Result(),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "aclaud_audit_r")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/reject",
            json={"reason": "bad"},
        )
    assert resp.status_code == 200
    audit_query, audit_params = conn.calls[3]
    assert "INSERT INTO connector_acl_audit" in audit_query
    assert "rejected" in audit_params
    assert "usr_test" in audit_params


# ============================================================================
# 8. 同步游标
# ============================================================================


@pytest.mark.asyncio
async def test_get_sync_cursor_success(monkeypatch):
    cursor = _cursor_row()
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=cursor),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/sync-cursor")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cursor"]["connector_id"] == "connv2_1"
    assert body["cursor"]["next_page_token"] == "cursor_abc"
    assert body["cursor"]["total_synced"] == 10
    # 顶层也应展开字段
    assert body["next_page_token"] == "cursor_abc"


@pytest.mark.asyncio
async def test_get_sync_cursor_not_found_404(monkeypatch):
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=None),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/sync-cursor")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reset_sync_cursor_success(monkeypatch):
    """重置已存在的游标：UPDATE 路径，返回 reset=True。"""
    cursor = _cursor_row()
    updated = _cursor_row(next_page_token=None, total_synced=0)
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=cursor),  # SELECT cursor (exists)
        _Result(row=updated),  # UPDATE RETURNING
        _Result(),  # INSERT audit
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "aclaud_reset")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/connv2_1/sync-cursor/reset")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reset"] is True
    assert body["cursor"] is not None


@pytest.mark.asyncio
async def test_reset_sync_cursor_writes_audit(monkeypatch):
    """重置游标应写一条 action='reset_cursor' 的审计。"""
    cursor = _cursor_row()
    updated = _cursor_row(next_page_token=None, total_synced=0)
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=cursor),
        _Result(row=updated),
        _Result(),  # INSERT audit
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "aclaud_reset_audit")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/connv2_1/sync-cursor/reset")
    assert resp.status_code == 200
    audit_query, audit_params = conn.calls[3]
    assert "INSERT INTO connector_acl_audit" in audit_query
    assert "reset_cursor" in audit_params
    assert "usr_test" in audit_params


@pytest.mark.asyncio
async def test_reset_sync_cursor_clears_next_page_token(monkeypatch):
    """重置后响应中 next_page_token 应为 None。"""
    cursor = _cursor_row()
    updated = _cursor_row(next_page_token="should_be_cleared", total_synced=0)
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=cursor),
        _Result(row=updated),
        _Result(),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "aclaud_x")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/connv2_1/sync-cursor/reset")
    assert resp.status_code == 200
    body = resp.json()
    # 即使 mock 返回了 token，端点也应强制清空
    assert body["cursor"]["next_page_token"] is None
    # 验证 UPDATE SQL 设置了 next_page_token=NULL
    update_query, _ = conn.calls[2]
    assert "next_page_token=NULL" in update_query.replace(" ", " ").replace("\n", " ")


@pytest.mark.asyncio
async def test_reset_sync_cursor_sets_total_synced_zero(monkeypatch):
    """重置后 total_synced 应为 0。"""
    cursor = _cursor_row(total_synced=999)
    updated = _cursor_row(next_page_token=None, total_synced=999)  # mock 未清零
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=cursor),
        _Result(row=updated),
        _Result(),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "aclaud_y")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/connv2_1/sync-cursor/reset")
    assert resp.status_code == 200
    body = resp.json()
    # 端点应强制覆盖 total_synced=0
    assert body["cursor"]["total_synced"] == 0
    update_query, _ = conn.calls[2]
    assert "total_synced=0" in update_query.replace("\n", " ")


# ============================================================================
# 9. 同步流程集成（sync 端点调用后落库 sync_run + acl_mapping + cursor + audit）
# ============================================================================


@pytest.mark.asyncio
async def test_sync_persists_sync_run(monkeypatch):
    """sync 端点应在 adapter.incremental_sync 后落库 sync_run。"""
    items = [
        {"source_id": "mock:item:1", "title": "Item 1"},
        {"source_id": "mock:item:2", "title": "Item 2"},
    ]
    adapter = _MockAdapter(items=items, next_cursor="cursor_xyz")
    monkeypatch.setattr(cv2, "_get_adapter", lambda provider: adapter)

    run_row = _sync_run_row(items_synced=2, acl_mappings_count=2)
    mapping_row_1 = _acl_mapping_row(id="aclmap_1", source_entity="mock:item:1")
    mapping_row_2 = _acl_mapping_row(id="aclmap_2", source_entity="mock:item:2")
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row(status="active")),  # SELECT connector
        _Result(row=None),  # SELECT cursor (not exists)
        _Result(row=run_row),  # INSERT sync_run RETURNING
        _Result(row=mapping_row_1),  # INSERT acl_mapping 1 RETURNING
        _Result(),  # INSERT audit 1
        _Result(row=mapping_row_2),  # INSERT acl_mapping 2 RETURNING
        _Result(),  # INSERT audit 2
        _Result(),  # UPDATE sync_run.acl_mappings_count
        _Result(),  # UPSERT cursor
        _Result(),  # UPDATE connector_config_v2 last_cursor
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "op_sync_1")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/connv2_1/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["items_synced"] == 2
    assert body["acl_mappings_count"] == 2
    assert body["operation_id"] == "op_sync_1"
    # 验证 sync_run INSERT 被调用
    insert_run_call = next(
        (c for c in conn.calls if "INSERT INTO connector_sync_run" in c[0]), None
    )
    assert insert_run_call is not None
    assert "completed" in insert_run_call[1]


@pytest.mark.asyncio
async def test_sync_persists_acl_mappings(monkeypatch):
    """sync 端点应为每个 item 落库一条 acl_mapping。"""
    items = [
        {"source_id": "mock:item:1"},
        {"source_id": "mock:item:2"},
    ]
    adapter = _MockAdapter(items=items, next_cursor="cursor_xyz")
    monkeypatch.setattr(cv2, "_get_adapter", lambda provider: adapter)

    run_row = _sync_run_row(items_synced=2)
    mapping_row_1 = _acl_mapping_row(id="m1", source_entity="mock:item:1")
    mapping_row_2 = _acl_mapping_row(id="m2", source_entity="mock:item:2")
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row(status="active")),
        _Result(row=None),
        _Result(row=run_row),
        _Result(row=mapping_row_1),
        _Result(),
        _Result(row=mapping_row_2),
        _Result(),
        _Result(),  # UPDATE sync_run
        _Result(),  # UPSERT cursor
        _Result(),  # UPDATE connector
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "op_x")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/connv2_1/sync")
    assert resp.status_code == 200
    # 应有 2 条 INSERT INTO connector_acl_mapping
    mapping_inserts = [c for c in conn.calls if "INSERT INTO connector_acl_mapping" in c[0]]
    assert len(mapping_inserts) == 2
    # 验证 source_entity 来自每个 item
    assert "mock:item:1" in mapping_inserts[0][1]
    assert "mock:item:2" in mapping_inserts[1][1]


@pytest.mark.asyncio
async def test_sync_updates_cursor(monkeypatch):
    """sync 端点应 UPSERT connector_sync_cursor。"""
    items = [{"source_id": "mock:item:1"}]
    adapter = _MockAdapter(items=items, next_cursor="next_page_xyz")
    monkeypatch.setattr(cv2, "_get_adapter", lambda provider: adapter)

    cursor_row = _cursor_row(next_page_token=None, total_synced=5)
    run_row = _sync_run_row(items_synced=1)
    mapping_row = _acl_mapping_row(id="m1")
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row(status="active")),
        _Result(row=cursor_row),  # SELECT cursor
        _Result(row=run_row),
        _Result(row=mapping_row),
        _Result(),  # audit
        _Result(),  # UPDATE sync_run
        _Result(),  # UPSERT cursor
        _Result(),  # UPDATE connector
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "op_c")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/connv2_1/sync")
    assert resp.status_code == 200
    # 找到 UPSERT cursor 的调用
    upsert_call = next(
        (c for c in conn.calls if "INSERT INTO connector_sync_cursor" in c[0]), None
    )
    assert upsert_call is not None
    assert "ON CONFLICT (connector_id) DO UPDATE" in upsert_call[0]
    # total_synced = 5 (prev) + 1 (new) = 6
    assert 6 in upsert_call[1]
    assert "next_page_xyz" in upsert_call[1]


@pytest.mark.asyncio
async def test_sync_writes_audit_for_each_mapping(monkeypatch):
    """sync 端点应为每条 acl_mapping 写一条 action='mapped' 的审计。"""
    items = [
        {"source_id": "mock:item:1"},
        {"source_id": "mock:item:2"},
        {"source_id": "mock:item:3"},
    ]
    adapter = _MockAdapter(items=items, next_cursor="cursor_xyz")
    monkeypatch.setattr(cv2, "_get_adapter", lambda provider: adapter)

    run_row = _sync_run_row(items_synced=3)
    mapping_rows = [
        _acl_mapping_row(id=f"m{i}", source_entity=f"mock:item:{i}")
        for i in range(1, 4)
    ]
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row(status="active")),
        _Result(row=None),
        _Result(row=run_row),
        _Result(row=mapping_rows[0]),
        _Result(),  # audit 1
        _Result(row=mapping_rows[1]),
        _Result(),  # audit 2
        _Result(row=mapping_rows[2]),
        _Result(),  # audit 3
        _Result(),  # UPDATE sync_run
        _Result(),  # UPSERT cursor
        _Result(),  # UPDATE connector
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "op_a")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/connv2_1/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["acl_mappings_count"] == 3
    audit_inserts = [c for c in conn.calls if "INSERT INTO connector_acl_audit" in c[0]]
    assert len(audit_inserts) == 3
    for audit_q, audit_p in audit_inserts:
        assert "mapped" in audit_p


# ============================================================================
# 10. 边界
# ============================================================================


@pytest.mark.asyncio
async def test_sync_handles_unique_constraint_conflict(monkeypatch):
    """UNIQUE 冲突时跳过该映射，不影响整体 sync。"""
    items = [{"source_id": "mock:item:1"}]
    adapter = _MockAdapter(items=items, next_cursor="cursor_xyz")
    monkeypatch.setattr(cv2, "_get_adapter", lambda provider: adapter)

    class _DuplicateConn(_RecordingConnection):
        async def execute(self, query, params=()):
            self.calls.append((query, params))
            if "INSERT INTO connector_acl_mapping" in query:
                # 模拟 UNIQUE 冲突
                raise Exception("duplicate key value violates unique constraint")
            if self._idx < len(self._results):
                r = self._results[self._idx]
                self._idx += 1
                return r
            return _Result()

    run_row = _sync_run_row(items_synced=1, acl_mappings_count=0)
    conn = _DuplicateConn(results=[
        _Result(row=_connector_row(status="active")),
        _Result(row=None),
        _Result(row=run_row),
        # INSERT acl_mapping 抛异常（不消耗 result）
        _Result(),  # UPDATE sync_run 不应被调用 (acl_mappings_count=0)
        _Result(),  # UPSERT cursor
        _Result(),  # UPDATE connector
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "op_dup")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/connv2_1/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["items_synced"] == 1
    assert body["acl_mappings_count"] == 0


@pytest.mark.asyncio
async def test_sync_connector_not_found_returns_404(monkeypatch):
    """sync 时 connector 不存在 → 404。"""
    conn = _RecordingConnection(results=[_Result(row=None)])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/missing/sync")
    assert resp.status_code == 404


# ============================================================================
# 11. 鉴权
# ============================================================================


@pytest.mark.asyncio
async def test_sync_history_requires_auth():
    """未认证访问 sync-history 应 401。"""
    app = _app(actor=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/sync-history")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_apply_acl_mapping_viewer_role_403():
    """viewer 角色（无 connector:write）应 403。"""
    app = _app(actor=_actor(role="viewer", capabilities=()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/connectors/v2/connv2_1/acl-mappings/aclmap_1/apply"
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_sync_cursor_viewer_can_read(monkeypatch):
    """viewer 角色可以读取 sync-cursor。"""
    cursor = _cursor_row()
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row()),
        _Result(row=cursor),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor(role="viewer", capabilities=()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/connectors/v2/connv2_1/sync-cursor")
    assert resp.status_code == 200


# ============================================================================
# 12. 路由注册
# ============================================================================


def test_router_exposes_acl_sync_endpoints():
    """验证 7 个新端点已注册到 router。"""
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in cv2.router.routes}
    assert ("/api/v1/connectors/v2/{connector_id}/sync-history", ("GET",)) in paths
    assert ("/api/v1/connectors/v2/{connector_id}/sync-history/{sync_id}", ("GET",)) in paths
    assert ("/api/v1/connectors/v2/{connector_id}/acl-mappings", ("GET",)) in paths
    assert (
        "/api/v1/connectors/v2/{connector_id}/acl-mappings/{mapping_id}/apply",
        ("POST",),
    ) in paths
    assert (
        "/api/v1/connectors/v2/{connector_id}/acl-mappings/{mapping_id}/reject",
        ("POST",),
    ) in paths
    assert ("/api/v1/connectors/v2/{connector_id}/sync-cursor", ("GET",)) in paths
    assert (
        "/api/v1/connectors/v2/{connector_id}/sync-cursor/reset",
        ("POST",),
    ) in paths


# ============================================================================
# 13. Pydantic 模型
# ============================================================================


def test_acl_mapping_reject_request_validates_reason():
    """ACLMappingRejectRequest 必须包含非空 reason。"""
    req = cv2.ACLMappingRejectRequest(reason="duplicate")
    assert req.reason == "duplicate"
    # 空 reason 不应通过
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        cv2.ACLMappingRejectRequest(reason="")
    with pytest.raises(ValidationError):
        cv2.ACLMappingRejectRequest()


def test_sync_cursor_reset_request_optional_reason():
    """SyncCursorResetRequest 的 reason 可选。"""
    req = cv2.SyncCursorResetRequest()
    assert req.reason is None
    req2 = cv2.SyncCursorResetRequest(reason="manual reset")
    assert req2.reason == "manual reset"


# ============================================================================
# 14. _assert_connector_in_workspace 辅助函数
# ============================================================================


@pytest.mark.asyncio
async def test_assert_connector_in_workspace_returns_row(monkeypatch):
    """connector 存在时返回 row。"""
    conn = _RecordingConnection(results=[_Result(row=_connector_row())])
    row = await cv2._assert_connector_in_workspace(conn, "connv2_1", "wsp_test")
    assert row["id"] == "connv2_1"


@pytest.mark.asyncio
async def test_assert_connector_in_workspace_raises_404(monkeypatch):
    """connector 不存在时 404。"""
    conn = _RecordingConnection(results=[_Result(row=None)])
    with pytest.raises(HTTPException) as exc:
        await cv2._assert_connector_in_workspace(conn, "missing", "wsp_test")
    assert exc.value.status_code == 404


# ============================================================================
# 15. 同步失败落库
# ============================================================================


@pytest.mark.asyncio
async def test_sync_records_failed_run_on_adapter_error(monkeypatch):
    """adapter.incremental_sync 抛异常时，应落库 status='failed' 的 sync_run。"""

    class _FailingAdapter(cv2.ConnectorAdapter):
        async def authenticate(self, config):
            return {"adapter": "fail", "authenticated": True}

        async def discover(self, config):
            return []

        async def incremental_sync(self, config, cursor):
            raise RuntimeError("upstream down")

        async def map_acl(self, config, source_item):
            return {}

        async def propagate_deletion(self, config, source_id):
            return {}

    monkeypatch.setattr(cv2, "_get_adapter", lambda provider: _FailingAdapter())

    conn = _RecordingConnection(results=[
        _Result(row=_connector_row(status="active")),
        _Result(row=None),  # SELECT cursor
        _Result(),  # INSERT failed sync_run
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))
    monkeypatch.setattr(cv2, "new_id", lambda prefix: "op_fail")

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/connv2_1/sync")
    assert resp.status_code == 500
    # 应有 INSERT INTO connector_sync_run with status='failed'
    insert_calls = [c for c in conn.calls if "INSERT INTO connector_sync_run" in c[0]]
    assert len(insert_calls) == 1
    assert "failed" in insert_calls[0][1]


@pytest.mark.asyncio
async def test_sync_inactive_connector_returns_409(monkeypatch):
    """非 active 的 connector 同步应 409。"""
    conn = _RecordingConnection(results=[
        _Result(row=_connector_row(status="pending")),
    ])
    monkeypatch.setattr(cv2, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/connectors/v2/connv2_1/sync")
    assert resp.status_code == 409
