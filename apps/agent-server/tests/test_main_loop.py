"""为 main.py 中 agent loop 核心函数补充的单元测试。

测试覆盖：
- send_event：事件分发（单播 / 广播 / closed 跳过）
- load_agent_shape：加载 session 配置（正常 / 不存在）
- record_usage：用量记录与 usage.updated 事件
- create_artifact / create_tool_artifact：通过 platform-api 创建 artifact
- await_approval：审批流程（approved / rejected）
- execute_tool：工具执行（A1/A2 成功 / A3 拒绝 / 未知工具 / 异常 re-raise）
- parse_plan_command：/plan 命令解析（边界用例）

所有外部依赖（pool、redis、httpx、tool_runtime）使用 fake/mock 替换，不调用真实服务。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import workama_agent.main as main_module
from workama_agent.main import (
    DeliveryState,
    await_approval,
    create_artifact,
    create_tool_artifact,
    execute_tool,
    load_agent_shape,
    parse_plan_command,
    record_usage,
    send_event,
)
from workama_agent.tool_runtime import ToolError, ToolResult


# ---------------------------------------------------------------------------
# Fake 类：模拟外部依赖（复制自 test_main.py，保持测试自包含）
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
    """将 fake pool/redis 安装到 main 模块上，供路由与业务函数使用。"""
    fake_pool = pool or FakePool()
    fake_redis = redis or FakeRedis()
    monkeypatch.setattr(main_module, "pool", fake_pool)
    monkeypatch.setattr(main_module, "redis", fake_redis)
    return fake_pool, fake_redis


# ---------------------------------------------------------------------------
# Fake WebSocket
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """模拟 WebSocket，记录所有 send_json / close 调用。"""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send_json(self, event: dict) -> None:
        self.sent.append(event)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason


# ---------------------------------------------------------------------------
# Fake httpx
# ---------------------------------------------------------------------------


class FakeResponse:
    """模拟 httpx.Response，支持 raise_for_status 和 json。"""

    def __init__(self, json_data=None, status_code: int = 200, raise_exc=None):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        return self._json


def _make_http_status_error(status_code: int = 500) -> httpx.HTTPStatusError:
    """构造一个 httpx.HTTPStatusError 用于测试。"""
    request = httpx.Request("POST", "http://platform-api:8000/internal/artifacts")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


class FakeHttpxClient:
    """Fake replacement for httpx.AsyncClient — returns queued responses for post/get."""

    def __init__(self):
        self._post_responses: list[FakeResponse] = []
        self._get_responses: list[FakeResponse] = []
        self.post_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def queue_post(self, response: FakeResponse) -> "FakeHttpxClient":
        self._post_responses.append(response)
        return self

    def queue_get(self, response: FakeResponse) -> "FakeHttpxClient":
        self._get_responses.append(response)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        if self._post_responses:
            return self._post_responses.pop(0)
        return FakeResponse()

    async def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        if self._get_responses:
            return self._get_responses.pop(0)
        return FakeResponse()


def _patch_httpx(monkeypatch, client: FakeHttpxClient):
    """Patch httpx.AsyncClient so every instantiation returns *client*."""
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: client)


def _patch_asyncio_sleep(monkeypatch):
    """Patch asyncio.sleep to be a no-op (avoid real delays in approval loops)."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))


def _clear_subscriber_state(monkeypatch):
    """Reset module-level subscriber/delivery dicts for test isolation."""
    monkeypatch.setattr(main_module, "session_subscribers", {})
    monkeypatch.setattr(main_module, "deliveries", {})


def _queue_append_events(conn: FakeConn, count: int, start_seq: int = 1) -> FakeConn:
    """Queue FakeConn results for *count* append_event calls.

    Each append_event call executes UPDATE (fetchone needed) + INSERT (no fetchone),
    so we queue 2 items per call: one with last_seq for the UPDATE, one dummy for INSERT.
    """
    for i in range(count):
        conn.queue(row={"last_seq": start_seq + i})  # UPDATE RETURNING last_seq
        conn.queue(row=None)                          # INSERT (fetchone not called)
    return conn


