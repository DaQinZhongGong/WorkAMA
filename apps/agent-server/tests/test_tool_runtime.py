"""为 tool_runtime.py（工具运行时）补充的单元测试。

测试覆盖：
- TOOL_DEFINITIONS 结构与 TOOLS 索引一致性
- parse_tool_command 的错误路径（空参数、非对象参数、无效 JSON）
- ToolRuntime.workspace 标识符校验与目录创建
- ToolRuntime.execute 各工具的成功/失败/超时路径
- _validate_code 安全约束（imports、global、dunder、禁用内置）
- _execute_remote 通过 sandbox fleet 的远程执行流程
- web_search 查询校验与 limit 钳制

所有外部依赖（httpx）使用简单 fake 类替换，不调用真实服务。
"""
from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from pathlib import Path

import pytest

from workama_agent.tool_runtime import (
    TOOL_DEFINITIONS,
    TOOLS,
    ToolError,
    ToolRuntime,
    parse_tool_command,
)


# ---------------------------------------------------------------------------
# Fake 类：模拟 httpx.AsyncClient 响应
# ---------------------------------------------------------------------------


class FakeHttpResponse:
    """模拟 httpx 响应对象。"""

    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 300:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeHttpClient:
    """模拟 httpx.AsyncClient，按调用顺序返回预设响应。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return self._responses.pop(0) if self._responses else FakeHttpResponse({})

    async def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return self._responses.pop(0) if self._responses else FakeHttpResponse({})

    async def put(self, url, **kwargs):
        self.requests.append(("PUT", url, kwargs))
        return self._responses.pop(0) if self._responses else FakeHttpResponse({})


def _patch_httpx(monkeypatch, responses):
    """将 httpx.AsyncClient 替换为返回预设响应的 FakeHttpClient。"""
    client = FakeHttpClient(responses)
    monkeypatch.setattr(
        "workama_agent.tool_runtime.httpx.AsyncClient",
        lambda **_kwargs: client,
    )
    return client


class FakeWebSocket:
    """模拟 websockets 客户端连接，按顺序吐出预设消息并记录发送内容。"""

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


def _patch_websockets_connect(monkeypatch, fake_ws):
    """将 websockets.connect 替换为返回 fake_ws 的可调用对象。"""
    monkeypatch.setattr(
        "workama_agent.tool_runtime.websockets.connect",
        lambda _url, **_kwargs: fake_ws,
    )


# ---------------------------------------------------------------------------
# TOOL_DEFINITIONS 与 TOOLS 索引测试
# ---------------------------------------------------------------------------


def test_tool_definitions_have_consistent_structure():
    """每个工具定义都应包含 name、version、description、risk、sandbox、input_schema 字段。"""
    for definition in TOOL_DEFINITIONS:
        assert isinstance(definition["name"], str) and definition["name"]
        assert isinstance(definition["version"], str) and definition["version"]
        assert isinstance(definition["description"], str)
        assert definition["risk"] in {"A1", "A2", "A3", "A4"}
        assert isinstance(definition["sandbox"], bool)
        assert definition["input_schema"]["type"] == "object"
        assert "properties" in definition["input_schema"]


def test_tools_dict_contains_all_definitions_by_name():
    """TOOLS 字典的键应与 TOOL_DEFINITIONS 中的 name 完全对应。"""
    names = [item["name"] for item in TOOL_DEFINITIONS]
    assert set(TOOLS.keys()) == set(names)
    for name in names:
        assert TOOLS[name]["name"] == name


# ---------------------------------------------------------------------------
# parse_tool_command 错误路径测试
# ---------------------------------------------------------------------------


def test_parse_tool_command_with_no_arguments():
    """/tool 后只有工具名时返回空参数字典。"""
    result = parse_tool_command("/tool file.read")
    assert result == ("file.read", {})


def test_parse_tool_command_rejects_non_object_arguments():
    """参数不是 JSON 对象（如数组）时抛出 ToolError。"""
    with pytest.raises(ToolError, match="must be a JSON object"):
        parse_tool_command('/tool file.read ["not", "an", "object"]')


def test_parse_tool_command_rejects_invalid_json():
    """参数 JSON 格式无效时抛出 JSONDecodeError。"""
    with pytest.raises(json.JSONDecodeError):
        parse_tool_command('/tool file.read {invalid json}')


# ---------------------------------------------------------------------------
# workspace 标识符校验测试
# ---------------------------------------------------------------------------


def test_workspace_rejects_short_identifiers(tmp_path):
    """workspace_id 或 session_id 少于 3 个字符时抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="Invalid workspace or session"):
        runtime.workspace("ab", "ses_test")


