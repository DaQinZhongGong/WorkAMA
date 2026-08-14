"""为 main.py、coordination.py、planner.py 补充的扩展单元测试。

测试覆盖：
- main.py: send_event 广播边界、_send_to_socket 异常处理与 pending 跟踪、
  parse_plan_command 边界用例（显式 ID / depends_on / 空工具 / null 字节 / 缺省 arguments）、
  control_checkpoint 暂停→恢复与暂停→取消、attachment_context 截断与空文本跳过、
  execute_tool 非沙箱工具与工件事件、await_approval consume 失败
- coordination.py: SubsessionRequest / ExecutorResult / ExecutorMessage 校验、
  TaskExecutionState 序列化、_BudgetLedger 预算预留与结算、
  Coordinator 静态方法、effective_limits 取最小值、执行器异常传播
- planner.py: PlannerLimits 多字段校验、Budget 消费与耗尽、TaskPlan.budget_for、
  blocked_task_ids、allocate_task_budgets 超限、ConvergenceDecision 序列化

所有外部依赖（pool、redis、httpx、tool_runtime）使用 fake/mock 替换，不调用真实服务。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import WebSocketDisconnect

import workama_agent.main as main_module
from workama_agent.main import (
    DeliveryState,
    _send_to_socket,
    attachment_context,
    await_approval,
    control_checkpoint,
    execute_tool,
    parse_plan_command,
    send_event,
)
from workama_agent.coordination import (
    CoordinationStatus,
    Coordinator,
    ExecutorMessage,
    ExecutorMessageType,
    ExecutorResult,
    SubsessionRequest,
    TaskExecutionState,
    _BudgetLedger,
)
from workama_agent.planner import (
    Budget,
    BudgetUsage,
    ConvergenceDecision,
    ConvergenceReason,
    PlannerError,
    PlannerLimits,
    TaskBudget,
    TaskStatus,
    allocate_task_budgets,
    blocked_task_ids,
    decompose_tasks,
    no_progress_detected,
)
from workama_agent.tool_runtime import ToolError, ToolResult


# ---------------------------------------------------------------------------
# Fake 类：模拟外部依赖（复制自 test_main_loop.py，保持测试自包含）
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


class QueueRedis:
    """按调用顺序返回预设 get 响应的 Redis fake，用于 control_checkpoint 轮询场景。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.deleted_keys: list[str] = []

    async def get(self, key):
        if self._responses:
            return self._responses.pop(0)
        return None

    async def getdel(self, key):
        self.deleted_keys.append(key)
        return None

    async def delete(self, key):
        self.deleted_keys.append(key)

    async def ping(self):
        pass

    async def aclose(self):
        pass


def _install_fakes(monkeypatch, pool=None, redis=None):
    """将 fake pool/redis 安装到 main 模块上。"""
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
    """Patch httpx.AsyncClient 使每次实例化都返回 *client*。"""
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: client)


def _patch_asyncio_sleep(monkeypatch):
    """Patch asyncio.sleep 为空操作，避免审批/轮询循环中的真实延迟。"""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))


def _clear_subscriber_state(monkeypatch):
    """重置模块级 subscriber/delivery 字典以保证测试隔离。"""
    monkeypatch.setattr(main_module, "session_subscribers", {})
    monkeypatch.setattr(main_module, "deliveries", {})


def _queue_append_events(conn: FakeConn, count: int, start_seq: int = 1) -> FakeConn:
    """为 *count* 次 append_event 调用预设 FakeConn 结果。

    每次 append_event 调用执行 UPDATE（需 fetchone）+ INSERT（不调用 fetchone），
    因此每次调用入队 2 个结果：一个含 last_seq 的 UPDATE 结果，一个空的 INSERT 结果。
    """
    for i in range(count):
        conn.queue(row={"last_seq": start_seq + i})  # UPDATE RETURNING last_seq
        conn.queue(row=None)                          # INSERT（fetchone 不被调用）
    return conn


# ===========================================================================
# main.py: send_event 广播边界测试
# ===========================================================================


def test_send_event_broadcast_false_uses_only_caller_websocket(monkeypatch):
    """broadcast=False 时即使有 session_id 也只发送给当前 websocket。"""
    _clear_subscriber_state(monkeypatch)
    ws_caller = FakeWebSocket()
    ws_other = FakeWebSocket()
    main_module.session_subscribers["ses_1"] = {ws_other}
    event = {"session_id": "ses_1", "type": "agent.thought", "payload": {"text": "hi"}}

    asyncio.run(send_event(ws_caller, event, broadcast=False))

    assert len(ws_caller.sent) == 1
    assert ws_caller.sent[0] == event
    # broadcast=False → 不应发送给其他订阅者
    assert len(ws_other.sent) == 0


def test_send_event_broadcast_with_session_no_subscribers_falls_back_to_caller(monkeypatch):
    """broadcast=True 且 session_id 有值但无订阅者时回退到当前 websocket。"""
    _clear_subscriber_state(monkeypatch)
    ws = FakeWebSocket()
    event = {"session_id": "ses_orphan", "type": "agent.thought", "payload": {"text": "hi"}}

    asyncio.run(send_event(ws, event, broadcast=True))

    assert len(ws.sent) == 1
    assert ws.sent[0] == event