# ---------------------------------------------------------------------------
# send_event 测试
# ---------------------------------------------------------------------------


def test_send_event_without_session_id_sends_only_to_current_websocket(monkeypatch):
    """不带 session_id 时只发送给当前 websocket。"""
    _clear_subscriber_state(monkeypatch)
    ws = FakeWebSocket()
    event = {"type": "agent.thought", "payload": {"text": "hello"}}

    asyncio.run(send_event(ws, event))

    assert len(ws.sent) == 1
    assert ws.sent[0] == event


def test_send_event_broadcast_sends_to_all_subscribers(monkeypatch):
    """broadcast=True 且 session 有订阅者时，发送给所有订阅者。"""
    _clear_subscriber_state(monkeypatch)
    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    ws3 = FakeWebSocket()
    main_module.session_subscribers["ses_1"] = {ws1, ws2, ws3}
    event = {"session_id": "ses_1", "type": "agent.thought", "payload": {"text": "hi"}}

    asyncio.run(send_event(ws1, event, broadcast=True))

    for ws in (ws1, ws2, ws3):
        assert len(ws.sent) == 1
        assert ws.sent[0] == event


def test_send_event_skips_when_delivery_state_closed(monkeypatch):
    """state.closed=True 时跳过发送。"""
    _clear_subscriber_state(monkeypatch)
    ws = FakeWebSocket()
    main_module.deliveries[id(ws)] = DeliveryState(closed=True)
    event = {"type": "agent.thought", "payload": {"text": "skip"}}

    asyncio.run(send_event(ws, event))

    assert len(ws.sent) == 0


# ---------------------------------------------------------------------------
# load_agent_shape 测试
# ---------------------------------------------------------------------------


def test_load_agent_shape_returns_row_with_prompt_fields(monkeypatch):
    """正常返回 row（含 prompt_version_id 和 prompt_content）。"""
    row = {
        "model": "gpt-4",
        "agent_kind": "chat",
        "model_config": {"temperature": 0.7},
        "toolset": ["file.read", "web_search"],
        "canvas_enabled": True,
        "max_steps": 10,
        "max_credits": 100.0,
        "max_duration_seconds": 3600,
        "used_steps": 0,
        "used_credits": 0.0,
        "started_at": None,
        "prompt_version_id": "pv_abc",
        "prompt_content": "You are a helpful assistant.",
        "prompt_checksum": "sha256xyz",
    }
    conn = FakeConn().queue(row=row)
    _install_fakes(monkeypatch, pool=FakePool(conn))

    result = asyncio.run(load_agent_shape("ses_1", "wsp_1"))

    assert result["prompt_version_id"] == "pv_abc"
    assert result["prompt_content"] == "You are a helpful assistant."
    assert result["model"] == "gpt-4"
    assert result["toolset"] == ["file.read", "web_search"]


def test_load_agent_shape_raises_when_session_not_found(monkeypatch):
    """session 不存在时 raise ValueError("session configuration not found")。"""
    conn = FakeConn().queue(row=None)
    _install_fakes(monkeypatch, pool=FakePool(conn))

    with pytest.raises(ValueError, match="session configuration not found"):
        asyncio.run(load_agent_shape("ses_missing", "wsp_1"))


# ---------------------------------------------------------------------------
# record_usage 测试
# ---------------------------------------------------------------------------