def test_workspace_rejects_invalid_characters(tmp_path):
    """workspace_id 包含非法字符（空格、斜杠等）时抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="Invalid workspace or session"):
        runtime.workspace("wsp test!", "ses_test")


def test_workspace_creates_nested_directories(tmp_path):
    """有效标识符时创建嵌套目录并返回正确路径。"""
    runtime = ToolRuntime(str(tmp_path))
    target = runtime.workspace("wsp_test", "ses_test")
    assert target == tmp_path / "wsp_test" / "ses_test"
    assert target.is_dir()


# ---------------------------------------------------------------------------
# execute 路由与本地工具错误路径测试
# ---------------------------------------------------------------------------


def test_execute_unknown_tool_raises_error(tmp_path):
    """execute 对未注册的工具名抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(runtime.execute("no_such_tool", {}, "wsp_test", "ses_test"))


def test_execute_terminal_without_fleet_raises_error(tmp_path):
    """无 fleet_url 时 terminal 工具抛出 ToolError，提示需要 sandbox fleet。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="managed sandbox fleet"):
        asyncio.run(runtime.execute("terminal", {"argv": ["ls"]}, "wsp_test", "ses_test"))


# ---------------------------------------------------------------------------
# file.read 错误路径测试
# ---------------------------------------------------------------------------


def test_file_read_rejects_missing_file(tmp_path):
    """file.read 对不存在的文件抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="missing or exceeds 256 KiB"):
        asyncio.run(runtime.execute("file.read", {"path": "absent.txt"}, "wsp_test", "ses_test"))


def test_file_read_rejects_oversized_file(tmp_path):
    """file.read 对超过 256 KiB 的文件抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    target = tmp_path / "wsp_test" / "ses_test" / "big.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x" * (262144 + 1))

    with pytest.raises(ToolError, match="missing or exceeds 256 KiB"):
        asyncio.run(runtime.execute("file.read", {"path": "big.txt"}, "wsp_test", "ses_test"))


# ---------------------------------------------------------------------------
# file.write 错误路径测试
# ---------------------------------------------------------------------------


def test_file_write_rejects_oversized_content(tmp_path):
    """file.write 对超过 256 KiB 的内容抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    large_content = "x" * (262144 + 1)
    with pytest.raises(ToolError, match="exceeds 256 KiB"):
        asyncio.run(
            runtime.execute("file.write", {"path": "big.txt", "content": large_content}, "wsp_test", "ses_test")
        )


# ---------------------------------------------------------------------------
# file.search 测试
# ---------------------------------------------------------------------------


def test_file_search_rejects_empty_query(tmp_path):
    """file.search 对空查询字符串抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="1-300 characters"):
        asyncio.run(runtime.execute("file.search", {"query": ""}, "wsp_test", "ses_test"))


def test_file_search_rejects_oversized_query(tmp_path):
    """file.search 对超过 300 字符的查询抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="1-300 characters"):
        asyncio.run(runtime.execute("file.search", {"query": "x" * 301}, "wsp_test", "ses_test"))


def test_file_search_returns_matching_lines(tmp_path):
    """file.search 返回匹配行的路径、行号和预览。"""
    runtime = ToolRuntime(str(tmp_path))
    asyncio.run(
        runtime.execute(
            "file.write",
            {"path": "doc.md", "content": "hello world\nfoo bar\nhello again"},
            "wsp_test",
            "ses_test",
        )
    )

    result = asyncio.run(runtime.execute("file.search", {"query": "hello"}, "wsp_test", "ses_test"))
    assert result.status == "succeeded"
    assert len(result.output) == 2
    assert result.output[0]["line"] == 1
    assert result.output[0]["preview"] == "hello world"
    assert result.output[1]["line"] == 3


# ---------------------------------------------------------------------------
# code_interpreter 安全约束与执行测试
# ---------------------------------------------------------------------------


def test_code_interpreter_rejects_empty_code(tmp_path):
    """code_interpreter 对空代码抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="1-32768 characters"):
        asyncio.run(runtime.execute("code_interpreter", {"code": ""}, "wsp_test", "ses_test"))


def test_code_interpreter_rejects_oversized_code(tmp_path):
    """code_interpreter 对超过 32768 字符的代码抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="1-32768 characters"):
        asyncio.run(
            runtime.execute("code_interpreter", {"code": "x" * 32769}, "wsp_test", "ses_test")
        )