def test_send_event_broadcast_true_without_session_id_uses_caller(monkeypatch):
    """broadcast=True 但事件无 session_id 时只发送给当前 websocket。"""
    _clear_subscriber_state(monkeypatch)
    ws = FakeWebSocket()
    ws_other = FakeWebSocket()
    main_module.session_subscribers["ses_1"] = {ws_other}
    event = {"type": "agent.thought", "payload": {"text": "no session"}}

    asyncio.run(send_event(ws, event, broadcast=True))

    assert len(ws.sent) == 1
    assert len(ws_other.sent) == 0


# ===========================================================================
# main.py: _send_to_socket 异常处理与 pending 跟踪测试
# ===========================================================================


def test_send_to_socket_without_delivery_state_sends_directly(monkeypatch):
    """无 DeliveryState 时直接调用 send_json，不跟踪 pending。"""
    _clear_subscriber_state(monkeypatch)
    ws = FakeWebSocket()
    event = {"type": "agent.thought", "payload": {"text": "hi"}}

    asyncio.run(_send_to_socket(ws, event))

    assert len(ws.sent) == 1
    assert ws.sent[0] == event


def test_send_to_socket_swallows_websocket_disconnect(monkeypatch):
    """send_json 抛出 WebSocketDisconnect 时静默吞掉，不向上传播。"""

    class DisconnectSocket:
        async def send_json(self, event):
            raise WebSocketDisconnect()

        async def close(self, code=1000, reason=""):
            pass

    _clear_subscriber_state(monkeypatch)
    ws = DisconnectSocket()

    # 不应抛出异常
    asyncio.run(_send_to_socket(ws, {"type": "agent.thought", "payload": {}}))


def test_send_to_socket_swallows_runtime_error(monkeypatch):
    """send_json 抛出 RuntimeError 时静默吞掉，pending 队列不更新。"""

    class ErrorSocket:
        async def send_json(self, event):
            raise RuntimeError("connection reset")

        async def close(self, code=1000, reason=""):
            pass

    _clear_subscriber_state(monkeypatch)

    async def scenario():
        ws = ErrorSocket()
        state = DeliveryState()
        main_module.deliveries[id(ws)] = state
        await _send_to_socket(ws, {"type": "agent.thought", "payload": {}, "seq": 1})
        # 异常被吞掉，pending 未追加
        assert len(state.pending) == 0
        assert state.pending_bytes == 0

    asyncio.run(scenario())


def test_send_to_socket_tracks_pending_seq_and_bytes(monkeypatch):
    """有 DeliveryState 且 seq > last_acked 时跟踪 pending 条目与字节数。"""
    _clear_subscriber_state(monkeypatch)

    async def scenario():
        ws = FakeWebSocket()
        state = DeliveryState()  # last_acked=0
        main_module.deliveries[id(ws)] = state
        event = {"type": "agent.message.delta", "payload": {"delta": "hello"}, "seq": 5}
        await _send_to_socket(ws, event)
        assert len(ws.sent) == 1
        assert state.pending[0][0] == 5
        expected_size = len(json.dumps(event, ensure_ascii=False).encode())
        assert state.pending_bytes == expected_size

    asyncio.run(scenario())


# ===========================================================================
# main.py: parse_plan_command 边界用例测试
# ===========================================================================


def test_parse_plan_command_preserves_explicit_step_ids():
    """显式提供 id 字段时保留该 id，而非生成 step_ 前缀。"""
    plan = parse_plan_command('/plan ' + json.dumps([
        {"id": "collect", "tool": "file.read", "arguments": {"path": "a.txt"}},
        {"id": "summarize", "tool": "file.write", "arguments": {"path": "b.txt", "content": "x"}},
    ]))
    assert plan[0]["id"] == "collect"
    assert plan[1]["id"] == "summarize"


def test_parse_plan_command_supports_depends_on_legacy_key():
    """使用 depends_on（而非 dependencies）作为依赖键时也能正确解析。"""
    plan = parse_plan_command('/plan ' + json.dumps([
        {"id": "step_a", "tool": "file.read", "arguments": {"path": "a.txt"}},
        {"id": "step_b", "tool": "file.write", "arguments": {"path": "b.txt"}, "depends_on": ["step_a"]},
    ]))
    assert plan[1]["dependencies"] == ["step_a"]


def test_parse_plan_command_rejects_empty_tool_string():
    """tool 字段 strip 后为空字符串时抛出 ToolError。"""
    with pytest.raises(ToolError, match="valid tool"):
        parse_plan_command('/plan ' + json.dumps([
            {"tool": "   ", "arguments": {}},
        ]))


def test_parse_plan_command_rejects_null_byte_in_tool():
    """tool 字段包含 \\x00 时抛出 ToolError。"""
    with pytest.raises(ToolError, match="valid tool"):
        parse_plan_command('/plan ' + json.dumps([
            {"tool": "file\x00read", "arguments": {}},
        ]))


def test_parse_plan_command_defaults_missing_arguments_to_empty_dict():
    """缺少 arguments 字段时默认使用空字典，不抛出异常。"""
    plan = parse_plan_command('/plan ' + json.dumps([
        {"tool": "file.read", "arguments": {"path": "a.txt"}},
        {"tool": "web_search"},  # 无 arguments 字段
    ]))
    assert plan[1]["tool"] == "web_search"
    assert plan[1]["arguments"] == {}