def test_record_usage_updates_counters_and_sends_usage_event(monkeypatch):
    """正常更新 used_steps/used_credits 并发送 usage.updated 事件。"""
    _clear_subscriber_state(monkeypatch)
    # FakeConn 队列：
    # 1) record_usage 的 UPDATE RETURNING → used_steps/used_credits/max_steps/max_credits
    # 2) append_event 的 UPDATE RETURNING last_seq
    conn = (
        FakeConn()
        .queue(row={"used_steps": 6, "used_credits": 51.0, "max_steps": 10, "max_credits": 100.0})
        .queue(row={"last_seq": 42})
    )
    _install_fakes(monkeypatch, pool=FakePool(conn))
    ws = FakeWebSocket()

    result = asyncio.run(record_usage(ws, "ses_1", "wsp_1", 1.0, resource="llm"))

    # 验证返回 payload
    assert result["step_usage"]["steps"] == 1
    assert result["step_usage"]["credits"] == 1.0
    assert result["step_usage"]["resource"] == "llm"
    assert result["session_usage"]["steps"] == 6
    assert result["session_usage"]["credits"] == 51.0
    assert result["budget_remaining"]["steps"] == 4
    assert result["budget_remaining"]["credits"] == 49.0
    # 验证 usage.updated 事件已发送
    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "usage.updated"
    assert ws.sent[0]["payload"]["step_usage"]["resource"] == "llm"
    # 验证 conn.commit 被调用
    assert conn.committed is True


def test_record_usage_payload_contains_three_required_fields(monkeypatch):
    """验证 payload 包含 step_usage、session_usage、budget_remaining 三个字段。"""
    _clear_subscriber_state(monkeypatch)
    conn = (
        FakeConn()
        .queue(row={"used_steps": 3, "used_credits": 10.0, "max_steps": 20, "max_credits": 50.0})
        .queue(row={"last_seq": 7})
    )
    _install_fakes(monkeypatch, pool=FakePool(conn))
    ws = FakeWebSocket()

    result = asyncio.run(record_usage(ws, "ses_1", "wsp_1", 2.5, resource="file.read"))

    assert set(result.keys()) == {"step_usage", "session_usage", "budget_remaining"}
    assert set(result["step_usage"].keys()) == {"steps", "credits", "resource"}
    assert set(result["session_usage"].keys()) == {"steps", "credits"}
    assert set(result["budget_remaining"].keys()) == {"steps", "credits"}


# ---------------------------------------------------------------------------
# create_artifact 测试
# ---------------------------------------------------------------------------


def test_create_artifact_calls_platform_api_and_returns_artifact(monkeypatch):
    """正常调用 platform-api /internal/artifacts 返回 artifact。"""
    fake_client = FakeHttpxClient().queue_post(
        FakeResponse(json_data={"id": "art_1", "name": "test.md", "content_type": "text/markdown"})
    )
    _patch_httpx(monkeypatch, fake_client)
    _install_fakes(monkeypatch)

    result = asyncio.run(create_artifact("ses_1", "wsp_1", "# Hello"))

    assert result == {"id": "art_1", "name": "test.md", "content_type": "text/markdown"}
    assert len(fake_client.post_calls) == 1
    call = fake_client.post_calls[0]
    assert "/internal/artifacts" in call["url"]
    assert call["headers"]["X-Internal-Token"] == main_module.settings.internal_token
    assert call["json"]["workspace_id"] == "wsp_1"
    assert call["json"]["session_id"] == "ses_1"
    assert call["json"]["content"] == "# Hello"
    assert call["json"]["kind"] == "doc"


def test_create_artifact_raises_when_httpx_fails(monkeypatch):
    """httpx 调用失败时 raise_for_status 抛错。"""
    fake_client = FakeHttpxClient().queue_post(
        FakeResponse(raise_exc=_make_http_status_error(500))
    )
    _patch_httpx(monkeypatch, fake_client)
    _install_fakes(monkeypatch)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(create_artifact("ses_1", "wsp_1", "content"))


# ---------------------------------------------------------------------------
# create_tool_artifact 测试
# ---------------------------------------------------------------------------


def test_create_tool_artifact_returns_id_name_content_type(monkeypatch):
    """正常返回 {id, name, content_type}。"""
    fake_client = FakeHttpxClient().queue_post(
        FakeResponse(json_data={"id": "art_2", "name": "output.txt", "content_type": "text/plain"})
    )
    _patch_httpx(monkeypatch, fake_client)
    _install_fakes(monkeypatch)
    artifact = {"name": "output.txt", "content_type": "text/plain", "content": "data"}

    result = asyncio.run(create_tool_artifact("ses_1", "wsp_1", artifact))

    assert result == {"id": "art_2", "name": "output.txt", "content_type": "text/plain"}
    assert len(fake_client.post_calls) == 1
    call = fake_client.post_calls[0]
    assert call["json"]["kind"] == "file"
    assert call["json"]["name"] == "output.txt"