def test_code_interpreter_rejects_global_statement(tmp_path):
    """code_interpreter 拒绝 global 语句。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="global scope"):
        asyncio.run(
            runtime.execute("code_interpreter", {"code": "global x\nx = 1"}, "wsp_test", "ses_test")
        )


def test_code_interpreter_rejects_dunder_attribute_access(tmp_path):
    """code_interpreter 拒绝 dunder 属性访问。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="Dunder"):
        asyncio.run(
            runtime.execute("code_interpreter", {"code": "x = obj.__class__"}, "wsp_test", "ses_test")
        )


@pytest.mark.parametrize(
    "code",
    [
        "eval('1+1')",
        "exec('1')",
        "compile('1', '', 'eval')",
        "__import__('os')",
        "input('>>')",
        "breakpoint()",
        "open('file')",
    ],
)
def test_code_interpreter_rejects_denied_builtins(tmp_path, code):
    """code_interpreter 拒绝调用被禁用的内置函数。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="disabled"):
        asyncio.run(runtime.execute("code_interpreter", {"code": code}, "wsp_test", "ses_test"))


def test_code_interpreter_returns_failed_status_on_error(tmp_path):
    """代码执行出错时返回 failed 状态并包含错误输出。"""
    runtime = ToolRuntime(str(tmp_path))
    result = asyncio.run(
        runtime.execute("code_interpreter", {"code": "raise ValueError('boom')"}, "wsp_test", "ses_test")
    )
    assert result.status == "failed"
    assert "ValueError" in result.output["output"]
    assert result.output["exit_code"] != 0


def test_code_interpreter_raises_on_timeout(tmp_path, monkeypatch):
    """子进程超时（>10s）时抛出 ToolError。"""

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["python"], timeout=10)

    monkeypatch.setattr("workama_agent.tool_runtime.subprocess.run", fake_run)

    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="exceeded 10 seconds"):
        asyncio.run(
            runtime.execute("code_interpreter", {"code": "pass"}, "wsp_test", "ses_test")
        )


# ---------------------------------------------------------------------------
# web_search 查询校验与 limit 钳制测试
# ---------------------------------------------------------------------------


def test_web_search_rejects_empty_query(tmp_path):
    """web_search 对空查询抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="1-300 characters"):
        asyncio.run(runtime.execute("web_search", {"query": ""}, "wsp_test", "ses_test"))


def test_web_search_rejects_oversized_query(tmp_path):
    """web_search 对超过 300 字符的查询抛出 ToolError。"""
    runtime = ToolRuntime(str(tmp_path))
    with pytest.raises(ToolError, match="1-300 characters"):
        asyncio.run(
            runtime.execute("web_search", {"query": "x" * 301}, "wsp_test", "ses_test")
        )


@pytest.mark.parametrize("input_limit,expected", [(0, 1), (100, 10), (-5, 1), (5, 5)])
def test_web_search_clamps_limit_to_valid_range(tmp_path, monkeypatch, input_limit, expected):
    """web_search 的 limit 参数被钳制到 1-10 范围。"""
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"query": {"search": []}}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get(self, _url, **kwargs):
            captured["params"] = kwargs.get("params", {})
            return Response()

    monkeypatch.setattr("workama_agent.tool_runtime.httpx.AsyncClient", lambda **_: Client())

    asyncio.run(
        ToolRuntime(str(tmp_path)).execute(
            "web_search", {"query": "test", "limit": input_limit}, "wsp_test", "ses_test"
        )
    )
    assert captured["params"]["srlimit"] == expected


# ---------------------------------------------------------------------------
# _execute_remote 远程执行测试
# ---------------------------------------------------------------------------


def test_execute_remote_writes_file_via_sandbox(monkeypatch, tmp_path):
    """有 fleet_url 时 file.write 通过 sandbox 远程执行并返回 artifact。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    write_resp = FakeHttpResponse({"path": "test.txt", "size": 5})
    client = _patch_httpx(monkeypatch, [sandbox_resp, write_resp])

    result = asyncio.run(
        runtime.execute("file.write", {"path": "test.txt", "content": "hello"}, "wsp_test", "ses_test")
    )

    assert result.status == "succeeded"
    assert "test.txt" in result.summary
    assert result.artifact["name"] == "test.txt"
    assert result.artifact["content"] == "hello"
    # 第一次请求是获取 sandbox
    assert client.requests[0][0] == "POST"
    assert "/internal/sandboxes" in client.requests[0][1]
    # 第二次请求是 PUT 写入文件
    assert client.requests[1][0] == "PUT"


def test_execute_remote_reads_file_via_sandbox(monkeypatch, tmp_path):
    """有 fleet_url 时 file.read 通过 sandbox 远程读取文件内容。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    read_resp = FakeHttpResponse({"path": "notes.txt", "content": "remote content"})
    _patch_httpx(monkeypatch, [sandbox_resp, read_resp])

    result = asyncio.run(
        runtime.execute("file.read", {"path": "notes.txt"}, "wsp_test", "ses_test")
    )

    assert result.status == "succeeded"
    assert result.output == "remote content"