# ===========================================================================
# main.py: control_checkpoint 暂停/恢复/取消测试
# ===========================================================================


def test_control_checkpoint_pause_then_resume_returns(monkeypatch):
    """pause 命令后收到 resume 命令时正常返回，发送两次 session.status 事件。"""
    _clear_subscriber_state(monkeypatch)
    _patch_asyncio_sleep(monkeypatch)
    # 2 次 append_event（paused 状态 + running 状态），每次需 UPDATE + INSERT
    conn = _queue_append_events(FakeConn(), 2)
    fake_redis = QueueRedis([
        json.dumps({"action": "pause", "reason": "用户暂停"}),
        json.dumps({"action": "resume", "reason": "用户恢复"}),
    ])
    _install_fakes(monkeypatch, pool=FakePool(conn), redis=fake_redis)
    ws = FakeWebSocket()

    # 不应抛出异常
    asyncio.run(control_checkpoint(ws, "ses_1", "wsp_1"))

    # 验证发送了 paused 和 running 两个 session.status 事件
    status_events = [e for e in ws.sent if e["type"] == "session.status"]
    assert len(status_events) == 2
    assert status_events[0]["payload"]["to"] == "paused"
    assert status_events[1]["payload"]["to"] == "running"
    # resume 后命令应被删除
    assert "agent-control:ses_1" in fake_redis.deleted_keys


def test_control_checkpoint_pause_then_cancel_raises(monkeypatch):
    """pause 命令后收到 cancel 命令时抛出 RunCancelled。"""
    from workama_agent.main import RunCancelled

    _clear_subscriber_state(monkeypatch)
    _patch_asyncio_sleep(monkeypatch)
    # 1 次 append_event（paused 状态），cancel 在发送事件前抛出
    conn = _queue_append_events(FakeConn(), 1)
    fake_redis = QueueRedis([
        json.dumps({"action": "pause", "reason": "用户暂停"}),
        json.dumps({"action": "cancel", "reason": "用户取消"}),
    ])
    _install_fakes(monkeypatch, pool=FakePool(conn), redis=fake_redis)
    ws = FakeWebSocket()

    with pytest.raises(RunCancelled, match="用户取消"):
        asyncio.run(control_checkpoint(ws, "ses_1", "wsp_1"))

    # 验证只发送了 paused 状态事件（cancel 在事件发送前抛出）
    status_events = [e for e in ws.sent if e["type"] == "session.status"]
    assert len(status_events) == 1
    assert status_events[0]["payload"]["to"] == "paused"


# ===========================================================================
# main.py: attachment_context 截断与空文本测试
# ===========================================================================


def test_attachment_context_truncates_to_budget(monkeypatch):
    """提取文本超过 12000 字符预算时截断，并跳过后续附件。"""
    long_text = "A" * 15000
    rows = [
        {"filename": "big.txt", "extracted_text": long_text},
        {"filename": "small.txt", "extracted_text": "small content"},
    ]
    conn = FakeConn().queue(rows=rows)
    _install_fakes(monkeypatch, pool=FakePool(conn))

    result = asyncio.run(attachment_context("ses_1", "wsp_1", ["att_1", "att_2"]))

    assert "File: big.txt" in result
    # 第二个文件因预算耗尽被跳过
    assert "small.txt" not in result
    # 截断为 12000 字符（"Attachment" 前缀中也有 A，所以用子串断言更精确）
    assert "A" * 12000 in result
    assert "A" * 12001 not in result


def test_attachment_context_skips_empty_extracted_text(monkeypatch):
    """extracted_text 为空字符串或 None 时跳过该附件。"""
    rows = [
        {"filename": "empty.txt", "extracted_text": ""},
        {"filename": "none.txt", "extracted_text": None},
        {"filename": "valid.txt", "extracted_text": "valid content"},
    ]
    conn = FakeConn().queue(rows=rows)
    _install_fakes(monkeypatch, pool=FakePool(conn))

    result = asyncio.run(attachment_context("ses_1", "wsp_1", ["att_1", "att_2", "att_3"]))

    assert "empty.txt" not in result
    assert "none.txt" not in result
    assert "valid.txt" in result
    assert "valid content" in result


# ===========================================================================
# main.py: execute_tool 非沙箱工具与工件事件测试
# ===========================================================================


def test_execute_tool_non_sandbox_skips_sandbox_status_event(monkeypatch):
    """非沙箱工具（如 web_search, sandbox=False）执行时不发送 sandbox.status 事件。"""
    _clear_subscriber_state(monkeypatch)
    # 3 次 append_event: tool.call, tool.result, step.finished
    conn = _queue_append_events(FakeConn(), 3)
    _install_fakes(monkeypatch, pool=FakePool(conn))
    ws = FakeWebSocket()

    fake_runtime = MagicMock()
    fake_runtime.execute = AsyncMock(return_value=ToolResult(
        "succeeded", "Found 3 references", [{"title": "x"}], untrusted=True
    ))
    monkeypatch.setattr(main_module, "tool_runtime", fake_runtime)

    result = asyncio.run(execute_tool(ws, "ses_1", "wsp_1", "user_1", "web_search", {"query": "test"}))

    event_types = [e["type"] for e in ws.sent]
    assert event_types == ["tool.call", "tool.result", "step.finished"]
    assert "sandbox.status" not in event_types
    assert result["status"] == "succeeded"
    assert result["artifact_refs"] == []