def test_create_tool_artifact_raises_when_httpx_fails(monkeypatch):
    """httpx 失败时抛错。"""
    fake_client = FakeHttpxClient().queue_post(
        FakeResponse(raise_exc=_make_http_status_error(403))
    )
    _patch_httpx(monkeypatch, fake_client)
    _install_fakes(monkeypatch)
    artifact = {"name": "fail.txt", "content": ""}

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(create_tool_artifact("ses_1", "wsp_1", artifact))


# ---------------------------------------------------------------------------
# await_approval 测试
# ---------------------------------------------------------------------------


def test_await_approval_approved_path_consumes_and_returns_true(monkeypatch):
    """approved 路径：首次 pending → 第二次 approved → consume 成功 → 返回 True。"""
    _clear_subscriber_state(monkeypatch)
    _patch_asyncio_sleep(monkeypatch)
    # 2 个 append_event 调用（tool.approval_required + tool.approval_decided），每个需要 UPDATE + INSERT
    conn = _queue_append_events(FakeConn(), 2)
    _install_fakes(monkeypatch, pool=FakePool(conn))
    ws = FakeWebSocket()

    fake_client = (
        FakeHttpxClient()
        .queue_post(FakeResponse(json_data={"id": "appr_1", "expires_at": "2026-01-01T00:00:00Z"}))  # create
        .queue_get(FakeResponse(json_data={"status": "pending", "decided_by": None}))                # check 1
        .queue_get(FakeResponse(json_data={"status": "approved", "decided_by": "user_1"}))           # check 2
        .queue_post(FakeResponse(json_data={}))                                                       # consume
    )
    _patch_httpx(monkeypatch, fake_client)

    result = asyncio.run(await_approval(
        ws, "ses_1", "wsp_1", "user_1", "call_1", "terminal", "A3", "hash123",
        {"argv": "<1 chars>"},
    ))

    assert result is True
    # 验证 consume POST 被调用（2 次 POST：create + consume）
    assert len(fake_client.post_calls) == 2
    assert "/consume" in fake_client.post_calls[1]["url"]
    # 验证事件：tool.approval_required + tool.approval_decided
    event_types = [e["type"] for e in ws.sent]
    assert "tool.approval_required" in event_types
    assert "tool.approval_decided" in event_types
    # approval_decided 的 decision 应为 approved
    decided_event = next(e for e in ws.sent if e["type"] == "tool.approval_decided")
    assert decided_event["payload"]["decision"] == "approved"


def test_await_approval_rejected_path_returns_false_without_consume(monkeypatch):
    """rejected 路径：首次 pending → 第二次 rejected → 返回 False（不调用 consume）。"""
    _clear_subscriber_state(monkeypatch)
    _patch_asyncio_sleep(monkeypatch)
    conn = _queue_append_events(FakeConn(), 2)
    _install_fakes(monkeypatch, pool=FakePool(conn))
    ws = FakeWebSocket()

    fake_client = (
        FakeHttpxClient()
        .queue_post(FakeResponse(json_data={"id": "appr_2", "expires_at": "2026-01-01T00:00:00Z"}))  # create
        .queue_get(FakeResponse(json_data={"status": "pending", "decided_by": None}))                # check 1
        .queue_get(FakeResponse(json_data={"status": "rejected", "decided_by": "user_2"}))           # check 2
    )
    _patch_httpx(monkeypatch, fake_client)

    result = asyncio.run(await_approval(
        ws, "ses_1", "wsp_1", "user_1", "call_2", "terminal", "A3", "hash456",
        {"argv": "<1 chars>"},
    ))

    assert result is False
    # 验证 consume POST 未被调用（仅 1 次 POST：create）
    assert len(fake_client.post_calls) == 1
    # 验证 approval_decided 的 decision 为 rejected
    event_types = [e["type"] for e in ws.sent]
    assert "tool.approval_decided" in event_types
    decided_event = next(e for e in ws.sent if e["type"] == "tool.approval_decided")
    assert decided_event["payload"]["decision"] == "rejected"