def test_execute_remote_runs_terminal_command(monkeypatch, tmp_path):
    """有 fleet_url 时 terminal 通过 sandbox 执行命令并返回退出码。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    exec_resp = FakeHttpResponse({"exit_code": 0, "output": "hello\n"})
    _patch_httpx(monkeypatch, [sandbox_resp, exec_resp])

    result = asyncio.run(
        runtime.execute("terminal", {"argv": ["echo", "hello"]}, "wsp_test", "ses_test")
    )

    assert result.status == "succeeded"
    assert result.output["exit_code"] == 0


def test_execute_remote_validates_terminal_argv(monkeypatch, tmp_path):
    """远程 terminal 对空 argv 列表抛出 ToolError（在获取 sandbox 之后校验）。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    _patch_httpx(monkeypatch, [sandbox_resp])

    with pytest.raises(ToolError, match="argv"):
        asyncio.run(runtime.execute("terminal", {"argv": []}, "wsp_test", "ses_test"))


def test_execute_remote_runs_code_interpreter(monkeypatch, tmp_path):
    """有 fleet_url 时 code_interpreter 通过 sandbox 远程执行 Python 代码。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    exec_resp = FakeHttpResponse({"exit_code": 0, "output": "30\n"})
    _patch_httpx(monkeypatch, [sandbox_resp, exec_resp])

    result = asyncio.run(
        runtime.execute(
            "code_interpreter", {"code": "print(sum(i*i for i in range(5)))"}, "wsp_test", "ses_test"
        )
    )

    assert result.status == "succeeded"
    assert result.output["exit_code"] == 0


def test_execute_remote_file_search(monkeypatch, tmp_path):
    """有 fleet_url 时 file.search 通过 sandbox 远程执行搜索脚本。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    exec_resp = FakeHttpResponse({"exit_code": 0, "output": json.dumps([{"path": "a.txt", "line": 1, "preview": "match"}])})
    _patch_httpx(monkeypatch, [sandbox_resp, exec_resp])

    result = asyncio.run(
        runtime.execute("file.search", {"query": "match"}, "wsp_test", "ses_test")
    )

    assert result.status == "succeeded"
    assert len(result.output) == 1
    assert result.output[0]["path"] == "a.txt"


# ---------------------------------------------------------------------------
# terminal 流式模式测试
# ---------------------------------------------------------------------------


