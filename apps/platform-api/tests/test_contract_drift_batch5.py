"""契约源码-设计漂移治理第五批：契约回归测试。

覆盖《720-实施级API操作与消息契约注册表》对响应结构的约束：
- 列表端点：``ListResponse<T>`` 必须包含 ``data``/``next_cursor``/``has_more``/``meta``，保留 ``items`` 向后兼容
- 异步端点：``OperationAccepted`` 必须包含 ``operation_id``/``status``/``status_url``/``submitted_at``
- 单资源端点：顶层暴露 DTO 字段，保留旧包装键

本文件仅做契约形状校验，不依赖真实数据库；通过 monkeypatch 替换连接池即可。
覆盖第五批修复的端点：enterprise_rbac、work、security/router、approvals。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from workama_platform.core import Actor
from workama_platform.modules import approvals, enterprise_rbac, work
from workama_platform.modules.security import router as security_router


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
    """简单连接 mock：所有 execute 返回同一组 rows，可被子类按 SQL 关键字细化。"""

    def __init__(self, rows: list[Any] | None = None, row: Any = None) -> None:
        self._rows = rows or []
        self._row = row

    def transaction(self):
        return _Transaction(self)

    async def execute(self, statement, params=None):
        # RETURNING 走单行返回；避免误命中 "LIMIT 100" 之类的子串
        if "RETURNING" in statement:
            return _Result(row=self._row)
        return _Result(rows=self._rows)

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    """模拟 psycopg AsyncConnectionPool：``connection()`` 返回连接上下文管理器。"""

    def __init__(self, connection: _ListConnection) -> None:
        self._connection = connection

    def connection(self):
        return self._connection


def _owner(workspace_id: str = "wsp_1", org_id: str = "org_1") -> Actor:
    return Actor(
        user_id="usr_owner",
        workspace_id=workspace_id,
        org_id=org_id,
        role="owner",
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
        capabilities=("*",),
        actor_type="user",
        auth_strength=2,
    )


def _admin(workspace_id: str = "wsp_1", org_id: str = "org_1") -> Actor:
    return Actor(
        user_id="usr_admin",
        workspace_id=workspace_id,
        org_id=org_id,
        role="admin",
        email="admin@example.test",
        display_name="Admin",
        onboarding_completed=True,
        capabilities=("*",),
        actor_type="user",
        auth_strength=2,
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


# ---------------------------------------------------------------------------
# enterprise_rbac.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_groups_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "grp_1",
            "org_id": "org_1",
            "name": "engineers",
            "external_id": None,
            "source": "manual",
            "status": "active",
            "version": 1,
            "created_by": "usr_owner",
            "created_at": now,
            "updated_at": now,
            "member_count": 3,
        }
    ]
    monkeypatch.setattr(enterprise_rbac, "pool", _Pool(_ListConnection(rows=rows)))
    result = await enterprise_rbac.list_groups(_owner())
    _assert_listresponse_envelope(result)
    # 保留旧字段 count 向后兼容
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "grp_1"


@pytest.mark.asyncio
async def test_list_roles_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "rol_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "name": "Engineer",
            "description": "Engineering role",
            "capabilities": ["dataset:read"],
            "system": False,
            "status": "active",
            "version": 1,
            "created_by": "usr_owner",
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(enterprise_rbac, "pool", _Pool(_ListConnection(rows=rows)))
    result = await enterprise_rbac.list_roles(_owner())
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "rol_1"


@pytest.mark.asyncio
async def test_list_role_bindings_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "rbd_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "role_id": "rol_1",
            "role_name": "Engineer",
            "subject_type": "user",
            "subject_id": "usr_alice",
            "resource_type": "workspace",
            "resource_id": "wsp_1",
            "conditions": {},
            "status": "active",
            "expires_at": None,
            "version": 1,
            "created_by": "usr_owner",
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(enterprise_rbac, "pool", _Pool(_ListConnection(rows=rows)))
    result = await enterprise_rbac.list_role_bindings(_owner())
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "rbd_1"


@pytest.mark.asyncio
async def test_list_service_account_policies_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "sap_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "service_account_id": "sa_1",
            "allowed_scopes": ["dataset:read"],
            "allowed_ip_cidrs": ["10.0.0.0/24"],
            "status": "active",
            "expires_at": None,
            "version": 1,
            "created_by": "usr_owner",
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(enterprise_rbac, "pool", _Pool(_ListConnection(rows=rows)))
    result = await enterprise_rbac.list_service_account_policies(_owner())
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "sap_1"


@pytest.mark.asyncio
async def test_list_auth_strength_policies_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "asp_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "operation": "external_app.invoke",
            "required_auth_strength": 2,
            "status": "active",
            "version": 1,
            "created_by": "usr_owner",
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(enterprise_rbac, "pool", _Pool(_ListConnection(rows=rows)))
    result = await enterprise_rbac.list_auth_strength_policies(_owner())
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "asp_1"


# ---------------------------------------------------------------------------
# work.py 契约测试
# ---------------------------------------------------------------------------


_PLAN_ROW = {
    "id": "pln_1",
    "workspace_id": "wsp_1",
    "session_id": "ses_1",
    "title": "Plan 1",
    "objective": "Demo",
    "status": "draft",
    "last_event_seq": 0,
    "created_by": "usr_owner",
    "created_at": datetime.now(UTC),
    "updated_at": datetime.now(UTC),
}


class _WorkListConnection(_ListConnection):
    """work.py 列表端点的 mock：``_owned_plan`` 查询返回 plan_row，列表查询返回 rows。"""

    def __init__(
        self,
        rows: list[Any] | None = None,
        plan_row: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(rows=rows)
        self._plan_row = plan_row

    async def execute(self, statement, params=None):
        # _owned_plan 内部按 id+workspace 查询 work_plan，返回单行
        if self._plan_row is not None and "FROM work_plan" in statement and "WHERE id=" in statement:
            return _Result(row=self._plan_row)
        return await super().execute(statement, params)


@pytest.mark.asyncio
async def test_work_list_plans_returns_listresponse_envelope(monkeypatch):
    rows = [_PLAN_ROW]
    monkeypatch.setattr(work, "pool", _Pool(_ListConnection(rows=rows)))
    result = await work.list_plans(_owner())
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "pln_1"


@pytest.mark.asyncio
async def test_work_list_tasks_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "tsk_1",
            "workspace_id": "wsp_1",
            "plan_id": "pln_1",
            "title": "Task 1",
            "description": "",
            "position": 1,
            "status": "todo",
            "created_by": "usr_owner",
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(work, "pool", _Pool(_WorkListConnection(rows=rows, plan_row=_PLAN_ROW)))
    result = await work.list_tasks("pln_1", _owner())
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "tsk_1"


@pytest.mark.asyncio
async def test_work_list_events_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "evt_1",
            "workspace_id": "wsp_1",
            "plan_id": "pln_1",
            "task_id": "tsk_1",
            "seq": 1,
            "event_type": "task.created",
            "payload": {"foo": "bar"},
            "created_by": "usr_owner",
            "created_at": now,
        }
    ]
    monkeypatch.setattr(work, "pool", _Pool(_WorkListConnection(rows=rows, plan_row=_PLAN_ROW)))
    result = await work.list_events("pln_1", _owner())
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "evt_1"


@pytest.mark.asyncio
async def test_work_list_sources_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "cit_1",
            "plan_id": "pln_1",
            "task_id": "tsk_1",
            "source_type": "web",
            "url": "https://example.test",
            "title": "Example",
            "excerpt": "Excerpt",
            "content_sha256": "a" * 64,
            "created_by": "usr_owner",
            "created_at": now,
        }
    ]
    monkeypatch.setattr(work, "pool", _Pool(_WorkListConnection(rows=rows, plan_row=_PLAN_ROW)))
    result = await work.list_sources("pln_1", _owner())
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "cit_1"


@pytest.mark.asyncio
async def test_work_list_artifacts_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "waf_1",
            "workspace_id": "wsp_1",
            "plan_id": "pln_1",
            "task_id": "tsk_1",
            "artifact_id": "art_1",
            "name": "report.pdf",
            "kind": "document",
            "content_type": "application/pdf",
            "s3_key": "s3://bucket/key",
            "size_bytes": 1024,
            "content_sha256": "a" * 64,
            "status": "ready",
            "preview": None,
            "created_by": "usr_owner",
            "created_at": now,
        }
    ]
    monkeypatch.setattr(work, "pool", _Pool(_WorkListConnection(rows=rows, plan_row=_PLAN_ROW)))
    result = await work.list_work_artifacts("pln_1", _owner())
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "waf_1"


# ---------------------------------------------------------------------------
# security/router.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_security_list_prompts_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "prm_1",
            "name": "safety.check",
            "version": 1,
            "content": "hello",
            "checksum": "a" * 64,
            "status": "draft",
            "created_at": now,
            "published_at": None,
            "eval_status": None,
            "eval_failures": None,
        }
    ]
    monkeypatch.setattr(security_router, "pool", _Pool(_ListConnection(rows=rows)))
    result = await security_router.list_prompts(_admin())
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "prm_1"


# ---------------------------------------------------------------------------
# approvals.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approvals_list_approvals_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "apr_1",
            "workspace_id": "wsp_1",
            "session_id": "ses_1",
            "call_id": "call_1",
            "requester_id": "usr_alice",
            "tool_name": "shell",
            "action_hash": "h" * 64,
            "risk": "A2",
            "preview": {},
            "status": "pending",
            "expires_at": now,
            "created_at": now,
        }
    ]
    monkeypatch.setattr(approvals, "pool", _Pool(_ListConnection(rows=rows)))
    result = await approvals.list_approvals(_owner(), None)
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "apr_1"


@pytest.mark.asyncio
async def test_approvals_list_grants_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "grt_1",
            "workspace_id": "wsp_1",
            "session_id": "ses_1",
            "tool_name": "shell",
            "scope": "session",
            "granted_by": "usr_owner",
            "granted_at": now,
            "expires_at": now,
            "status": "active",
            "created_at": now,
        }
    ]
    monkeypatch.setattr(approvals, "pool", _Pool(_ListConnection(rows=rows)))
    result = await approvals.list_grants(_owner())
    _assert_listresponse_envelope(result)
    assert result["meta"]["count"] == 1
    assert result["data"][0]["id"] == "grt_1"