def test_execute_tool_with_artifact_emits_artifact_created_event(monkeypatch):
    """工具返回 artifact 时发送 artifact.created 事件并附带 artifact_ref。"""
    _clear_subscriber_state(monkeypatch)
    # 5 次 append_event: tool.call, sandbox.status, artifact.created, tool.result, step.finished
    conn = _queue_append_events(FakeConn(), 5)
    _install_fakes(monkeypatch, pool=FakePool(conn))
    ws = FakeWebSocket()

    fake_runtime = MagicMock()
    fake_runtime.execute = AsyncMock(return_value=ToolResult(
        "succeeded", "Wrote a.txt", {"path": "a.txt", "size": 5},
        artifact={"name": "a.txt", "content_type": "text/plain", "content": "hello"},
    ))
    monkeypatch.setattr(main_module, "tool_runtime", fake_runtime)

    fake_client = FakeHttpxClient().queue_post(
        FakeResponse(json_data={"id": "art_1", "name": "a.txt", "content_type": "text/plain"})
    )
    _patch_httpx(monkeypatch, fake_client)

    result = asyncio.run(execute_tool(ws, "ses_1", "wsp_1", "user_1", "file.write", {"path": "a.txt", "content": "hello"}))

    event_types = [e["type"] for e in ws.sent]
    assert event_types == ["tool.call", "sandbox.status", "artifact.created", "tool.result", "step.finished"]
    assert result["artifact_refs"] == ["art_1"]
    artifact_event = next(e for e in ws.sent if e["type"] == "artifact.created")
    assert artifact_event["payload"]["artifact_id"] == "art_1"
    assert artifact_event["payload"]["name"] == "a.txt"


# ===========================================================================
# main.py: await_approval consume 失败测试
# ===========================================================================


def test_await_approval_consume_failure_reraises(monkeypatch):
    """审批通过但 consume 接口返回 HTTP 错误时重新抛出异常。"""
    _clear_subscriber_state(monkeypatch)
    _patch_asyncio_sleep(monkeypatch)
    # 1 次 append_event: tool.approval_required（consume 失败前 approval_decided 未发送）
    conn = _queue_append_events(FakeConn(), 1)
    _install_fakes(monkeypatch, pool=FakePool(conn))
    ws = FakeWebSocket()

    fake_client = (
        FakeHttpxClient()
        .queue_post(FakeResponse(json_data={"id": "appr_1", "expires_at": "2026-01-01T00:00:00Z"}))  # create
        .queue_get(FakeResponse(json_data={"status": "pending", "decided_by": None}))                # check 1
        .queue_get(FakeResponse(json_data={"status": "approved", "decided_by": "user_1"}))           # check 2
        .queue_post(FakeResponse(raise_exc=_make_http_status_error(500)))                            # consume 失败
    )
    _patch_httpx(monkeypatch, fake_client)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(await_approval(
            ws, "ses_1", "wsp_1", "user_1", "call_1", "terminal", "A3", "hash123",
            {"argv": "<1 chars>"},
        ))

    # 验证只发送了 tool.approval_required 事件（approval_decided 未发送）
    event_types = [e["type"] for e in ws.sent]
    assert "tool.approval_required" in event_types
    assert "tool.approval_decided" not in event_types


# ===========================================================================
# coordination.py: SubsessionRequest 校验与序列化测试
# ===========================================================================


def test_subsession_request_rejects_empty_required_field():
    """必填字段为空字符串时抛出 PlannerError。"""
    with pytest.raises(PlannerError, match="request_id is invalid"):
        SubsessionRequest(
            request_id="",
            parent_session_id="ses_parent",
            child_session_id="ses_child",
            workspace_id="wsp_test",
            task_id="task_1",
            objective="Objective",
            budget=TaskBudget(1, 1.0),
            idempotency_key="key",
            depth=1,
        )


def test_subsession_request_rejects_depth_below_one():
    """depth < 1 时抛出 PlannerError。"""
    with pytest.raises(PlannerError, match="depth must be at least 1"):
        SubsessionRequest(
            request_id="req_1",
            parent_session_id="ses_parent",
            child_session_id="ses_child",
            workspace_id="wsp_test",
            task_id="task_1",
            objective="Objective",
            budget=TaskBudget(1, 1.0),
            idempotency_key="key",
            depth=0,
        )


