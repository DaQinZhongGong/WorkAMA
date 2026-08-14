"""mcp_test_server 模块单元测试。

覆盖范围：
- 常量导出（JSONRPC_VERSION / PROTOCOL_VERSION / SERVER_NAME / SERVER_VERSION）
- 响应构造辅助（_ok / _err）
- 协议初始化（_initialize_result / 能力协商 / protocolVersion 默认与回退）
- 工具列表（_tools_list / echo 工具 schema）
- JSON-RPC 请求处理（handle_request）：
  * initialize / tools/list / tools/call 成功路径
  * unknown tool / unknown method / 缺失 method 错误码
  * echo 参数校验（非字符串 / 超长文本）
  * notifications/initialized 返回 None
- SSE ASGI 应用（sse_asgi_app）：
  * lifespan 启动/关闭
  * 未识别路径返回 404
  * GET /sse 推送 endpoint 事件 + keepalive
  * POST /messages 未识别 session 返回 400
  * POST /messages 非法 JSON 返回 202
  * POST /messages 合法请求返回 202 并将响应推入 session 队列
- 辅助函数（_session_id_from_scope / _send_json）
- CLI 入口（main 参数解析）

测试风格：直接调用纯函数 + 构造 ASGI scope/receive/send，无外部依赖。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from workama_platform.modules import mcp_test_server as mts


# ============================================================================
# 常量导出
# ============================================================================


def test_constants_expose_protocol_identity():
    """模块导出 JSON-RPC 版本与服务器标识常量。"""
    assert mts.JSONRPC_VERSION == "2.0"
    assert mts.PROTOCOL_VERSION == "2025-06-18"
    assert mts.SERVER_NAME == "workama-mcp-test-server"
    assert mts.SERVER_VERSION == "1.0.0"


# ============================================================================
# 响应构造辅助
# ============================================================================


def test_ok_response_carries_result_and_request_id():
    response = mts._ok(7, {"hello": "world"})
    assert response == {"jsonrpc": "2.0", "id": 7, "result": {"hello": "world"}}


def test_err_response_carries_code_and_message():
    response = mts._err("req-1", -32601, "Method not found")
    assert response == {
        "jsonrpc": "2.0",
        "id": "req-1",
        "error": {"code": -32601, "message": "Method not found"},
    }


# ============================================================================
# 协议初始化
# ============================================================================


def test_initialize_result_returns_server_capabilities_and_info():
    request = {"method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
    result = mts._initialize_result(request)
    assert result["protocolVersion"] == "2024-11-05"
    assert "tools" in result["capabilities"]
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"] == {
        "name": "workama-mcp-test-server",
        "version": "1.0.0",
    }


def test_initialize_result_falls_back_to_default_protocol_version():
    request = {"method": "initialize"}
    result = mts._initialize_result(request)
    assert result["protocolVersion"] == mts.PROTOCOL_VERSION


def test_initialize_result_handles_missing_params_dict():
    """params 缺失或为 None 时不应抛出。"""
    result = mts._initialize_result({"method": "initialize", "params": None})
    assert result["protocolVersion"] == mts.PROTOCOL_VERSION


# ============================================================================
# 工具列表
# ============================================================================


def test_tools_list_exposes_single_echo_tool():
    tools = mts._tools_list()["tools"]
    assert len(tools) == 1
    echo = tools[0]
    assert echo["name"] == "echo"
    assert "description" in echo
    schema = echo["inputSchema"]
    assert schema["type"] == "object"
    assert "text" in schema["properties"]
    assert schema["required"] == ["text"]


# ============================================================================
# handle_request
# ============================================================================


def test_handle_request_initialize_returns_ok_with_capabilities():
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"},
    }
    response = mts.handle_request(request)
    assert response is not None
    assert response["id"] == 1
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert "capabilities" in response["result"]


def test_handle_request_tools_list_returns_echo_tool():
    response = mts.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert response is not None
    assert response["id"] == 2
    tools = response["result"]["tools"]
    assert any(tool["name"] == "echo" for tool in tools)


def test_handle_request_tools_call_echo_returns_text_content():
    response = mts.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hello world"}},
        }
    )
    assert response is not None
    content = response["result"]["content"]
    assert content == [{"type": "text", "text": "hello world"}]
    assert response["result"]["isError"] is False


def test_handle_request_tools_call_unknown_tool_returns_32601():
    response = mts.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        }
    )
    assert response is not None
    assert response["error"]["code"] == -32601
    assert "Unknown tool" in response["error"]["message"]


def test_handle_request_tools_call_echo_non_string_text_returns_32602():
    response = mts.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": 42}},
        }
    )
    assert response is not None
    assert response["error"]["code"] == -32602
    assert "string argument" in response["error"]["message"]


def test_handle_request_tools_call_echo_missing_text_returns_32602():
    response = mts.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {}},
        }
    )
    assert response is not None
    assert response["error"]["code"] == -32602


def test_handle_request_tools_call_echo_oversized_text_returns_32602():
    response = mts.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "x" * 8193}},
        }
    )
    assert response is not None
    assert response["error"]["code"] == -32602
    assert "too long" in response["error"]["message"]


def test_handle_request_tools_call_with_missing_params_returns_unknown_tool():
    """params 缺失时，name 为 None，应返回 Unknown tool: None。"""
    response = mts.handle_request({"jsonrpc": "2.0", "id": 8, "method": "tools/call"})
    assert response is not None
    assert response["error"]["code"] == -32601


def test_handle_request_unknown_method_returns_32601():
    response = mts.handle_request(
        {"jsonrpc": "2.0", "id": 9, "method": "resources/read", "params": {}}
    )
    assert response is not None
    assert response["error"]["code"] == -32601
    assert "Method not found" in response["error"]["message"]


def test_handle_request_initialized_notification_returns_none():
    """notifications/initialized 是通知（无 id），返回 None 表示无响应。"""
    response = mts.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response is None


def test_handle_request_missing_method_returns_32601():
    response = mts.handle_request({"jsonrpc": "2.0", "id": 10})
    assert response is not None
    assert response["error"]["code"] == -32601
    assert "Method not found" in response["error"]["message"]


# ============================================================================
# _session_id_from_scope
# ============================================================================


def test_session_id_from_scope_supports_session_id_param():
    scope = {"query_string": b"session_id=abc123"}
    assert mts._session_id_from_scope(scope) == "abc123"


def test_session_id_from_scope_supports_sid_alias():
    scope = {"query_string": b"sid=xyz"}
    assert mts._session_id_from_scope(scope) == "xyz"


def test_session_id_from_scope_returns_none_when_missing():
    assert mts._session_id_from_scope({"query_string": b""}) is None
    assert mts._session_id_from_scope({}) is None


def test_session_id_from_scope_handles_str_query_string():
    scope = {"query_string": "session_id=str-form"}
    assert mts._session_id_from_scope(scope) == "str-form"


# ============================================================================
# _send_json
# ============================================================================


@pytest.mark.asyncio
async def test_send_json_writes_http_response_with_json_payload():
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await mts._send_json(send, 201, {"ok": True})

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 201
    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"] == b"application/json"

    assert sent[1]["type"] == "http.response.body"
    payload = json.loads(sent[1]["body"].decode("utf-8"))
    assert payload == {"ok": True}
    assert sent[1]["body"] == b'{"ok":true}'


# ============================================================================
# sse_asgi_app: lifespan
# ============================================================================


@pytest.mark.asyncio
async def test_sse_asgi_app_lifespan_completes_startup_and_shutdown():
    events = [
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ]
    sent: list[dict] = []

    async def receive():
        return events.pop(0)

    async def send(message):
        sent.append(message)

    await mts.sse_asgi_app({"type": "lifespan"}, receive, send)

    assert {"type": "lifespan.startup.complete"} in sent
    assert {"type": "lifespan.shutdown.complete"} in sent


@pytest.mark.asyncio
async def test_sse_asgi_app_unknown_http_path_returns_404():
    sent: list[dict] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await mts.sse_asgi_app(
        {"type": "http", "path": "/unknown", "method": "GET"}, receive, send
    )

    assert sent[0]["status"] == 404
    payload = json.loads(sent[1]["body"].decode("utf-8"))
    assert payload == {"error": "not found"}


@pytest.mark.asyncio
async def test_sse_asgi_app_non_http_scope_returns_silently():
    """非 http / lifespan 的 scope 类型应静默返回。"""
    sent: list[dict] = []

    async def receive():
        return {}

    async def send(message):
        sent.append(message)

    # 应该不抛异常也不发送任何消息
    await mts.sse_asgi_app({"type": "websocket"}, receive, send)
    assert sent == []


# ============================================================================
# sse_asgi_app: POST /messages
# ============================================================================


@pytest.mark.asyncio
async def test_post_messages_unknown_session_returns_400():
    """未注册的 session_id 返回 400。"""
    sent: list[dict] = []
    request_body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()

    async def receive():
        return {"type": "http.request", "body": request_body, "more_body": False}

    async def send(message):
        sent.append(message)

    await mts.sse_asgi_app(
        {
            "type": "http",
            "path": "/messages",
            "method": "POST",
            "query_string": b"session_id=unknown-session",
        },
        receive,
        send,
    )

    assert sent[0]["status"] == 400
    payload = json.loads(sent[1]["body"].decode("utf-8"))
    assert payload == {"error": "unknown session"}


@pytest.mark.asyncio
async def test_post_messages_malformed_json_returns_202():
    """非法 JSON 仍返回 202（已接受，无响应可发）。"""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"not-valid-json", "more_body": False}

    async def send(message):
        sent.append(message)

    # 先注册一个 session
    queue: asyncio.Queue = asyncio.Queue()
    mts._SSE_SESSIONS["registered"] = queue

    try:
        await mts.sse_asgi_app(
            {
                "type": "http",
                "path": "/messages",
                "method": "POST",
                "query_string": b"session_id=registered",
            },
            receive,
            send,
        )
    finally:
        mts._SSE_SESSIONS.pop("registered", None)

    assert sent[0]["status"] == 202
    # 队列应保持空（无响应可推）
    assert queue.empty()


@pytest.mark.asyncio
async def test_post_messages_valid_request_enqueues_response_and_returns_202():
    sent: list[dict] = []
    request_body = json.dumps(
        {"jsonrpc": "2.0", "id": 42, "method": "initialize"}
    ).encode()

    async def receive():
        return {"type": "http.request", "body": request_body, "more_body": False}

    async def send(message):
        sent.append(message)

    queue: asyncio.Queue = asyncio.Queue()
    mts._SSE_SESSIONS["enqueue-test"] = queue

    try:
        await mts.sse_asgi_app(
            {
                "type": "http",
                "path": "/messages",
                "method": "POST",
                "query_string": b"session_id=enqueue-test",
            },
            receive,
            send,
        )
    finally:
        mts._SSE_SESSIONS.pop("enqueue-test", None)

    assert sent[0]["status"] == 202
    # 队列应有一个响应（initialize 的 ok 响应）
    queued = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert queued["id"] == 42
    assert "result" in queued
    assert queued["result"]["serverInfo"]["name"] == mts.SERVER_NAME


# ============================================================================
# sse_asgi_app: GET /sse
# ============================================================================


@pytest.mark.asyncio
async def test_get_sse_streams_endpoint_event_then_terminates_on_none():
    """GET /sse 先发 endpoint 事件；向队列推 None 终止循环。

    保持简单：只验证响应头与 endpoint chunk 形态，不验证 keepalive 行为
    （keepalive 由 10s 超时控制，是行为细节而非契约）。
    """
    sent: list[dict] = []

    async def receive():
        # _handle_sse 不调用 receive，这里只为 ASGI 协议完整
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        body = message.get("body", b"")
        # 收到 endpoint chunk 后向队列推 None 终止 _handle_sse 循环
        if body.startswith(b"event: endpoint"):
            for sid, q in list(mts._SSE_SESSIONS.items()):
                q.put_nowait(None)
                break

    await mts.sse_asgi_app(
        {"type": "http", "path": "/sse", "method": "GET"}, receive, send
    )

    # 首条应是 http.response.start，其次 endpoint 事件
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"] == b"text/event-stream"
    # mcp-session-id 应在 headers 中（hex 16 位）
    session_ids = [v for k, v in sent[0]["headers"] if k == b"mcp-session-id"]
    assert len(session_ids[0]) == 16

    endpoint_chunk = sent[1]["body"]
    assert b"event: endpoint" in endpoint_chunk
    assert b"/messages?session_id=" in endpoint_chunk
    assert sent[1]["more_body"] is True


# ============================================================================
# main: CLI 解析
# ============================================================================


def test_main_invokes_stdio_when_no_sse_flag(monkeypatch):
    called: list[str] = []

    def fake_run_stdio():
        called.append("stdio")

    monkeypatch.setattr(mts, "run_stdio", fake_run_stdio)
    monkeypatch.setattr("sys.argv", ["mcp_test_server.py"])

    mts.main()

    assert called == ["stdio"]


def test_main_invokes_uvicorn_when_sse_flag(monkeypatch):
    """--sse 模式应通过 uvicorn 启动 ASGI app。"""
    captured: dict = {}

    def fake_uvicorn_run(app, host, port, log_level):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["log_level"] = log_level

    # uvicorn 是在函数内部 import 的，需要往 sys.modules 注入 mock
    import sys
    import types

    fake_uvicorn = types.SimpleNamespace(run=fake_uvicorn_run)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr("sys.argv", ["mcp_test_server.py", "--sse", "--host", "0.0.0.0", "--port", "9999"])

    mts.main()

    assert captured["app"] is mts.sse_asgi_app
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9999
    assert captured["log_level"] == "warning"