# ---------------------------------------------------------------------------
# execute_tool 测试
# ---------------------------------------------------------------------------


def test_execute_tool_a1_tool_sends_call_result_and_step_finished(monkeypatch):
    """A1/A2 工具（如 file.read）正常执行：发送 tool.call、sandbox.status、tool.result、step.finished 事件。"""
    _clear_subscriber_state(monkeypatch)
    # 4 个 append_event 调用：tool.call, sandbox.status, tool.result, step.finished
    conn = _queue_append_events(FakeConn(), 4)
    _install_fakes(monkeypatch, pool=FakePool(conn))
    ws = FakeWebSocket()

    # 替换 tool_runtime
    fake_runtime = MagicMock()
    fake_runtime.execute = AsyncMock(return_value=ToolResult("succeeded", "Read test.txt", "file content"))
    monkeypatch.setattr(main_module, "tool_runtime", fake_runtime)

    result = asyncio.run(execute_tool(ws, "ses_1", "wsp_1", "user_1", "file.read", {"path": "test.txt"}))

    assert result["status"] == "succeeded"
    assert result["summary"] == "Read test.txt"
    assert result["output"] == "file content"
    assert result["artifact_refs"] == []

    event_types = [e["type"] for e in ws.sent]
    assert event_types == ["tool.call", "sandbox.status", "tool.result", "step.finished"]
    # step.finished 的 outcome 应与 result.status 一致
    assert ws.sent[-1]["payload"]["outcome"] == "succeeded"
    # 验证 tool_runtime.execute 被调用
    fake_runtime.execute.assert_awaited_once_with("file.read", {"path": "test.txt"}, "wsp_1", "ses_1")


def test_execute_tool_a3_rejected_sends_approval_and_rejection_events(monkeypatch):
    """A3 工具被拒绝审批时：发送 tool.call、tool.approval_required、tool.approval_decided、tool.result(rejected)、step.finished(rejected)。"""
    _clear_subscriber_state(monkeypatch)
    _patch_asyncio_sleep(monkeypatch)
    # 5 个 append_event 调用：tool.call, tool.approval_required, tool.approval_decided, tool.result, step.finished
    conn = _queue_append_events(FakeConn(), 5)
    _install_fakes(monkeypatch, pool=FakePool(conn))
    ws = FakeWebSocket()

    # 为 await_approval 设置 httpx fake（rejected 路径）
    fake_client = (
        FakeHttpxClient()
        .queue_post(FakeResponse(json_data={"id": "appr_3", "expires_at": "2026-01-01T00:00:00Z"}))
        .queue_get(FakeResponse(json_data={"status": "pending", "decided_by": None}))
        .queue_get(FakeResponse(json_data={"status": "rejected", "decided_by": "user_3"}))
    )
    _patch_httpx(monkeypatch, fake_client)

    result = asyncio.run(execute_tool(ws, "ses_1", "wsp_1", "user_1", "terminal", {"argv": ["ls"]}))

    assert result["status"] == "rejected"
    assert "not approved" in result["summary"]

    event_types = [e["type"] for e in ws.sent]
    assert event_types == [
        "tool.call",
        "tool.approval_required",
        "tool.approval_decided",
        "tool.result",
        "step.finished",
    ]
    # tool.result 应为 rejected
    tool_result_event = next(e for e in ws.sent if e["type"] == "tool.result")
    assert tool_result_event["payload"]["status"] == "rejected"
    # step.finished 的 outcome 应为 rejected
    step_finished_event = next(e for e in ws.sent if e["type"] == "step.finished")
    assert step_finished_event["payload"]["outcome"] == "rejected"