def test_subsession_request_to_dict_round_trip():
    """to_dict 包含所有字段且 context_refs/capabilities 转为 list。"""
    request = SubsessionRequest(
        request_id="req_1",
        parent_session_id="ses_parent",
        child_session_id="ses_child",
        workspace_id="wsp_test",
        task_id="task_1",
        objective="Inspect the source",
        budget=TaskBudget(3, 2.5),
        idempotency_key="task-key",
        depth=1,
        context_refs=("session:summary",),
        capabilities=("file.read",),
        actor_ref="user_1",
        trace_id="trace_1",
    )
    d = request.to_dict()
    assert d["request_id"] == "req_1"
    assert d["parent_session_id"] == "ses_parent"
    assert d["child_session_id"] == "ses_child"
    assert d["workspace_id"] == "wsp_test"
    assert d["task_id"] == "task_1"
    assert d["objective"] == "Inspect the source"
    assert d["budget"] == {"max_steps": 3, "max_credits": 2.5}
    assert d["idempotency_key"] == "task-key"
    assert d["depth"] == 1
    assert d["executor"] == "default"
    assert d["context_refs"] == ["session:summary"]
    assert d["capabilities"] == ["file.read"]
    assert d["actor_ref"] == "user_1"
    assert d["trace_id"] == "trace_1"
    assert d["schema_version"] == "1.0"


# ===========================================================================
# coordination.py: ExecutorResult 校验与序列化测试
# ===========================================================================


def test_executor_result_from_value_with_mapping():
    """from_value 接收 dict 时正确构造 ExecutorResult。"""
    value = {
        "task_id": "task_1",
        "status": "succeeded",
        "summary": "done",
        "output_ref": "artifact:1",
        "usage": {"steps": 2, "credits": 1.5},
        "progress_marker": "marker",
        "error_code": None,
        "metadata": {"key": "value"},
    }
    result = ExecutorResult.from_value(value)
    assert result.task_id == "task_1"
    assert result.status == TaskStatus.SUCCEEDED
    assert result.summary == "done"
    assert result.output_ref == "artifact:1"
    assert result.usage.steps == 2
    assert result.usage.credits == 1.5
    assert result.progress_marker == "marker"
    assert result.metadata == {"key": "value"}


def test_executor_result_from_value_passthrough_existing_instance():
    """from_value 接收已存在的 ExecutorResult 时直接返回同一实例。"""
    original = ExecutorResult(task_id="task_1", status="succeeded", summary="done")
    result = ExecutorResult.from_value(original)
    assert result is original


def test_executor_result_rejects_non_terminal_status():
    """非终态状态（如 running）抛出 PlannerError。"""
    with pytest.raises(PlannerError, match="executor result must be terminal"):
        ExecutorResult(task_id="task_1", status="running")


def test_executor_result_rejects_invalid_status_string():
    """无法识别的状态字符串抛出 PlannerError。"""
    with pytest.raises(PlannerError, match="unsupported status"):
        ExecutorResult(task_id="task_1", status="bogus")


def test_executor_result_to_dict_round_trip():
    """to_dict 正确序列化所有字段。"""
    result = ExecutorResult(
        task_id="task_1",
        status="failed",
        summary="boom",
        output_ref="ref_1",
        usage=BudgetUsage(3, 2.0),
        progress_marker="m",
        error_code="E07001",
        metadata={"k": "v"},
    )
    d = result.to_dict()
    assert d["task_id"] == "task_1"
    assert d["status"] == "failed"
    assert d["summary"] == "boom"
    assert d["output_ref"] == "ref_1"
    assert d["usage"] == {"steps": 3, "credits": 2.0}
    assert d["progress_marker"] == "m"
    assert d["error_code"] == "E07001"
    assert d["metadata"] == {"k": "v"}


# ===========================================================================
# coordination.py: ExecutorMessage 校验测试
# ===========================================================================


def test_executor_message_rejects_unknown_event_type():
    """未知 event_type 字符串抛出 PlannerError。"""
    with pytest.raises(PlannerError, match="unsupported executor message type"):
        ExecutorMessage(
            event_type="unknown.type",
            message_id="msg_1",
            request_id="req_1",
            task_id="task_1",
            parent_session_id="ses_parent",
            child_session_id="ses_child",
            workspace_id="wsp_test",
            payload={},
        )


def test_executor_message_rejects_non_mapping_payload():
    """payload 为非 Mapping 类型（如 list）时抛出 PlannerError。"""
    with pytest.raises(PlannerError, match="payload must be an object"):
        ExecutorMessage(
            event_type=ExecutorMessageType.TASK_ASSIGNED,
            message_id="msg_1",
            request_id="req_1",
            task_id="task_1",
            parent_session_id="ses_parent",
            child_session_id="ses_child",
            workspace_id="wsp_test",
            payload=["not", "a", "dict"],
        )


def test_executor_message_to_dict_shape():
    """to_dict 包含 event_id、event_type 等关键字段。"""
    msg = ExecutorMessage(
        event_type=ExecutorMessageType.TASK_COMPLETED,
        message_id="msg_42",
        request_id="req_1",
        task_id="task_1",
        parent_session_id="ses_parent",
        child_session_id="ses_child",
        workspace_id="wsp_test",
        payload={"status": "succeeded"},
        idempotency_key="key_1",
        actor_ref="user_1",
        trace_id="trace_1",
        occurred_at="2026-01-01T00:00:00Z",
    )
    d = msg.to_dict()
    assert d["event_id"] == "msg_42"
    assert d["message_id"] == "msg_42"
    assert d["event_type"] == "task.completed"
    assert d["producer"] == "agent-coordinator"
    assert d["workspace_id"] == "wsp_test"
    assert d["parent_session_id"] == "ses_parent"
    assert d["child_session_id"] == "ses_child"
    assert d["task_id"] == "task_1"
    assert d["request_id"] == "req_1"
    assert d["idempotency_key"] == "key_1"
    assert d["actor_ref"] == "user_1"
    assert d["trace_id"] == "trace_1"
    assert d["classification"] == "C2"
    assert d["occurred_at"] == "2026-01-01T00:00:00Z"
    assert d["payload"] == {"status": "succeeded"}


