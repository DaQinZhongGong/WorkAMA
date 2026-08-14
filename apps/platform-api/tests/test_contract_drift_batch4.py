"""契约源码-设计漂移治理第四批：契约回归测试。

覆盖《720-实施级API操作与消息契约注册表》对响应结构的约束：
- 列表端点：``ListResponse<T>`` 必须包含 ``data``/``next_cursor``/``has_more``/``meta``，保留 ``items`` 向后兼容
- 异步端点：``OperationAccepted`` 必须包含 ``operation_id``/``status``/``status_url``/``submitted_at``
- 单资源端点：顶层暴露 DTO 字段，保留旧包装键

本文件仅做契约形状校验，不依赖真实数据库；通过 monkeypatch 替换连接池即可。
覆盖第四批修复的端点：gateway/router、gateway_prompts、channel_extensions、enterprise、mcp。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from workama_platform.core import Actor
from workama_platform.modules import channel_extensions, enterprise, gateway_prompts, mcp
from workama_platform.modules.gateway import router as gateway_router


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


def _admin(workspace_id: str = "wsp_1", org_id: str = "org_1") -> Actor:
    """管理员 actor，满足 gateway 与 channel_extensions 的 _require_admin。"""
    return Actor(
        user_id="usr_admin",
        workspace_id=workspace_id,
        org_id=org_id,
        role="admin",
        email="admin@example.test",
        display_name="Admin",
        onboarding_completed=True,
        capabilities=("*",),
    )


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
# gateway/router.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_channels_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "chn_1",
            "name": "OpenAI",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "models": ["gpt-4"],
            "weight": 100,
            "status": "active",
            "last_health": None,
            "has_credential": True,
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(gateway_router, "pool", _Pool(_ListConnection(rows=rows)))
    result = await gateway_router.list_channels(_admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "chn_1"
    assert result["meta"]["count"] == 1


@pytest.mark.asyncio
async def test_list_providers_returns_listresponse_envelope():
    result = await gateway_router.list_providers(_admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"]  # 至少包含 provider id
    assert result["meta"]["count"] == len(result["items"])


@pytest.mark.asyncio
async def test_list_free_providers_returns_listresponse_envelope():
    result = await gateway_router.list_free_providers()
    _assert_listresponse_envelope(result)
    # 保留旧字段 total 向后兼容
    assert "total" in result
    assert result["total"] == len(result["items"])
    assert result["data"] == result["items"]


@pytest.mark.asyncio
async def test_list_model_mappings_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "gmm_1",
            "model": "gpt-4",
            "channel_id": "chn_1",
            "channel_name": "OpenAI",
            "upstream_model": "gpt-4-0613",
            "channel_status": "active",
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(gateway_router, "pool", _Pool(_ListConnection(rows=rows)))
    result = await gateway_router.list_model_mappings(_admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "gmm_1"


@pytest.mark.asyncio
async def test_list_token_groups_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "wtg_1",
            "name": "default",
            "rpm_limit": 60,
            "tpm_limit": 60000,
            "model_whitelist": [],
            "pinned_channel_id": None,
            "fallback_chain": [],
            "model_mapping_override": {},
            "pinned_channel_name": None,
            "status": "active",
            "active_token_count": 2,
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(gateway_router, "pool", _Pool(_ListConnection(rows=rows)))
    result = await gateway_router.list_token_groups(_admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "wtg_1"


@pytest.mark.asyncio
async def test_list_tokens_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "gwt_1",
            "name": "client-a",
            "last_four": "abcd",
            "rpm_limit": 60,
            "tpm_limit": 60000,
            "model_whitelist": [],
            "pinned_channel_id": None,
            "pinned_channel_name": None,
            "group_id": None,
            "group_name": None,
            "group_pinned_channel_name": None,
            "expires_at": None,
            "revoked_at": None,
            "created_at": now,
        }
    ]
    monkeypatch.setattr(gateway_router, "pool", _Pool(_ListConnection(rows=rows)))
    result = await gateway_router.list_tokens(_admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "gwt_1"


@pytest.mark.asyncio
async def test_list_pricing_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "model": "gpt-4",
            "input_per_million": 30,
            "output_per_million": 60,
            "markup_percent": 0,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(gateway_router, "pool", _Pool(_ListConnection(rows=rows)))
    result = await gateway_router.list_pricing(_admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["model"] == "gpt-4"


@pytest.mark.asyncio
async def test_list_logs_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "request_id": "req_1",
            "token_id": "gwt_1",
            "channel_id": "chn_1",
            "model": "gpt-4",
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "cost_credits": 1,
            "latency_ms": 120,
            "status_code": 200,
            "error_code": None,
            "created_at": now,
        }
    ]
    monkeypatch.setattr(gateway_router, "pool", _Pool(_ListConnection(rows=rows)))
    result = await gateway_router.list_logs(_admin(), limit=50)
    _assert_listresponse_envelope(result)
    assert result["data"][0]["request_id"] == "req_1"


# ---------------------------------------------------------------------------
# gateway_prompts.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_prompts_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "gwprm_1",
            "name": "support.reply",
            "version": 1,
            "content": "hello",
            "checksum": "a" * 64,
            "status": "draft",
            "rollout_percent": 0,
            "created_at": now,
            "published_at": None,
            "eval_status": None,
            "eval_failures": None,
        }
    ]
    monkeypatch.setattr(gateway_prompts, "pool", _Pool(_ListConnection(rows=rows)))
    result = await gateway_prompts.list_prompts(
        _owner(), name=None, status_filter=None, workspace_id=None, limit=50, cursor=None
    )
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "gwprm_1"


@pytest.mark.asyncio
async def test_list_prompt_versions_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    base_row = {
        "id": "gwprm_1",
        "name": "support.reply",
        "version": 1,
        "content": "hello",
        "checksum": "a" * 64,
        "status": "draft",
        "rollout_percent": 0,
        "created_at": now,
        "published_at": None,
        "eval_status": None,
        "eval_failures": None,
    }
    version_rows = [
        {
            "id": "gwprm_1",
            "workspace_id": "wsp_1",
            "name": "support.reply",
            "version": 1,
            "content": "hello",
            "checksum": "a" * 64,
            "status": "draft",
            "rollout_percent": 0,
            "created_at": now,
            "published_at": None,
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            # _get_version 内部按 id + workspace 查询，返回 base_row 作为基础版本
            if "FROM sec_prompt_version p" in statement and "WHERE p.id=" in statement:
                return _Result(row=base_row)
            return await super().execute(statement, params)

    monkeypatch.setattr(gateway_prompts, "pool", _Pool(_Conn(rows=version_rows)))
    result = await gateway_prompts.list_prompt_versions("gwprm_1", _owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "gwprm_1"


# ---------------------------------------------------------------------------
# channel_extensions.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_account_pools_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "pool_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "name": "default",
            "provider": "wechat",
            "sticky_ttl_seconds": 3600,
            "billing_policy": {},
            "status": "active",
            "created_by": "usr_admin",
            "created_at": now,
            "updated_at": now,
            "account_count": 2,
        }
    ]
    monkeypatch.setattr(channel_extensions, "pool", _Pool(_ListConnection(rows=rows)))
    result = await channel_extensions.list_account_pools(_admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "pool_1"


@pytest.mark.asyncio
async def test_list_pool_accounts_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "acct_1",
            "pool_id": "pool_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "display_name": "wechat-001",
            "account_ref_enc": "secret",
            "account_ref_hash": "hash",
            "last_four": "abcd",
            "region": "cn",
            "weight": 100,
            "quota_remaining": 100,
            "lease_owner_hash": None,
            "lease_expires_at": None,
            "last_used_at": None,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(channel_extensions, "pool", _Pool(_ListConnection(rows=rows)))
    result = await channel_extensions.list_pool_accounts("pool_1", _admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "acct_1"
    # _account_view 应剥离敏感字段
    assert "account_ref_enc" not in result["data"][0]
    assert "account_ref_hash" not in result["data"][0]


@pytest.mark.asyncio
async def test_list_im_channels_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "imc_1",
            "workspace_id": "wsp_1",
            "kind": "feishu",
            "name": "Feishu Bot",
            "endpoint": "https://open.feishu.cn/webhook",
            "agent_id": "ast_1",
            "status": "active",
            "config": {},
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(channel_extensions, "pool", _Pool(_ListConnection(rows=rows)))
    result = await channel_extensions.list_im_channels(_admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "imc_1"


@pytest.mark.asyncio
async def test_list_im_messages_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "imm_1",
            "channel_id": "imc_1",
            "external_message_id": "ext_1",
            "direction": "inbound",
            "sender_ref_hash": "hash",
            "payload_min": {},
            "status": "accepted",
            "response_summary": {},
            "created_at": now,
        }
    ]
    monkeypatch.setattr(channel_extensions, "pool", _Pool(_ListConnection(rows=rows)))
    result = await channel_extensions.list_im_messages("imc_1", _admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "imm_1"


@pytest.mark.asyncio
async def test_list_miniapp_sessions_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "mps_1",
            "provider": "wechat",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(channel_extensions, "pool", _Pool(_ListConnection(rows=rows)))
    result = await channel_extensions.list_miniapp_sessions(_admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "mps_1"


@pytest.mark.asyncio
async def test_list_miniapp_messages_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "mpm_1",
            "role": "user",
            "content": "hello",
            "status": "delivered",
            "created_at": now,
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            # 拥有者检查返回单行，确认会话归属
            if "SELECT 1 FROM miniapp_session" in statement:
                return _Result(row={"?column?": 1})
            return await super().execute(statement, params)

    monkeypatch.setattr(channel_extensions, "pool", _Pool(_Conn(rows=rows)))
    result = await channel_extensions.list_miniapp_messages("mps_1", _admin())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "mpm_1"


@pytest.mark.asyncio
async def test_list_channel_bindings_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "cbd_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "channel_type": "feishu",
            "external_subject": "ext_1",
            "credential_ref": "cr_1",
            "mapping": {},
            "status": "active",
            "last_sync": None,
            "created_by": "usr_admin",
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(channel_extensions, "pool", _Pool(_ListConnection(rows=rows)))
    result = await channel_extensions.list_channel_bindings(_admin(), limit=50)
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "cbd_1"


# ---------------------------------------------------------------------------
# enterprise.py 契约测试
# ---------------------------------------------------------------------------


_ORG_ROW = {
    "id": "org_1",
    "name": "Org",
    "owner_user_id": "usr_owner",
    "status": "active",
    "deletion_requested_at": None,
    "deletion_scheduled_at": None,
    "deletion_cancelled_at": None,
    "created_at": datetime.now(UTC),
}


@pytest.mark.asyncio
async def test_list_service_accounts_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    sa_rows = [
        {
            "id": "sa_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "name": "ci-bot",
            "owner_user_id": "usr_owner",
            "purpose": "ci",
            "status": "active",
            "expires_at": None,
            "network_policy": {},
            "scopes": ["session:write"],
            "active_credential_version": 1,
            "last_used_at": None,
            "created_by": "usr_owner",
            "created_at": now,
            "updated_at": now,
            "credential_version": 1,
            "last_four": "abcd",
            "effective_status": "active",
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            # _get_org 内部按 id 查询 id_org
            if "FROM id_org" in statement and "WHERE id=" in statement:
                return _Result(row=_ORG_ROW)
            return await super().execute(statement, params)

    monkeypatch.setattr(enterprise, "pool", _Pool(_Conn(rows=sa_rows)))
    result = await enterprise.list_service_accounts(_owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "sa_1"


@pytest.mark.asyncio
async def test_list_owner_transfers_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    transfer_rows = [
        {
            "id": "otx_1",
            "org_id": "org_1",
            "from_owner_user_id": "usr_owner",
            "to_owner_user_id": "usr_new",
            "initiated_by": "usr_owner",
            "status": "pending",
            "reason": "rotation",
            "expires_at": now,
            "created_at": now,
            "confirmed_by": None,
            "confirmed_at": None,
            "cancelled_by": None,
            "cancelled_at": None,
        }
    ]
    fact_rows: list[dict[str, Any]] = []

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            if "FROM id_org" in statement and "WHERE id=" in statement:
                return _Result(row=_ORG_ROW)
            if "FROM id_org_owner_transfer_fact" in statement:
                return _Result(rows=fact_rows)
            return await super().execute(statement, params)

    monkeypatch.setattr(enterprise, "pool", _Pool(_Conn(rows=transfer_rows)))
    result = await enterprise.list_owner_transfers("org_1", _owner())
    _assert_listresponse_envelope(result)
    # 保留 facts 字段向后兼容
    assert "facts" in result
    assert result["data"][0]["id"] == "otx_1"


@pytest.mark.asyncio
async def test_list_organization_deletion_requests_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    deletion_rows = [
        {
            "id": "odr_1",
            "org_id": "org_1",
            "requested_by": "usr_owner",
            "status": "pending",
            "reason": "cleanup",
            "retention_until": now,
            "requested_at": now,
            "cancelled_by": None,
            "cancelled_at": None,
            "cancel_reason": None,
            "created_at": now,
            "updated_at": now,
        }
    ]

    class _Conn(_ListConnection):
        async def execute(self, statement, params=None):
            if "FROM id_org" in statement and "WHERE id=" in statement:
                return _Result(row=_ORG_ROW)
            return await super().execute(statement, params)

    monkeypatch.setattr(enterprise, "pool", _Pool(_Conn(rows=deletion_rows)))
    result = await enterprise.list_organization_deletion_requests("org_1", _owner())
    _assert_listresponse_envelope(result)
    assert result["data"][0]["id"] == "odr_1"


# ---------------------------------------------------------------------------
# mcp.py 契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_mcp_servers_returns_listresponse_envelope(monkeypatch):
    now = datetime.now(UTC)
    rows = [
        {
            "id": "mcp_1",
            "org_id": "org_1",
            "workspace_id": "wsp_1",
            "name": "Docs",
            "transport": "streamable_http",
            "endpoint_or_command": "https://example.com/mcp",
            "auth_type": "none",
            "auth_ref": None,
            "protocol_version": "2025-06-18",
            "server_identity": {},
            "capabilities": {"tools": [], "resources": [], "prompts": []},
            "schema_hash": "hash",
            "roots": [],
            "approval_policy": "explicit",
            "risk_policy": {"source": "workama"},
            "status": "draft",
            "last_test": {},
            "last_tested_at": None,
            "version": 1,
            "created_by": "usr_owner",
            "created_at": now,
            "updated_at": now,
        }
    ]
    monkeypatch.setattr(mcp, "pool", _Pool(_ListConnection(rows=rows)))
    result = await mcp.list_mcp_servers(_admin())
    _assert_listresponse_envelope(result)
    # 保留旧字段 count 向后兼容
    assert "count" in result
    assert result["count"] == 1
    assert result["data"][0]["id"] == "mcp_1"