def test_execute_remote_terminal_stream_false_uses_sync_exec(monkeypatch, tmp_path):
    """stream 显式为 false 时走同步 exec 接口，不触发 WebSocket 连接。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    exec_resp = FakeHttpResponse({"exit_code": 0, "output": "hi\n"})
    client = _patch_httpx(monkeypatch, [sandbox_resp, exec_resp])

    # 若误连 WebSocket 会让此断言失败
    def _fail_connect(*_args, **_kwargs):
        raise AssertionError("WebSocket 不应在同步模式下被调用")

    monkeypatch.setattr("workama_agent.tool_runtime.websockets.connect", _fail_connect)

    result = asyncio.run(
        runtime.execute("terminal", {"argv": ["echo", "hi"], "stream": False}, "wsp_test", "ses_test")
    )

    assert result.status == "succeeded"
    assert result.output["exit_code"] == 0
    # 确认走的是同步 exec HTTP 接口而非 WebSocket
    assert any("/exec" in req[1] for req in client.requests)


def test_execute_remote_terminal_stream_true_emits_output_events(monkeypatch, tmp_path):
    """stream=true 时连接 WebSocket，逐块解码 base64 并通过 event_callback 发出 terminal.output 事件。"""
    encoded_hello = base64.b64encode(b"hello\n").decode()
    encoded_world = base64.b64encode(b"world\n").decode()
    messages = [
        json.dumps({"type": "output", "data": encoded_hello}),
        json.dumps({"type": "output", "data": encoded_world}),
        json.dumps({"type": "exit", "exit_code": 0}),
    ]
    fake_ws = FakeWebSocket(messages)
    _patch_websockets_connect(monkeypatch, fake_ws)

    events = []

    async def callback(event_type, event_dict):
        events.append((event_type, event_dict))

    runtime = ToolRuntime(
        str(tmp_path),
        fleet_url="http://fleet",
        internal_token="tok",
        event_callback=callback,
    )
    # sandbox 获取仍走 httpx
    _patch_httpx(monkeypatch, [FakeHttpResponse({"id": "sbx_1"})])

    result = asyncio.run(
        runtime.execute("terminal", {"argv": ["echo", "hello"], "stream": True}, "wsp_test", "ses_test")
    )

    # 验证发送了 start chunk
    assert fake_ws.sent
    start = json.loads(fake_ws.sent[0])
    assert start["type"] == "start"
    assert start["argv"] == ["echo", "hello"]
    assert start["rows"] == 24 and start["cols"] == 80
    # 验证发出了两个 output 事件，data 已从 base64 解码
    assert len(events) == 2
    assert events[0] == ("terminal.output", {"type": "terminal.output", "data": "hello\n"})
    assert events[1] == ("terminal.output", {"type": "terminal.output", "data": "world\n"})
    # 验证返回的 ToolResult 状态与聚合输出
    assert result.status == "succeeded"
    assert result.output["exit_code"] == 0
    assert "hello" in result.output["output"]
    assert "world" in result.output["output"]


def test_execute_remote_terminal_stream_true_returns_failed_on_nonzero_exit(monkeypatch, tmp_path):
    """stream=true 收到非零 exit 块时返回 failed 状态的 ToolResult。"""
    encoded_err = base64.b64encode(b"boom\n").decode()
    messages = [
        json.dumps({"type": "output", "data": encoded_err}),
        json.dumps({"type": "exit", "exit_code": 2}),
    ]
    fake_ws = FakeWebSocket(messages)
    _patch_websockets_connect(monkeypatch, fake_ws)

    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    _patch_httpx(monkeypatch, [FakeHttpResponse({"id": "sbx_1"})])

    result = asyncio.run(
        runtime.execute("terminal", {"argv": ["false"], "stream": True}, "wsp_test", "ses_test")
    )

    assert result.status == "failed"
    assert result.output["exit_code"] == 2
    assert "boom" in result.output["output"]
    assert result.summary == "Command exited with code 2"


# ---------------------------------------------------------------------------
# browser 工具测试
# ---------------------------------------------------------------------------


def test_execute_remote_browser_navigate_succeeds(monkeypatch, tmp_path):
    """browser navigate 在 sandbox 返回 ok=true 时返回 succeeded 状态。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    browser_resp = FakeHttpResponse({"ok": True, "url": "https://example.com", "title": "Example"})
    client = _patch_httpx(monkeypatch, [sandbox_resp, browser_resp])

    result = asyncio.run(
        runtime.execute(
            "browser", {"action": "navigate", "target": "https://example.com"}, "wsp_test", "ses_test"
        )
    )

    assert result.status == "succeeded"
    assert "navigate" in result.summary
    assert result.output["ok"] is True
    assert result.artifact is None
    # 第二次请求应发往 browser 端点
    assert client.requests[1][0] == "POST"
    assert "/browser" in client.requests[1][1]
    # 校验转发给 fleet 的 payload 结构
    sent_payload = client.requests[1][2]["json"]
    assert sent_payload["action"] == "navigate"
    assert sent_payload["target"] == "https://example.com"
    assert sent_payload["timeout_ms"] == 10000


def test_execute_remote_browser_screenshot_returns_artifact(monkeypatch, tmp_path):
    """browser screenshot 在响应包含 screenshot 字段时返回 image/png artifact。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    screenshot_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    browser_resp = FakeHttpResponse({"ok": True, "screenshot": screenshot_b64})
    _patch_httpx(monkeypatch, [sandbox_resp, browser_resp])

    result = asyncio.run(
        runtime.execute("browser", {"action": "screenshot"}, "wsp_test", "ses_test")
    )

    assert result.status == "succeeded"
    assert "screenshot" in result.summary
    # 截图作为 image/png artifact 返回，base64 数据透传
    assert result.artifact is not None
    assert result.artifact["type"] == "image/png"
    assert result.artifact["encoding"] == "base64"
    assert result.artifact["data"] == screenshot_b64


def test_execute_remote_browser_navigate_fails(monkeypatch, tmp_path):
    """browser navigate 在 sandbox 返回 ok=false 时返回 failed 状态并携带 error。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    browser_resp = FakeHttpResponse({"ok": False, "error": "navigation timeout"})
    _patch_httpx(monkeypatch, [sandbox_resp, browser_resp])

    result = asyncio.run(
        runtime.execute(
            "browser", {"action": "navigate", "target": "https://slow.example.com"}, "wsp_test", "ses_test"
        )
    )

    assert result.status == "failed"
    assert "navigate" in result.summary
    assert "navigation timeout" in result.summary
    assert result.output["ok"] is False
    assert result.artifact is None