# ===========================================================================
# coordination.py: TaskExecutionState 序列化测试
# ===========================================================================


def test_task_execution_state_to_dict_without_result():
    """无 result 时 to_dict 的 result 字段为 None。"""
    state = TaskExecutionState(task_id="task_1")
    d = state.to_dict()
    assert d["task_id"] == "task_1"
    assert d["status"] == "pending"
    assert d["result"] is None
    assert d["attempt"] == 0
    assert d["request_id"] is None
    assert d["child_session_id"] is None
    assert d["error"] is None
    assert d["progress_marker"] is None


def test_task_execution_state_to_dict_with_result():
    """有 result 时 to_dict 包含完整 result 字典。"""
    result = ExecutorResult(task_id="task_1", status="succeeded", summary="done")
    state = TaskExecutionState(
        task_id="task_1",
        status=TaskStatus.SUCCEEDED,
        attempt=2,
        request_id="req_1",
        child_session_id="ses_child",
        result=result,
        progress_marker="marker_1",
    )
    d = state.to_dict()
    assert d["status"] == "succeeded"
    assert d["attempt"] == 2
    assert d["request_id"] == "req_1"
    assert d["child_session_id"] == "ses_child"
    assert d["progress_marker"] == "marker_1"
    assert d["result"]["task_id"] == "task_1"
    assert d["result"]["status"] == "succeeded"


# ===========================================================================
# coordination.py: _BudgetLedger 预算管理测试
# ===========================================================================


@pytest.mark.asyncio
async def test_budget_ledger_reserve_fails_when_exceeding_budget():
    """预留超出父预算时 reserve 返回 False。"""
    ledger = _BudgetLedger(Budget(max_steps=5, max_credits=5.0))
    # 预留 3 步 → 成功
    assert await ledger.reserve(TaskBudget(3, 3.0)) is True
    # 再预留 3 步 → 超出（3+3 > 5）
    assert await ledger.reserve(TaskBudget(3, 1.0)) is False
    # 再预留 3 credits → 超出（3+3 > 5）
    assert await ledger.reserve(TaskBudget(1, 3.0)) is False


@pytest.mark.asyncio
async def test_budget_ledger_settle_detects_overrun():
    """settle 检测超出 grant 或父预算的情况并返回 False。"""
    # 超出 grant
    ledger1 = _BudgetLedger(Budget(max_steps=5, max_credits=5.0))
    grant1 = TaskBudget(3, 3.0)
    await ledger1.reserve(grant1)
    assert await ledger1.settle(grant1, BudgetUsage(4, 1.0)) is False  # 4 > 3

    # 超出父预算
    ledger2 = _BudgetLedger(Budget(max_steps=5, max_credits=5.0, used_steps=4, used_credits=4.0))
    grant2 = TaskBudget(3, 3.0)
    await ledger2.reserve(grant2)
    assert await ledger2.settle(grant2, BudgetUsage(3, 3.0)) is False  # 4+3 > 5

    # 在 grant 与父预算内
    ledger3 = _BudgetLedger(Budget(max_steps=5, max_credits=5.0))
    grant3 = TaskBudget(3, 3.0)
    await ledger3.reserve(grant3)
    assert await ledger3.settle(grant3, BudgetUsage(2, 2.0)) is True


@pytest.mark.asyncio
async def test_budget_ledger_snapshot_reflects_settled_usage():
    """settle 后 snapshot 反映已使用的步骤与 credits。"""
    ledger = _BudgetLedger(Budget(max_steps=10, max_credits=10.0))
    grant = TaskBudget(5, 5.0)
    await ledger.reserve(grant)
    await ledger.settle(grant, BudgetUsage(3, 2.0))
    snapshot = ledger.snapshot()
    assert snapshot.used_steps == 3
    assert snapshot.used_credits == 2.0
    assert snapshot.remaining_steps == 7
    assert snapshot.remaining_credits == 8.0


# ===========================================================================
# coordination.py: Coordinator 静态方法与异常传播测试
# ===========================================================================


def test_coordinator_result_status_mapping():
    """_result_status 将 ConvergenceReason 正确映射到 CoordinationStatus。"""
    # COMPLETE → SUCCEEDED
    d1 = ConvergenceDecision(True, ConvergenceReason.COMPLETE, "done", 1, 0)
    assert Coordinator._result_status(d1) == CoordinationStatus.SUCCEEDED
    # FAILED → FAILED
    d2 = ConvergenceDecision(True, ConvergenceReason.FAILED, "fail", 0, 1)
    assert Coordinator._result_status(d2) == CoordinationStatus.FAILED
    # BLOCKED → FAILED
    d3 = ConvergenceDecision(True, ConvergenceReason.BLOCKED, "blocked", 0, 1)
    assert Coordinator._result_status(d3) == CoordinationStatus.FAILED
    # 其他 → STOPPED
    for reason in (ConvergenceReason.NO_PROGRESS, ConvergenceReason.DEPTH_LIMIT,
                   ConvergenceReason.BUDGET_EXHAUSTED, ConvergenceReason.AGENT_LIMIT):
        d = ConvergenceDecision(True, reason, "stopped", 0, 1)
        assert Coordinator._result_status(d) == CoordinationStatus.STOPPED


