"""契约源码-设计漂移治理第三批：契约回归测试。

覆盖《720-实施级API操作与消息契约注册表》对响应结构的约束：
- 列表端点：``ListResponse<T>`` 必须包含 ``data``/``next_cursor``/``has_more``/``meta``，保留 ``items`` 向后兼容
- 异步端点：``OperationAccepted`` 必须包含 ``operation_id``/``status``/``status_url``/``submitted_at``
- 单资源端点：顶层暴露 DTO 字段，保留旧包装键

本文件仅做契约形状校验，不依赖真实数据库；通过 monkeypatch 替换连接池即可。
覆盖第三批修复的端点：moderation、workflows、memory、notification、session、billing、subscriptions。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from workama_platform.core import Actor
from workama_platform.modules import memory, moderation, subscriptions, workflows
from workama_platform.modules.billing import router as billing_router
from workama_platform.modules.notification import router as notification_router
from workama_platform.modules.session import router as session_router


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
        # 仅 RETURNING 走单行返回；不使用 "LIMIT 1" 子串匹配以避免误命中 "LIMIT 100"
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


# ---------------------------------------------------------------------------
# moderation.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_moderation_policies_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    policy_row = {
        "id": "mpo_1",
        "workspace_id": "wsp_1",
        "name": "default",
        "description": "",
        "default_input_action": "log",
        "default_output_action": "block",
        "status": "active",
        "version": 1,
        "created_by": "usr_owner",
        "updated_by": "usr_owner",
        "created_at": now,
        "updated_at": now,
    }

    class _ModerationConn(_ListConnection):
        async def execute(self, statement, params=None):
            # _get_policy 内部按 id 查询单条策略
            if "FROM sec_moderation_policy WHERE id=" in statement:
                return _Result(row=policy_row)
            # 规则查询返回空列表，避免污染 _policy_response
            if "FROM sec_moderation_rule" in statement:
                return _Result(rows=[])
            return await super().execute(statement, params)

    monkeypatch.setattr(moderation.pool, "connection", lambda: _ModerationConn(rows=[policy_row]))
    result = await moderation.list_moderation_policies(_owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "mpo_1"
    assert result["meta"]["count"] == 1


@pytest.mark.asyncio
async def test_list_moderation_logs_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    audit_rows = [
        {
            "id": "mda_1",
            "workspace_id": "wsp_1",
            "policy_id": "mpo_1",
            "policy_version": 1,
            "actor_id": "usr_owner",
            "direction": "output",
            "action": "block",
            "matched_rule_ids": ["mrl_1"],
            "rule_hits": [{"rule_id": "mrl_1", "kind": "sensitive_word"}],
            "content_hash": "hash",
            "request_id": None,
            "created_at": now,
        }
    ]

    class _ModerationLogConn(_ListConnection):
        async def execute(self, statement, params=None):
            # 旧版 sec_moderation_log 表在测试 fake 中不存在，触发 AssertionError 回退分支
            if "FROM sec_moderation_log" in statement:
                raise AssertionError("table not modeled in fake")
            return await super().execute(statement, params)

    monkeypatch.setattr(moderation.pool, "connection", lambda: _ModerationLogConn(rows=audit_rows))
    result = await moderation.list_moderation_logs(_owner(), limit=50)
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "mda_1"
    assert result["meta"]["count"] == 1


# ---------------------------------------------------------------------------
# workflows.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_assistants_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "ast_1",
            "name": "Helper",
            "description": "",
            "status": "active",
            "current_version_id": None,
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(workflows.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await workflows.list_assistants(_owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "ast_1"


@pytest.mark.asyncio
async def test_list_assistant_runs_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    assistant_row = {
        "id": "ast_1",
        "name": "Helper",
        "description": "",
        "status": "active",
        "current_version_id": None,
        "created_at": now,
        "updated_at": now,
    }
    run_rows = [
        {
            "id": "run_1",
            "app_id": "ast_1",
            "app_type": "assistant",
            "version_id": None,
            "actor_id": "usr_owner",
            "trigger": "api",
            "status": "succeeded",
            "input_meta": {},
            "output_meta": {},
            "error": None,
            "credits": 1,
            "duration_ms": 100,
            "created_at": now,
            "started_at": now,
            "completed_at": now,
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            # _assistant 内部查询 pf_assistant WHERE id=...
            if "SELECT * FROM pf_assistant WHERE id=" in statement:
                return _Result(row=assistant_row)
            return await super().execute(statement, params)

    monkeypatch.setattr(workflows.pool, "connection", lambda: _Conn(rows=run_rows))
    result = await workflows.list_assistant_runs("ast_1", _owner(), limit=50)
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "run_1"


@pytest.mark.asyncio
async def test_list_assistant_run_events_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    assistant_row = {
        "id": "ast_1",
        "name": "Helper",
        "description": "",
        "status": "active",
        "current_version_id": None,
        "created_at": now,
        "updated_at": now,
    }
    event_rows = [
        {
            "id": "rev_1",
            "run_id": "run_1",
            "seq": 1,
            "event_type": "message",
            "payload": {},
            "occurred_at": now,
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            if "SELECT * FROM pf_assistant WHERE id=" in statement:
                return _Result(row=assistant_row)
            return await super().execute(statement, params)

    monkeypatch.setattr(workflows.pool, "connection", lambda: _Conn(rows=event_rows))
    result = await workflows.list_assistant_run_events("ast_1", "run_1", _owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "rev_1"


@pytest.mark.asyncio
async def test_list_workflows_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "wfl_1",
            "name": "Flow",
            "description": "",
            "version": 1,
            "graph": {"nodes": [], "edges": []},
            "status": "draft",
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(workflows.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await workflows.list_workflows(_owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "wfl_1"


@pytest.mark.asyncio
async def test_list_workflow_versions_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    workflow_row = {
        "id": "wfl_1",
        "name": "Flow",
        "description": "",
        "version": 1,
        "graph": {"nodes": [], "edges": []},
        "status": "draft",
        "created_at": now,
        "updated_at": now,
    }
    version_rows = [
        {
            "id": "wfv_1",
            "workflow_id": "wfl_1",
            "version": 1,
            "created_by": "usr_owner",
            "created_at": now,
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            # _workflow 内部查询 pf_workflow WHERE id=...
            if "SELECT * FROM pf_workflow WHERE id=" in statement:
                return _Result(row=workflow_row)
            return await super().execute(statement, params)

    monkeypatch.setattr(workflows.pool, "connection", lambda: _Conn(rows=version_rows))
    result = await workflows.list_workflow_versions("wfl_1", _owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "wfv_1"


# ---------------------------------------------------------------------------
# memory.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_memories_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "mem_1",
            "kind": "episodic",
            "memory_key": "trip",
            "content": "weekend trip",
            "metadata": {},
            "source_session_id": None,
            "status": "active",
            "expires_at": None,
            "created_at": now,
            "updated_at": now,
            "importance": 0.5,
            "confidence": 0.5,
            "retention_policy": "standard",
            "semantic_version": "local-hash-v1",
            "last_recalled_at": None,
        }
    ]
    monkeypatch.setattr(memory.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await memory.list_memories(_owner(), query=None, limit=50)
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "mem_1"
    assert result["meta"]["count"] == 1


@pytest.mark.asyncio
async def test_recall_memories_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "mem_1",
            "kind": "episodic",
            "memory_key": "trip plan",
            "content": "weekend trip plan",
            "metadata": {},
            "source_session_id": None,
            "status": "active",
            "expires_at": None,
            "created_at": now,
            "updated_at": now,
            "importance": 0.5,
            "confidence": 0.5,
            "retention_policy": "standard",
            "semantic_version": "local-hash-v1",
            "last_recalled_at": None,
        }
    ]
    monkeypatch.setattr(memory.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await memory.recall_memories(_owner(), query="trip", limit=10)
    _assert_listresponse_envelope(result)
    assert result["query"] == "trip"
    assert result["mode"] == "hybrid"


# ---------------------------------------------------------------------------
# notification/router.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_notifications_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "ntf_1",
            "event_type": "system.alert",
            "priority": "high",
            "title": "Alert",
            "summary": "",
            "action_url": None,
            "payload_min": {},
            "resource_ref": None,
            "read_at": None,
            "created_at": now,
            "expires_at": None,
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            # 计数查询返回固定行
            if "SELECT count(*) AS count" in statement:
                return _Result(row={"count": 0})
            return await super().execute(statement, params)

    monkeypatch.setattr(notification_router.pool, "connection", lambda: _Conn(rows=rows))
    result = await notification_router.list_notifications(_owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "ntf_1"
    assert result["unread_count"] == 0


@pytest.mark.asyncio
async def test_get_notification_preferences_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "event_type": "system.alert",
            "channel": "in_app",
            "enabled": True,
            "quiet_start": None,
            "quiet_end": None,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(notification_router.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await notification_router.get_notification_preferences(_owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["channel"] == "in_app"


# ---------------------------------------------------------------------------
# session/router.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sessions_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "sess_1",
            "title": "Conversation",
            "model": "workama-chat",
            "agent_kind": "ama_chat",
            "model_config": {},
            "toolset": [],
            "canvas_enabled": True,
            "prompt_version_id": None,
            "max_steps": 50,
            "max_credits": 500,
            "max_duration_seconds": 3600,
            "used_steps": 0,
            "used_credits": 0,
            "started_at": None,
            "status": "idle",
            "last_seq": 0,
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(session_router.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await session_router.list_sessions(_owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "sess_1"


@pytest.mark.asyncio
async def test_list_events_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "evt_1",
            "seq": 1,
            "type": "message",
            "payload": {},
            "created_at": now,
        }
    ]
    monkeypatch.setattr(session_router.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await session_router.list_events("sess_1", _owner(), after=0)
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "evt_1"


# ---------------------------------------------------------------------------
# billing/router.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_billing_transactions_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "txn_1",
            "kind": "credit",
            "amount": 100,
            "balance_after": 100,
            "reference_id": None,
            "description": "initial",
            "created_at": now,
        }
    ]
    monkeypatch.setattr(billing_router.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await billing_router.transactions(_owner(), limit=50)
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "txn_1"


@pytest.mark.asyncio
async def test_billing_reconciliations_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "rec_1",
            "business_date": now.date(),
            "usage_credits": 100,
            "ledger_credits": 100,
            "difference": 0,
            "difference_ratio": 0.0,
            "status": "balanced",
            "checked_at": now,
        }
    ]
    monkeypatch.setattr(billing_router.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await billing_router.reconciliations(_owner(), limit=31)
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "rec_1"


# ---------------------------------------------------------------------------
# subscriptions.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_plans_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "code": "free",
            "name": "Free",
            "monthly_price": 0,
            "currency": "USD",
            "quotas": {},
        }
    ]
    monkeypatch.setattr(subscriptions.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await subscriptions.list_plans(_owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["code"] == "free"


@pytest.mark.asyncio
async def test_list_invoices_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "inv_1",
            "invoice_number": "INV-001",
            "payment_id": "pay_1",
            "status": "paid",
            "amount": 100,
            "currency": "USD",
            "issued_at": now,
        }
    ]
    monkeypatch.setattr(subscriptions.pool, "connection", lambda: _ListConnection(rows=rows))
    result = await subscriptions.list_invoices(_owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "inv_1"
