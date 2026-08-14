"""契约源码-设计漂移治理第二批：契约回归测试。

覆盖《720-实施级API操作与消息契约注册表》对响应结构的约束：
- 列表端点：``ListResponse<T>`` 必须包含 ``data``/``next_cursor``/``has_more``/``meta``，保留 ``items`` 向后兼容
- 异步端点：``OperationAccepted`` 必须包含 ``operation_id``/``status``/``status_url``/``submitted_at``
- 单资源端点：顶层暴露 DTO 字段，保留旧包装键

本文件仅做契约形状校验，不依赖真实数据库；通过 monkeypatch 替换连接池即可。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from workama_platform.core import Actor
from workama_platform.modules import audit_exports, connectors, design, knowledge, workspaces


# ---------------------------------------------------------------------------
# 通用 mock 基础设施
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, row: Any = None, rows: list[Any] | None = None) -> None:
        self._row = row
        self._rows = rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Transaction:
    def __init__(self, connection) -> None:
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ListConnection:
    """简单连接 mock：所有 execute 返回同一组 rows。"""

    def __init__(self, rows: list[Any] | None = None, row: Any = None) -> None:
        self._rows = rows or []
        self._row = row

    def transaction(self):
        return _Transaction(self)

    async def execute(self, statement, params=None):
        if "RETURNING" in statement or "LIMIT 1" in statement:
            return _Result(row=self._row)
        return _Result(rows=self._rows)

    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _owner(workspace_id: str = "wsp_1") -> Actor:
    return Actor(
        user_id="usr_owner",
        workspace_id=workspace_id,
        org_id="org_1",
        role="owner",
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
        capabilities=("*",),
    )


def _assert_listresponse_envelope(result: dict[str, Any]) -> None:
    """校验 ListResponse<T> 契约形状。"""
    assert "items" in result, "向后兼容字段 items 必须保留"
    assert "data" in result, "契约字段 data 必须存在"
    assert result["data"] == result["items"], "data 与 items 必须指向同一份数据"
    assert "next_cursor" in result
    assert "has_more" in result
    assert isinstance(result["has_more"], bool)
    assert "meta" in result and "request_id" in result["meta"]


def _assert_operation_accepted_envelope(result: dict[str, Any]) -> None:
    """校验 OperationAccepted 契约形状。"""
    assert "operation_id" in result
    assert "status" in result
    assert result["status"] in {"queued", "running", "succeeded", "failed", "cancelled", "unsupported", "delivered", "pending_external"}
    assert "status_url" in result and isinstance(result["status_url"], str)
    assert "submitted_at" in result


# ---------------------------------------------------------------------------
# connectors.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_connectors_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "conn_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "name": "mock",
            "provider": "mock",
            "auth_mode": "none",
            "endpoint_ref": "mock://connector/x",
            "manifest": {},
            "credential_hash": None,
            "credential_ref": None,
            "status": "active",
            "enabled": True,
            "source_cursor": {},
            "last_sync_at": None,
            "version": 1,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    ]
    monkeypatch.setattr(connectors.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await connectors.list_connectors(_owner(), limit=50)
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "conn_1"


@pytest.mark.asyncio
async def test_list_sync_runs_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "cnrun_1",
            "connector_id": "conn_1",
            "workspace_id": "wsp_1",
            "mode": "full",
            "idempotency_key": "k",
            "status": "succeeded",
            "execution_status": "executed",
            "executed": True,
            "source_cursor_before": {},
            "source_cursor_after": {},
            "documents_seen": 0,
            "documents_upserted": 0,
            "documents_tombstoned": 0,
            "documents_revoked": 0,
            "error_code": None,
            "error_message": None,
            "created_at": datetime.now(UTC),
            "completed_at": None,
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            if "SELECT * FROM pf_connector" in statement and "LIMIT" not in statement:
                return _Result(row=rows[0])  # _get_connector 返回值
            return await super().execute(statement, params)

    monkeypatch.setattr(connectors.pool, "connection", lambda: _Conn(rows=rows))
    result = await connectors.list_sync_runs("conn_1", _owner(), limit=50)
    _assert_listresponse_envelope(result)


@pytest.mark.asyncio
async def test_list_connector_documents_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "cdoc_1",
            "connector_id": "conn_1",
            "workspace_id": "wsp_1",
            "source_id": "source:1",
            "source_version": "1",
            "source_etag": None,
            "source_updated_at": None,
            "title": "Doc",
            "content": None,
            "content_ref": None,
            "content_sha256": None,
            "acl": {},
            "status": "active",
            "version": 1,
            "updated_at": datetime.now(UTC),
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            # _get_connector 运行 SELECT * FROM pf_connector WHERE id=%s ...
            if "SELECT * FROM pf_connector WHERE id=" in statement:
                return _Result(row={"id": "conn_1", "workspace_id": "wsp_1", "status": "active"})
            if "FROM pf_connector_identity_mapping" in statement and "SELECT external_id" in statement:
                return _Result(rows=[])
            return await super().execute(statement, params)

    monkeypatch.setattr(connectors.pool, "connection", lambda: _Conn(rows=rows))
    result = await connectors.list_connector_documents("conn_1", _owner(), visible_only=False, limit=100)
    _assert_listresponse_envelope(result)


@pytest.mark.asyncio
async def test_list_identity_mappings_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "cmap_1",
            "connector_id": "conn_1",
            "workspace_id": "wsp_1",
            "external_type": "group",
            "external_id": "ext-1",
            "principal_type": "user",
            "principal_id": "usr_1",
            "enabled": True,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            # _get_connector 运行 SELECT * FROM pf_connector WHERE id=%s ...
            if "SELECT * FROM pf_connector WHERE id=" in statement:
                return _Result(row={"id": "conn_1", "workspace_id": "wsp_1", "status": "active"})
            return await super().execute(statement, params)

    monkeypatch.setattr(connectors.pool, "connection", lambda: _Conn(rows=rows))
    result = await connectors.list_identity_mappings("conn_1", _owner())
    _assert_listresponse_envelope(result)


@pytest.mark.asyncio
async def test_get_connector_exposes_top_level_dto(monkeypatch):
    row = {
        "id": "conn_1",
        "org_id": "org_1",
        "workspace_id": "wsp_1",
        "name": "mock",
        "provider": "mock",
        "auth_mode": "none",
        "endpoint_ref": "mock://connector/x",
        "manifest": {},
        "credential_hash": None,
        "credential_ref": None,
        "status": "active",
        "enabled": True,
        "source_cursor": {},
        "last_sync_at": None,
        "version": 1,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            # _get_connector 运行 SELECT * FROM pf_connector WHERE id=%s ...
            if "SELECT * FROM pf_connector WHERE id=" in statement:
                return _Result(row=self._row)
            return await super().execute(statement, params)

    monkeypatch.setattr(connectors.pool, "connection", lambda: _Conn(row=row))
    result = await connectors.get_connector("conn_1", _owner())
    # 保留旧包装
    assert "connector" in result
    # 顶层暴露 DTO 字段
    assert result["id"] == "conn_1"
    assert result["name"] == "mock"


# ---------------------------------------------------------------------------
# design.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_projects_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "dsg_1",
            "name": "Project",
            "slug": "project",
            "description": None,
            "canvas_width": 1024,
            "canvas_height": 768,
            "status": "active",
            "version": 1,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    ]
    monkeypatch.setattr(design.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await design.list_projects(_owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "dsg_1"


@pytest.mark.asyncio
async def test_list_assets_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "dsgasset_1",
            "name": "asset",
            "kind": "image",
            "content_type": "image/png",
            "artifact_ref": "design://artifact/dsgasset_1",
            "content_sha256": "a" * 64,
            "size_bytes": 1,
            "provenance": {},
            "provenance_hash": "b" * 64,
            "parent_asset_ids": [],
            "status": "ready",
            "created_at": datetime.now(UTC),
        }
    ]
    monkeypatch.setattr(design.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await design.list_assets("dsg_1", _owner(), limit=100)
    _assert_listresponse_envelope(result)


# ---------------------------------------------------------------------------
# audit_exports.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_audit_events_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "aud_1",
            "sequence": 100,
            "event_type": "role.updated",
            "actor_user_id": "usr_1",
            "resource_type": "role",
            "resource_id": "role_1",
            "details": {},
            "record_hash": "h",
            "previous_hash": "p",
            "occurred_at": datetime.now(UTC),
        }
    ]
    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            # _backfill_audit_chain 查询 id_enterprise_audit_event，返回空使其提前返回
            if "id_enterprise_audit_event" in statement:
                return _Result(rows=[])
            return await super().execute(statement, params)

    monkeypatch.setattr(audit_exports.pool, "connection", lambda: _Conn(rows=rows))
    query = audit_exports.AuditQuery(limit=10)
    result = await audit_exports.list_audit_events(_owner(), query)
    _assert_listresponse_envelope(result)
    # next_cursor 仍按既有语义保留
    assert result["next_cursor"] == "100"


@pytest.mark.asyncio
async def test_list_audit_exports_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "aexp_1",
            "status": "queued",
            "format": "jsonl",
            "record_count": 0,
            "content_hash": "",
            "manifest": {},
            "created_at": datetime.now(UTC),
            "expires_at": None,
        }
    ]
    monkeypatch.setattr(audit_exports.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await audit_exports.list_audit_exports(_owner())
    _assert_listresponse_envelope(result)


@pytest.mark.asyncio
async def test_list_siem_deliveries_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "siemd_1",
            "config_id": "siem_1",
            "event_type": "role.updated",
            "idempotency_key": "k",
            "payload_hash": "h",
            "status": "delivered",
            "attempt": 1,
            "next_attempt_at": None,
            "response_code": 200,
            "error_code": None,
            "signature": "sig",
            "response_summary": "ok",
            "claimed_at": None,
            "delivered_at": datetime.now(UTC),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    ]
    monkeypatch.setattr(audit_exports.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await audit_exports.list_siem_deliveries(_owner())
    _assert_listresponse_envelope(result)


@pytest.mark.asyncio
async def test_create_audit_export_operation_returns_operation_accepted(monkeypatch):
    now = datetime.now(UTC)
    row = {
        "id": "aexp_1",
        "status": "queued",
        "format": "jsonl",
        "record_count": 0,
        "content_hash": "",
        "manifest": {},
        "created_at": now,
        "expires_at": None,
    }

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            if "INSERT INTO sec_audit_export" in statement:
                return _Result(row=row)
            return _Result(rows=[])

        async def commit(self):
            return None

    monkeypatch.setattr(audit_exports.pool, "connection", lambda: _Conn(row=row))
    body = audit_exports.AuditExportRequest(format="jsonl", action="role.updated")
    result = await audit_exports.create_audit_export_operation(_owner(), body)
    _assert_operation_accepted_envelope(result)
    # operation_id 由 new_id("aexp") 生成，非 row["id"]
    assert isinstance(result["operation_id"], str) and result["operation_id"].startswith("aexp")
    assert result["status"] == "queued"
    assert result["status_url"].endswith(f"/api/v1/audit-exports/{result['operation_id']}")
    # 旧字段保留
    assert result["execution_mode"] == "controlled_mock"


# ---------------------------------------------------------------------------
# knowledge.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_datasets_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "dts_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "name": "kb",
            "description": None,
            "embedding_model": "workama-embed",
            "retrieval_config": {},
            "active_generation_id": "idx_1",
            "embedding_profile": {},
            "stats": {"document_count": 0, "chunk_count": 0, "size_bytes": 0},
            "status": "active",
            "version": 1,
            "document_count": 0,
            "chunk_count": 0,
            "size_bytes": 0,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "deleted_at": None,
        }
    ]
    monkeypatch.setattr(knowledge.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await knowledge.list_datasets(_owner(), limit=50)
    _assert_listresponse_envelope(result)


@pytest.mark.asyncio
async def test_list_dataset_documents_returns_listresponse_envelope(monkeypatch):
    dataset_row = {
        "id": "dts_1",
        "workspace_id": "wsp_1",
        "active_generation_id": "idx_1",
        "status": "active",
        "version": 1,
    }
    doc_rows = [
        {
            "id": "doc_1",
            "dataset_id": "dts_1",
            "workspace_id": "wsp_1",
            "name": "doc.md",
            "source": "upload",
            "source_url": None,
            "s3_key": "k",
            "mime": "text/markdown",
            "size_bytes": 1,
            "content_sha256": "a" * 64,
            "status": "indexed",
            "version": 1,
            "error": None,
            "chunk_count": 0,
            "indexed_at": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "deleted_at": None,
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            if "SELECT * FROM pf_dataset" in statement and "LIMIT" not in statement:
                return _Result(row=dataset_row)
            return _Result(rows=doc_rows)

    monkeypatch.setattr(knowledge.pool, "connection", lambda: _Conn(rows=doc_rows, row=dataset_row))
    result = await knowledge.list_dataset_documents("dts_1", _owner(), limit=100)
    _assert_listresponse_envelope(result)


@pytest.mark.asyncio
async def test_list_index_generations_returns_listresponse_envelope(monkeypatch):
    dataset_row = {
        "id": "dts_1",
        "workspace_id": "wsp_1",
        "active_generation_id": "idx_1",
        "status": "active",
        "version": 1,
    }
    gen_rows = [
        {
            "id": "idx_1",
            "dataset_id": "dts_1",
            "workspace_id": "wsp_1",
            "generation": 1,
            "embedding_profile": {},
            "status": "active",
            "created_by": "usr_1",
            "activated_at": datetime.now(UTC),
            "completed_at": datetime.now(UTC),
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            if "SELECT * FROM pf_dataset" in statement and "LIMIT" not in statement:
                return _Result(row=dataset_row)
            return _Result(rows=gen_rows)

    monkeypatch.setattr(knowledge.pool, "connection", lambda: _Conn(rows=gen_rows, row=dataset_row))
    result = await knowledge.list_index_generations("dts_1", _owner())
    _assert_listresponse_envelope(result)


# ---------------------------------------------------------------------------
# workspaces.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_organizations_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "org_1",
            "name": "Org",
            "owner_user_id": "usr_owner",
            "created_at": datetime.now(UTC),
            "role": "owner",
        }
    ]
    monkeypatch.setattr(workspaces.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await workspaces.list_organizations(_owner())
    _assert_listresponse_envelope(result)


@pytest.mark.asyncio
async def test_list_workspaces_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "wsp_1",
            "org_id": "org_1",
            "name": "WS",
            "slug": "ws",
            "settings": {},
            "status": "active",
            "created_by": "usr_owner",
            "created_at": datetime.now(UTC),
            "role": "owner",
        }
    ]
    monkeypatch.setattr(workspaces.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await workspaces.list_workspaces(_owner())
    _assert_listresponse_envelope(result)


@pytest.mark.asyncio
async def test_list_invitations_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "inv_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "email_normalized": "invitee@example.test",
            "role": "member",
            "idempotency_key": None,
            "invited_by": "usr_owner",
            "expires_at": datetime.now(UTC),
            "status": "pending",
            "accepted_by": None,
            "accepted_at": None,
            "revoked_at": None,
            "created_at": datetime.now(UTC),
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            # _workspace_access 运行 SELECT w.id, ... FROM id_workspace w JOIN id_org o ...
            if "FROM id_workspace w" in statement and "JOIN id_org" in statement:
                return _Result(row={"id": "wsp_1", "org_id": "org_1", "role": "owner"})
            return await super().execute(statement, params)

    monkeypatch.setattr(workspaces.pool, "connection", lambda: _Conn(rows=rows))
    result = await workspaces.list_invitations("wsp_1", _owner())
    _assert_listresponse_envelope(result)
