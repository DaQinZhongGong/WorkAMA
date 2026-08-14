"""为 main.py（FastAPI app 入口、路由定义）补充的单元测试。

测试覆盖：
- FastAPI app 创建与配置（标题、版本、路由、中间件）
- 路由端点（/healthz、/internal/tools、/internal/event-types）
- lifespan 生命周期（启动/关闭资源）
- 核心业务函数（append_event、load_history、budget_checkpoint、control_checkpoint、attachment_context）
- 工具函数（new_id、DeliveryState、parse_sse_data）

所有外部依赖（pool、redis）使用简单 fake 类替换，不调用真实服务。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import workama_agent.main as main_module
from workama_agent.main import (
    AGENT_EVENT_TYPES,
    DeliveryState,
    RunCancelled,
    RunLimit,
    append_event,
    attachment_context,
    budget_checkpoint,
    control_checkpoint,
    load_history,
    new_id,
    parse_sse_data,
)
from workama_agent.tool_runtime import TOOL_DEFINITIONS


# ---------------------------------------------------------------------------
# Fake 类：模拟外部依赖（psycopg 异步连接池、redis 客户端）
# ---------------------------------------------------------------------------


class _FakeFetchResult:
    """模拟 psycopg 查询结果，返回可控的 fetchone/fetchall 数据。"""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class FakeConn:
    """模拟 psycopg 异步连接，按调用顺序返回预设结果。"""

    def __init__(self):
        self.queries = []
        self._results = []
        self.committed = False

    def queue(self, row=None, rows=None):
        """为下一次 execute 调用预设返回结果。"""
        self._results.append(_FakeFetchResult(row, rows))
        return self

    async def execute(self, sql, *args):
        self.queries.append((sql, args))
        if self._results:
            return self._results.pop(0)
        return _FakeFetchResult()

    async def commit(self):
        self.committed = True

    def transaction(self):
        return _FakeTransaction()


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class _FakePoolConnection:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        return None


class FakePool:
    """模拟异步连接池，记录 open/close 调用。"""

    def __init__(self, conn=None):
        self.conn = conn or FakeConn()
        self.opened = False
        self.closed = False

    def connection(self):
        return _FakePoolConnection(self.conn)

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True


class FakeRedis:
    """模拟 Redis 客户端，维护一个简单的内存键值存储。"""

    def __init__(self, store=None):
        self.store = store or {}
        self.pinged = False
        self.closed = False

    async def get(self, key):
        return self.store.get(key)

    async def getdel(self, key):
        return self.store.pop(key, None)

    async def delete(self, key):
        self.store.pop(key, None)

    async def ping(self):
        self.pinged = True

    async def aclose(self):
        self.closed = True


def _install_fakes(monkeypatch, pool=None, redis=None):
    """将 fake pool/redis 安装到 main 模块上，供路由与 lifespan 使用。"""
    fake_pool = pool or FakePool()
    fake_redis = redis or FakeRedis()
    monkeypatch.setattr(main_module, "pool", fake_pool)
    monkeypatch.setattr(main_module, "redis", fake_redis)
    return fake_pool, fake_redis


# ---------------------------------------------------------------------------
# App 配置测试
# ---------------------------------------------------------------------------


def test_app_metadata_has_title_and_version():
    """FastAPI app 应具有正确的标题和版本号。"""
    assert main_module.app.title == "WorkAMA Agent Server"
    assert main_module.app.version == "0.1.0"


def test_app_registers_expected_routes():
    """app 应注册 healthz、internal/tools、internal/event-types、ws/sessions 路由。"""
    paths = {route.path for route in main_module.app.routes}
    assert "/healthz" in paths
    assert "/internal/tools" in paths
    assert "/internal/event-types" in paths
    assert "/ws/sessions/{session_id}" in paths


def test_app_has_cors_middleware():
    """app 应配置 CORS 中间件。"""
    from fastapi.middleware.cors import CORSMiddleware

    middleware_classes = [m.cls for m in main_module.app.user_middleware]
    assert CORSMiddleware in middleware_classes


# ---------------------------------------------------------------------------
# 路由端点测试
# ---------------------------------------------------------------------------


def test_healthz_returns_ok_status(monkeypatch):
    """GET /healthz 在 pool 可用时返回 200 和 ok 状态。"""
    _install_fakes(monkeypatch)

    with TestClient(main_module.app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "agent-server"}


def test_internal_tools_rejects_missing_token(monkeypatch):
    """GET /internal/tools 不带 token 返回 401。"""
    _install_fakes(monkeypatch)

    with TestClient(main_module.app) as client:
        response = client.get("/internal/tools")
    assert response.status_code == 401


def test_internal_tools_rejects_invalid_token(monkeypatch):
    """GET /internal/tools 带错误 token 返回 401。"""
    _install_fakes(monkeypatch)

    with TestClient(main_module.app) as client:
        response = client.get("/internal/tools", headers={"X-Internal-Token": "wrong"})
    assert response.status_code == 401


def test_internal_tools_returns_definitions_with_valid_token(monkeypatch):
    """GET /internal/tools 带正确 token 返回工具定义列表。"""
    _install_fakes(monkeypatch)

    with TestClient(main_module.app) as client:
        response = client.get(
            "/internal/tools",
            headers={"X-Internal-Token": main_module.settings.internal_token},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == TOOL_DEFINITIONS
    assert body["registry_version"] == "builtin-1"


def test_internal_event_types_rejects_missing_token(monkeypatch):
    """GET /internal/event-types 不带 token 返回 401。"""
    _install_fakes(monkeypatch)

    with TestClient(main_module.app) as client:
        response = client.get("/internal/event-types")
    assert response.status_code == 401


def test_internal_event_types_returns_sorted_types_with_valid_token(monkeypatch):
    """GET /internal/event-types 带正确 token 返回排序后的事件类型列表。"""
    _install_fakes(monkeypatch)

    with TestClient(main_module.app) as client:
        response = client.get(
            "/internal/event-types",
            headers={"X-Internal-Token": main_module.settings.internal_token},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == sorted(AGENT_EVENT_TYPES)
    assert body["count"] == len(AGENT_EVENT_TYPES)
    assert body["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# lifespan 生命周期测试
# ---------------------------------------------------------------------------


def test_lifespan_opens_pool_pings_redis_and_closes_on_exit(monkeypatch):
    """lifespan 启动时打开 pool 并 ping redis，退出时关闭两者。"""
    fake_pool, fake_redis = _install_fakes(monkeypatch)

    async def scenario():
        async with main_module.lifespan(MagicMock()):
            assert fake_pool.opened
            assert fake_redis.pinged
            assert not fake_pool.closed
            assert not fake_redis.closed
        assert fake_pool.closed
        assert fake_redis.closed

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# append_event 测试
# ---------------------------------------------------------------------------


def test_append_event_rejects_unknown_type(monkeypatch):
    """append_event 对不在 AGENT_EVENT_TYPES 中的类型抛出 ValueError。"""
    _install_fakes(monkeypatch)

    with pytest.raises(ValueError, match="Unknown or non-persisted"):
        asyncio.run(append_event("ses_1", "wsp_1", "totally.fake.event", {}))


def test_append_event_rejects_non_persisted_type(monkeypatch):
    """append_event 对非持久化类型（如 connection.ready）抛出 ValueError。"""
    _install_fakes(monkeypatch)

    with pytest.raises(ValueError, match="non-persisted"):
        asyncio.run(append_event("ses_1", "wsp_1", "connection.ready", {}))


def test_append_event_persists_event_with_incremented_seq(monkeypatch):
    """append_event 成功时返回包含递增 seq 的事件，并执行 UPDATE 与 INSERT 两条 SQL。"""
    conn = FakeConn().queue(row={"last_seq": 42})
    _install_fakes(monkeypatch, pool=FakePool(conn))

    event = asyncio.run(append_event("ses_1", "wsp_1", "user.message", {"content": "hi"}))

    assert event["seq"] == 42
    assert event["type"] == "user.message"
    assert event["session_id"] == "ses_1"
    assert event["payload"] == {"content": "hi"}
    assert event["id"].startswith("evt_")
    assert event["schema_version"] == "1.0"
    assert event["producer"] == "agent-server"
    assert len(conn.queries) == 2
    assert "UPDATE ag_session" in conn.queries[0][0]
    assert "INSERT INTO ag_event" in conn.queries[1][0]


def test_append_event_raises_when_session_not_found(monkeypatch):
    """append_event 在 session 不存在（fetchone 返回 None）时抛出 ValueError。"""
    conn = FakeConn().queue(row=None)
    _install_fakes(monkeypatch, pool=FakePool(conn))

    with pytest.raises(ValueError, match="session not found"):
        asyncio.run(append_event("ses_missing", "wsp_1", "user.message", {"content": "hi"}))


# ---------------------------------------------------------------------------
# load_history 测试
# ---------------------------------------------------------------------------


def test_load_history_returns_messages_in_order(monkeypatch):
    """load_history 按顺序返回消息，并从事件类型推断角色。"""
    rows = [
        {"type": "user.message", "payload": {"content": "你好", "role": "user"}},
        {"type": "agent.message.completed", "payload": {"content": "你好，有什么可以帮您？"}},
    ]
    conn = FakeConn().queue(rows=rows)
    _install_fakes(monkeypatch, pool=FakePool(conn))

    messages = asyncio.run(load_history("ses_1", "wsp_1"))

    assert len(messages) == 2
    assert messages[0] == {"role": "user", "content": "你好"}
    assert messages[1] == {"role": "assistant", "content": "你好，有什么可以帮您？"}


# ---------------------------------------------------------------------------
# budget_checkpoint 测试
# ---------------------------------------------------------------------------


def test_budget_checkpoint_raises_when_steps_exhausted(monkeypatch):
    """used_steps >= max_steps 时抛出 RunLimit(code=E04003)。"""
    conn = FakeConn().queue(row={
        "max_steps": 10, "max_credits": 100.0, "max_duration_seconds": 3600,
        "used_steps": 10, "used_credits": 50.0, "elapsed": 100.0,
    })
    _install_fakes(monkeypatch, pool=FakePool(conn))

    with pytest.raises(RunLimit, match="maximum step count") as exc_info:
        asyncio.run(budget_checkpoint("ses_1", "wsp_1"))
    assert exc_info.value.code == "E04003"


def test_budget_checkpoint_raises_when_credits_exhausted(monkeypatch):
    """used_credits >= max_credits 时抛出 RunLimit(code=E04002)。"""
    conn = FakeConn().queue(row={
        "max_steps": 10, "max_credits": 100.0, "max_duration_seconds": 3600,
        "used_steps": 5, "used_credits": 100.0, "elapsed": 100.0,
    })
    _install_fakes(monkeypatch, pool=FakePool(conn))

    with pytest.raises(RunLimit, match="credit budget") as exc_info:
        asyncio.run(budget_checkpoint("ses_1", "wsp_1"))
    assert exc_info.value.code == "E04002"


def test_budget_checkpoint_raises_when_duration_exceeded(monkeypatch):
    """elapsed >= max_duration_seconds 时抛出 RunLimit(code=E04003)。"""
    conn = FakeConn().queue(row={
        "max_steps": 10, "max_credits": 100.0, "max_duration_seconds": 3600,
        "used_steps": 5, "used_credits": 50.0, "elapsed": 3600.0,
    })
    _install_fakes(monkeypatch, pool=FakePool(conn))

    with pytest.raises(RunLimit, match="maximum duration") as exc_info:
        asyncio.run(budget_checkpoint("ses_1", "wsp_1"))
    assert exc_info.value.code == "E04003"


def test_budget_checkpoint_passes_when_within_budget(monkeypatch):
    """在预算范围内时正常返回 row，不抛出异常。"""
    row = {
        "max_steps": 10, "max_credits": 100.0, "max_duration_seconds": 3600,
        "used_steps": 5, "used_credits": 50.0, "elapsed": 100.0,
    }
    conn = FakeConn().queue(row=row)
    _install_fakes(monkeypatch, pool=FakePool(conn))

    result = asyncio.run(budget_checkpoint("ses_1", "wsp_1"))
    assert result == row


# ---------------------------------------------------------------------------
# control_checkpoint 测试
# ---------------------------------------------------------------------------


def test_control_checkpoint_returns_when_no_command(monkeypatch):
    """Redis 中无控制命令时立即返回 None，不抛出异常。"""
    _install_fakes(monkeypatch, redis=FakeRedis(store={}))

    result = asyncio.run(control_checkpoint(MagicMock(), "ses_1", "wsp_1"))
    assert result is None


def test_control_checkpoint_cancels_on_cancel_action(monkeypatch):
    """Redis 中有 cancel 命令时删除命令并抛出 RunCancelled。"""
    fake_redis = FakeRedis(store={
        "agent-control:ses_1": json.dumps({"action": "cancel", "reason": "用户取消"})
    })
    _install_fakes(monkeypatch, redis=fake_redis)

    with pytest.raises(RunCancelled, match="用户取消"):
        asyncio.run(control_checkpoint(MagicMock(), "ses_1", "wsp_1"))
    # cancel 后命令应被删除
    assert "agent-control:ses_1" not in fake_redis.store


# ---------------------------------------------------------------------------
# attachment_context 测试
# ---------------------------------------------------------------------------


def test_attachment_context_returns_empty_when_no_ids(monkeypatch):
    """没有附件 ID 时返回空字符串，不查询数据库。"""
    _install_fakes(monkeypatch)

    result = asyncio.run(attachment_context("ses_1", "wsp_1", []))
    assert result == ""


def test_attachment_context_builds_context_from_attachments(monkeypatch):
    """有附件时拼接文件名和提取文本，并添加不可信来源前缀。"""
    rows = [
        {"filename": "report.md", "extracted_text": "重要报告内容"},
        {"filename": "data.csv", "extracted_text": "col1,col2\n1,2"},
    ]
    conn = FakeConn().queue(rows=rows)
    _install_fakes(monkeypatch, pool=FakePool(conn))

    result = asyncio.run(attachment_context("ses_1", "wsp_1", ["att_1", "att_2"]))
    assert "Attachment context" in result
    assert "File: report.md" in result
    assert "重要报告内容" in result
    assert "File: data.csv" in result


# ---------------------------------------------------------------------------
# 工具函数测试
# ---------------------------------------------------------------------------


def test_new_id_generates_unique_prefixed_ids():
    """new_id 生成的 ID 具有正确前缀且多次调用结果唯一。"""
    ids = {new_id("evt") for _ in range(100)}
    assert len(ids) == 100
    assert all(identifier.startswith("evt_") for identifier in ids)


def test_delivery_state_acknowledge_ignores_old_sequences():
    """acknowledge 对小于等于 last_acked 的 seq 不做任何变更。"""
    state = DeliveryState(last_acked=5)
    state.pending.append((3, 100))
    state.pending_bytes = 100

    state.acknowledge(3)  # 3 <= 5，应被忽略

    assert state.last_acked == 5
    assert len(state.pending) == 1
    assert state.pending_bytes == 100


def test_delivery_state_acknowledge_trims_pending_queue():
    """acknowledge 更新 last_acked 并清除已确认的 pending 条目及对应字节数。"""
    state = DeliveryState(last_acked=0)
    state.pending.append((1, 100))
    state.pending.append((2, 200))
    state.pending.append((5, 300))
    state.pending_bytes = 600

    state.acknowledge(2)

    assert state.last_acked == 2
    assert len(state.pending) == 1
    assert state.pending[0][0] == 5
    assert state.pending_bytes == 300


def test_parse_sse_data_returns_none_for_non_data_lines():
    """parse_sse_data 对非 'data: ' 开头的行返回 None。"""
    assert parse_sse_data("event: message") is None
    assert parse_sse_data(": comment") is None
    assert parse_sse_data("") is None


def test_parse_sse_data_returns_none_for_invalid_json():
    """parse_sse_data 对无效 JSON 返回 None 而非抛出异常。"""
    assert parse_sse_data("data: {invalid json}") is None
    assert parse_sse_data("data: ") is None