def test_coordinator_effective_limits_takes_min():
    """_effective_limits 对每个字段取 plan.limits 与 self.limits 的最小值。"""
    coord = Coordinator(
        executor=MagicMock(),
        limits=PlannerLimits(
            max_steps=10, max_credits=10.0, max_depth=1,
            max_concurrency=2, max_agents=5, no_progress_rounds=2,
        ),
    )
    plan = decompose_tasks(
        "Goal",
        [{"id": "a", "objective": "A"}],
        limits=PlannerLimits(
            max_steps=20, max_credits=20.0, max_depth=2,
            max_concurrency=3, max_agents=8, no_progress_rounds=5,
        ),
    )
    effective = coord._effective_limits(plan)
    assert effective.max_steps == 10
    assert effective.max_credits == 10.0
    assert effective.max_depth == 1
    assert effective.max_concurrency == 2
    assert effective.max_agents == 5
    assert effective.no_progress_rounds == 2


@pytest.mark.asyncio
async def test_coordinator_propagates_executor_exception_as_failed():
    """执行器抛出通用异常时任务标记为 FAILED，并发出 TASK_FAILED 消息。"""

    class RaisingExecutor:
        async def execute(self, request, emit):
            raise RuntimeError("executor boom")

    plan = decompose_tasks(
        "Goal",
        [{"id": "a", "objective": "A"}],
        limits=PlannerLimits(max_steps=5, max_credits=5.0),
    )
    result = await Coordinator(RaisingExecutor()).run(
        plan, parent_session_id="ses_parent", workspace_id="wsp_test",
    )

    assert result.status == CoordinationStatus.FAILED
    assert result.reason == ConvergenceReason.FAILED
    state = result.states[0]
    assert state.status == TaskStatus.FAILED
    assert "executor boom" in state.error
    assert state.result is not None
    assert state.result.error_code == "E07001"  # 通用异常默认错误码
    # 消息序列应包含 TASK_ASSIGNED、TASK_ACCEPTED、TASK_FAILED
    event_types = [msg.event_type for msg in result.messages]
    assert ExecutorMessageType.TASK_ASSIGNED in event_types
    assert ExecutorMessageType.TASK_ACCEPTED in event_types
    assert ExecutorMessageType.TASK_FAILED in event_types


# ===========================================================================
# planner.py: PlannerLimits 多字段校验测试
# ===========================================================================


@pytest.mark.parametrize("field,value,code", [
    ("max_steps", 0, "E04003"),
    ("max_steps", True, "E04003"),       # bool 被拒绝
    ("max_credits", 0, "E04002"),
    ("max_credits", -1.0, "E04002"),
    ("max_depth", -1, "E07006"),
    ("max_depth", 3, "E07006"),          # 超过 GLOBAL_MAX_DEPTH=2
    ("max_concurrency", 0, "E07006"),
    ("max_concurrency", 4, "E07006"),    # 超过 GLOBAL_MAX_CONCURRENCY=3
    ("max_agents", 0, "E07006"),
    ("max_agents", 9, "E07006"),         # 超过 GLOBAL_MAX_AGENTS=8
    ("no_progress_rounds", 0, "E07006"),
])
def test_planner_limits_rejects_invalid_values(field, value, code):
    """PlannerLimits 对各字段做边界校验，越界时抛出对应 code 的 PlannerError。"""
    kwargs = {
        "max_steps": 5, "max_credits": 5.0, "max_depth": 2,
        "max_concurrency": 3, "max_agents": 8, "no_progress_rounds": 3,
    }
    kwargs[field] = value
    with pytest.raises(PlannerError) as excinfo:
        PlannerLimits(**kwargs)
    assert excinfo.value.code == code


# ===========================================================================
# planner.py: Budget 消费与属性测试
# ===========================================================================


def test_budget_consume_returns_new_budget_with_updated_usage():
    """consume 成功时返回新的 Budget，原 Budget 不变（frozen）。"""
    budget = Budget(max_steps=10, max_credits=10.0)
    consumed = budget.consume(BudgetUsage(3, 2.0))
    assert consumed.used_steps == 3
    assert consumed.used_credits == 2.0
    assert consumed.remaining_steps == 7
    assert consumed.remaining_credits == 8.0
    # 原 Budget 未变
    assert budget.used_steps == 0
    assert budget.used_credits == 0.0


def test_budget_consume_raises_when_exceeding():
    """consume 超出剩余预算时抛出 PlannerError。"""
    budget = Budget(max_steps=5, max_credits=5.0)
    with pytest.raises(PlannerError, match="budget would be exceeded"):
        budget.consume(BudgetUsage(6, 1.0))  # steps 超出
    with pytest.raises(PlannerError, match="budget would be exceeded"):
        budget.consume(BudgetUsage(1, 6.0))  # credits 超出