def test_execute_remote_browser_passes_text_params(monkeypatch, tmp_path):
    """browser input 动作时将 text 参数放入 params 字段转发给 fleet。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    browser_resp = FakeHttpResponse({"ok": True})
    client = _patch_httpx(monkeypatch, [sandbox_resp, browser_resp])

    result = asyncio.run(
        runtime.execute(
            "browser",
            {"action": "input", "target": "#search", "text": "hello world", "timeout_ms": 5000},
            "wsp_test",
            "ses_test",
        )
    )

    assert result.status == "succeeded"
    sent_payload = client.requests[1][2]["json"]
    assert sent_payload["action"] == "input"
    assert sent_payload["target"] == "#search"
    assert sent_payload["params"] == {"text": "hello world"}
    assert sent_payload["timeout_ms"] == 5000


# ---------------------------------------------------------------------------
# 沙箱镜像 image 路由测试
# ---------------------------------------------------------------------------


def test_browser_tool_uses_sandbox_browser_image(monkeypatch, tmp_path):
    """browser 工具应申请 sandbox-browser 镜像（含 Chromium + CDP 桥）。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    browser_resp = FakeHttpResponse({"ok": True})
    client = _patch_httpx(monkeypatch, [sandbox_resp, browser_resp])

    result = asyncio.run(
        runtime.execute("browser", {"action": "screenshot"}, "wsp_test", "ses_test")
    )

    assert result.status == "succeeded"
    # 第一次请求是申请沙箱，校验 POST body 中的 image 字段
    assert client.requests[0][0] == "POST"
    assert "/internal/sandboxes" in client.requests[0][1]
    acquire_body = client.requests[0][2]["json"]
    assert acquire_body["image"] == "sandbox-browser"
    # workspace_id 与 session_id 也应正确透传
    assert acquire_body["workspace_id"] == "wsp_test"
    assert acquire_body["session_id"] == "ses_test"


def test_file_read_uses_sandbox_code_image(monkeypatch, tmp_path):
    """file.read 工具应申请 sandbox-code 镜像（多语言工具链，不含 Chromium）。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    read_resp = FakeHttpResponse({"path": "notes.txt", "content": "remote content"})
    client = _patch_httpx(monkeypatch, [sandbox_resp, read_resp])

    result = asyncio.run(
        runtime.execute("file.read", {"path": "notes.txt"}, "wsp_test", "ses_test")
    )

    assert result.status == "succeeded"
    assert result.output == "remote content"
    # 申请沙箱的 POST body 中 image 应为 sandbox-code
    assert client.requests[0][0] == "POST"
    assert "/internal/sandboxes" in client.requests[0][1]
    acquire_body = client.requests[0][2]["json"]
    assert acquire_body["image"] == "sandbox-code"


def test_terminal_uses_sandbox_code_image(monkeypatch, tmp_path):
    """terminal 工具应申请 sandbox-code 镜像（执行 shell 命令，不需要 Chromium）。"""
    runtime = ToolRuntime(str(tmp_path), fleet_url="http://fleet", internal_token="tok")
    sandbox_resp = FakeHttpResponse({"id": "sbx_1"})
    exec_resp = FakeHttpResponse({"exit_code": 0, "output": "hello\n"})
    client = _patch_httpx(monkeypatch, [sandbox_resp, exec_resp])

    result = asyncio.run(
        runtime.execute("terminal", {"argv": ["echo", "hello"]}, "wsp_test", "ses_test")
    )

    assert result.status == "succeeded"
    assert result.output["exit_code"] == 0
    # 申请沙箱的 POST body 中 image 应为 sandbox-code
    assert client.requests[0][0] == "POST"
    assert "/internal/sandboxes" in client.requests[0][1]
    acquire_body = client.requests[0][2]["json"]
    assert acquire_body["image"] == "sandbox-code"