def test_execute_tool_unknown_tool_raises_tool_error(monkeypatch):
    """未知工具 raise ToolError。"""
    _clear_subscriber_state(monkeypatch)
    _install_fakes(monkeypatch)
    ws = FakeWebSocket()

    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(execute_tool(ws, "ses_1", "wsp_1", "user_1", "nonexistent.tool", {}))

    # 未知工具不应发送任何事件
    assert len(ws.sent) == 0


def test_execute_tool_runtime_exception_sends_failed_result_and_reraises(monkeypatch):
    """工具执行抛异常时发送 tool.result(failed) 并 re-raise。"""
    _clear_subscriber_state(monkeypatch)
    # 3 个 append_event 调用：tool.call, sandbox.status, tool.result(failed)
    conn = _queue_append_events(FakeConn(), 3)
    _install_fakes(monkeypatch, pool=FakePool(conn))
    ws = FakeWebSocket()

    fake_runtime = MagicMock()
    fake_runtime.execute = AsyncMock(side_effect=RuntimeError("sandbox unavailable"))
    monkeypatch.setattr(main_module, "tool_runtime", fake_runtime)

    with pytest.raises(RuntimeError, match="sandbox unavailable"):
        asyncio.run(execute_tool(ws, "ses_1", "wsp_1", "user_1", "file.read", {"path": "missing.txt"}))

    event_types = [e["type"] for e in ws.sent]
    assert event_types == ["tool.call", "sandbox.status", "tool.result"]
    # 最后一个事件应为 tool.result 且 status=failed
    failed_event = ws.sent[-1]
    assert failed_event["type"] == "tool.result"
    assert failed_event["payload"]["status"] == "failed"
    assert "sandbox unavailable" in failed_event["payload"]["summary"]
    # 不应有 step.finished（异常路径不发送）
    assert "step.finished" not in event_types


# ---------------------------------------------------------------------------
# parse_plan_command 测试
# ---------------------------------------------------------------------------


def test_parse_plan_command_returns_none_for_non_plan_message():
    """非 /plan 开头返回 None。"""
    assert parse_plan_command("hello world") is None
    assert parse_plan_command("/tool file.read") is None
    assert parse_plan_command("") is None
    assert parse_plan_command("  /plant grow something") is None  # /plant, not /plan
    assert parse_plan_command("/planx") is None  # no space after /plan


def test_parse_plan_command_parses_valid_json_array():
    """/plan + 有效 JSON 数组：返回 plan list。"""
    plan = parse_plan_command('/plan ' + json.dumps([
        {"tool": "file.read", "arguments": {"path": "test.txt"}},
        {"tool": "file.write", "arguments": {"path": "out.txt", "content": "data"}},
    ]))

    assert isinstance(plan, list)
    assert len(plan) == 2
    assert plan[0]["tool"] == "file.read"
    assert plan[0]["arguments"] == {"path": "test.txt"}
    assert plan[0]["status"] == "pending"
    assert plan[0]["id"].startswith("step_")
    assert plan[0]["dependencies"] == []
    assert plan[1]["tool"] == "file.write"
    assert plan[1]["arguments"] == {"path": "out.txt", "content": "data"}


def test_parse_plan_command_raises_on_invalid_json():
    """/plan + 无效 JSON：raise ToolError。"""
    with pytest.raises(ToolError, match="valid JSON"):
        parse_plan_command("/plan {not valid json}")


def test_parse_plan_command_raises_on_empty_array():
    """/plan + 空数组：raise ToolError。"""
    with pytest.raises(ToolError, match="non-empty JSON array"):
        parse_plan_command("/plan []")


def test_parse_plan_command_raises_on_missing_tool_field():
    """/plan + 缺 tool 字段：raise ToolError。"""
    with pytest.raises(ToolError, match="tool and arguments"):
        parse_plan_command('/plan ' + json.dumps([
            {"arguments": {"path": "test.txt"}},
        ]))