def test_budget_exhausted_and_remaining_properties():
    """exhausted 在 steps 或 credits 耗尽时为 True；remaining 钳制到 0。"""
    # 未耗尽
    b1 = Budget(max_steps=5, max_credits=5.0, used_steps=3, used_credits=2.0)
    assert not b1.exhausted
    assert b1.remaining_steps == 2
    assert b1.remaining_credits == 3.0
    # steps 耗尽
    b2 = Budget(max_steps=5, max_credits=5.0, used_steps=5, used_credits=2.0)
    assert b2.exhausted
    assert b2.remaining_steps == 0
    # credits 耗尽
    b3 = Budget(max_steps=5, max_credits=5.0, used_steps=2, used_credits=5.0)
    assert b3.exhausted
    assert b3.remaining_credits == 0.0


def test_budget_usage_addition():
    """BudgetUsage.__add__ 正确累加 steps 与 credits。"""
    a = BudgetUsage(2, 1.5)
    b = BudgetUsage(3, 2.5)
    c = a + b
    assert c.steps == 5
    assert c.credits == 4.0


# ===========================================================================
# planner.py: TaskPlan.budget_for 测试
# ===========================================================================


def test_task_plan_budget_for_returns_task_budget():
    """budget_for 已知任务 ID 时返回对应的 TaskBudget。"""
    plan = decompose_tasks(
        "Goal",
        [{"id": "a", "objective": "A", "estimated_steps": 3, "estimated_credits": 2.5}],
    )
    budget = plan.budget_for("a")
    assert budget.max_steps == 3
    assert budget.max_credits == 2.5


def test_task_plan_budget_for_raises_for_unknown_task():
    """budget_for 未知任务 ID 时抛出 PlannerError。"""
    plan = decompose_tasks("Goal", [{"id": "a", "objective": "A"}])
    with pytest.raises(PlannerError, match="task not found"):
        plan.budget_for("nonexistent")


# ===========================================================================
# planner.py: blocked_task_ids 与 allocate_task_budgets 测试
# ===========================================================================


def test_blocked_task_ids_returns_tasks_with_failed_dependencies():
    """依赖任务处于失败/取消/阻塞态时，被依赖任务出现在 blocked 列表中。"""
    plan = decompose_tasks("Goal", [
        {"id": "a", "objective": "A"},
        {"id": "b", "objective": "B", "dependencies": ["a"]},
        {"id": "c", "objective": "C"},
    ])
    # 无失败依赖 → 无阻塞
    assert blocked_task_ids(plan, {}) == ()
    # a 失败 → b 阻塞
    assert blocked_task_ids(plan, {"a": TaskStatus.FAILED}) == ("b",)
    # a 取消 → b 阻塞
    assert blocked_task_ids(plan, {"a": TaskStatus.CANCELLED}) == ("b",)
    # a 阻塞 → b 阻塞
    assert blocked_task_ids(plan, {"a": TaskStatus.BLOCKED}) == ("b",)


def test_allocate_task_budgets_raises_on_credit_overrun():
    """子任务估算 credits 超出父预算剩余值时抛出 PlannerError。

    使用 used_credits 让 validate_plan 通过（estimated <= max）但
    allocate_task_budgets 的剩余预算检查失败（estimated > remaining）。
    """
    plan = decompose_tasks(
        "Goal",
        [{"id": "a", "objective": "A", "estimated_credits": 5.0}],
    )
    budget = Budget(max_steps=10, max_credits=10.0, used_credits=8.0)
    with pytest.raises(PlannerError, match="credit estimates exceed the remaining parent budget"):
        allocate_task_budgets(plan.tasks, budget)


def test_allocate_task_budgets_raises_on_step_overrun():
    """子任务估算 steps 超出父预算剩余值时抛出 PlannerError。

    使用 used_steps 让 validate_plan 通过（estimated <= max）但
    allocate_task_budgets 的剩余预算检查失败（estimated > remaining）。
    """
    plan = decompose_tasks(
        "Goal",
        [{"id": "a", "objective": "A", "estimated_steps": 5}],
    )
    budget = Budget(max_steps=10, max_credits=10.0, used_steps=8)
    with pytest.raises(PlannerError, match="step estimates exceed the remaining parent budget"):
        allocate_task_budgets(plan.tasks, budget)


# ===========================================================================
# planner.py: no_progress_detected 与 ConvergenceDecision 测试
# ===========================================================================


def test_no_progress_detected_window_must_be_positive():
    """window < 1 时抛出 PlannerError。"""
    with pytest.raises(PlannerError, match="no-progress window must be positive"):
        no_progress_detected(["a", "b"], window=0)


def test_no_progress_detected_returns_false_for_short_history():
    """历史记录数不足 window 时返回 False。"""
    assert no_progress_detected(["same", "same"], window=3) is False
    assert no_progress_detected([], window=3) is False


def test_convergence_decision_to_dict_serializes_fields():
    """ConvergenceDecision.to_dict 正确序列化所有字段。"""
    decision = ConvergenceDecision(
        should_stop=True,
        reason=ConvergenceReason.COMPLETE,
        message="all done",
        completed=3,
        remaining=0,
    )
    d = decision.to_dict()
    assert d["should_stop"] is True
    assert d["reason"] == "complete"
    assert d["message"] == "all done"
    assert d["completed"] == 3
    assert d["remaining"] == 0
