from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shlex
import sys

import pytest
import pytest_asyncio

from workama_platform.modules import mcp, mcp_protocol


def _server(
    *,
    transport: str = "streamable_http",
    target: str = "https://example.com/mcp",
    **overrides,
) -> dict:
    server = {
        "id": "mcp_test",
        "name": "Test MCP",
        "transport": transport,
        "endpoint_or_command": target,
        "auth_type": "none",
        "auth_ref": "managed-ref-should-not-leak",
        "protocol_version": "2025-06-18",
    }
    server.update(overrides)
    return server


def _request(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


@pytest.mark.asyncio
async def test_mock_initialize_lists_capabilities_and_calls_tools():
    server = _server(target="mock://deterministic")

    initialized = await mcp_protocol.bridge_jsonrpc(
        server,
        _request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        ),
    )
    tools = await mcp_protocol.bridge_jsonrpc(server, _request("tools/list", request_id=2))
    resources = await mcp_protocol.bridge_jsonrpc(server, _request("resources/list", request_id=3))
    prompts = await mcp_protocol.bridge_jsonrpc(server, _request("prompts/list", request_id=4))
    called = await mcp_protocol.bridge_jsonrpc(
        server,
        _request("tools/call", {"name": "echo", "arguments": {"text": "hello"}}, request_id=5),
    )

    assert initialized["result"]["protocolVersion"] == "2025-06-18"
    assert {"tools", "resources", "prompts"} <= set(initialized["result"]["capabilities"])
    assert [item["name"] for item in tools["result"]["tools"]] == ["echo", "sum"]
    assert resources["result"]["resources"][0]["name"] == "README"
    assert prompts["result"]["prompts"][0]["name"] == "greeting"
    assert called["result"]["content"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_jsonrpc_validation_errors_are_structured():
    invalid = await mcp_protocol.bridge_jsonrpc(_server(target="mock://default"), [])
    unknown = await mcp_protocol.bridge_jsonrpc(
        _server(target="mock://default"),
        _request("unknown/method"),
    )
    bad_params = await mcp_protocol.bridge_jsonrpc(
        _server(target="mock://default"),
        _request("tools/call", {"name": "echo", "arguments": []}, request_id=7),
    )
    bad_initialize = await mcp_protocol.bridge_jsonrpc(
        _server(target="mock://default"),
        _request("initialize", {"protocolVersion": "unsupported"}, request_id=8),
    )
    invalid_id_message = _request("tools/list")
    invalid_id_message["id"] = {"secret": "must-not-echo"}
    invalid_id = await mcp_protocol.bridge_jsonrpc(_server(target="mock://default"), invalid_id_message)

    assert invalid == {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    assert unknown["error"]["code"] == -32601
    assert bad_params["error"]["code"] == -32602
    assert bad_initialize["error"]["code"] == -32602
    assert invalid_id == {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}


@dataclass
class FakeResponse:
    payload: dict
    status_code: int = 200
    headers: dict[str, str] | None = None
    text: str = ""
    is_redirect: bool = False

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[tuple[str, dict, dict]] = []

    async def post(self, url: str, *, json: dict, headers: dict):
        self.calls.append((url, json, headers))
        return self.response


@pytest.mark.asyncio
async def test_streamable_http_forwards_jsonrpc_without_auth_reference(monkeypatch):
    response = FakeResponse(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "result": {
                "serverInfo": {"name": "remote", "auth_ref": "server-secret"},
                "credential": "provider-secret",
                "tools": [{"name": "safe"}],
            },
        }
    )
    client = FakeHttpClient(response)
    monkeypatch.setattr(mcp_protocol, "validate_endpoint_url", lambda value, resolve=False: value)

    result = await mcp_protocol.bridge_jsonrpc(
        _server(),
        _request("initialize", {"protocolVersion": "2025-06-18"}, request_id=11),
        http_client=client,
    )

    assert result["result"]["serverInfo"] == {"name": "remote"}
    assert "credential" not in result["result"]
    assert client.calls[0][0] == "https://example.com/mcp"
    assert "auth_ref" not in client.calls[0][2]
    assert client.calls[0][1]["method"] == "initialize"


@pytest.mark.asyncio
async def test_streamable_http_rejects_ssrf_and_redirects(monkeypatch):
    ssrf = await mcp_protocol.bridge_jsonrpc(
        _server(target="http://127.0.0.1/mcp"),
        _request("tools/list"),
    )
    monkeypatch.setattr(mcp_protocol, "validate_endpoint_url", lambda value, resolve=False: value)
    redirect_client = FakeHttpClient(
        FakeResponse({"jsonrpc": "2.0", "id": 1, "result": {}}, status_code=302, is_redirect=True)
    )
    redirect = await mcp_protocol.bridge_jsonrpc(
        _server(),
        _request("tools/list"),
        http_client=redirect_client,
    )

    assert ssrf["error"]["code"] == -32002
    assert redirect["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_oauth_is_explicitly_pending_and_route_exists(monkeypatch):
    oauth_server = _server()
    oauth_server["auth_type"] = "oauth"
    monkeypatch.setattr(mcp_protocol, "validate_endpoint_url", lambda value, resolve=False: value)
    result = await mcp_protocol.bridge_jsonrpc(oauth_server, _request("tools/list"))

    assert result["error"]["code"] == -32003
    assert result["error"]["data"] == {"oauth_pending": True}
    assert all(route.path != "/api/v1/mcp-servers/{server_id}/rpc" or "POST" in route.methods for route in mcp.router.routes)


def test_mock_target_is_allowed_only_for_streamable_http():
    assert mcp.validate_transport_target("streamable_http", "mock://deterministic") == "mock://deterministic"
    with pytest.raises(ValueError):
        mcp.validate_transport_target("sse", "mock://deterministic")


@pytest_asyncio.fixture(autouse=True)
async def clean_mcp_sessions():
    await mcp_protocol.reset_session_registry_async()
    yield
    await mcp_protocol.reset_session_registry_async()


@pytest.mark.asyncio
async def test_stdio_and_sse_mock_bridges_share_capabilities_and_require_session():
    for transport in ("stdio", "sse"):
        server = _server(
            transport=transport,
            target=f"mock://{transport}/deterministic",
            workspace_id="ws-mcp",
            roots=["file:///workspaces/ws-mcp/project", "mock://roots/docs"],
        )
        initialized = await mcp_protocol.bridge_jsonrpc(
            server,
            _request(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"roots": {"listChanged": True}},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            ),
        )
        assert initialized["result"]["session_id"]
        session_id = initialized["result"]["session_id"]
        assert initialized["mcp-session-id"] == session_id

        missing = await mcp_protocol.bridge_jsonrpc(server, _request("tools/list", request_id=2))
        assert missing["error"]["code"] == mcp_protocol.MCP_SESSION_ERROR

        listed = await mcp_protocol.bridge_jsonrpc(
            server,
            _request("tools/list", {"session_id": session_id}, request_id=3),
        )
        roots = await mcp_protocol.bridge_jsonrpc(
            server,
            _request("roots/list", {"mcp-session-id": session_id}, request_id=4),
        )
        called = await mcp_protocol.bridge_jsonrpc(
            server,
            _request(
                "tools/call",
                {"session_id": session_id, "name": "echo", "arguments": {"text": transport}},
                request_id=5,
            ),
        )
        assert [item["name"] for item in listed["result"]["tools"]] == ["echo", "sum"]
        assert roots["result"]["roots"] == ["file:///workspaces/ws-mcp/project", "mock://roots/docs"]
        assert called["result"]["content"][0]["text"] == transport


@pytest.mark.asyncio
async def test_session_is_rejected_when_unknown_or_bound_to_another_server():
    server = _server(transport="stdio", target="mock://stdio/deterministic")
    initialized = await mcp_protocol.bridge_jsonrpc(
        server,
        _request("initialize", {"protocolVersion": "2025-06-18"}),
    )
    session_id = initialized["result"]["session_id"]

    unknown = await mcp_protocol.bridge_jsonrpc(
        server,
        _request("tools/list", {"session_id": "not-a-session"}, request_id=2),
    )
    other_server = _server(
        transport="stdio",
        target="mock://stdio/deterministic",
        id="mcp_other",
    )
    cross_server = await mcp_protocol.bridge_jsonrpc(
        other_server,
        _request("tools/list", {"session_id": session_id}, request_id=3),
    )

    assert unknown["error"]["code"] == mcp_protocol.MCP_SESSION_ERROR
    assert cross_server["error"]["code"] == mcp_protocol.MCP_SESSION_ERROR
    assert "mcp_test" not in str(cross_server)
    assert "endpoint_or_command" not in str(cross_server)


@pytest.mark.asyncio
async def test_stdio_execution_is_disabled_without_explicit_policy(monkeypatch):
    monkeypatch.delenv("WORKAMA_MCP_STDIO_ENABLED", raising=False)
    monkeypatch.delenv("WORKAMA_MCP_STDIO_ALLOWED_COMMANDS", raising=False)
    stdio = await mcp_protocol.bridge_jsonrpc(
        _server(transport="stdio", target="python -c print('deterministic')"),
        _request("initialize", {"protocolVersion": "2025-06-18"}),
    )

    assert stdio["error"]["code"] == mcp_protocol.MCP_PENDING_ERROR
    assert stdio["error"]["data"] == {
        "transport": "stdio",
        "execution": "disabled",
        "subprocess_started": False,
    }
    assert "python" not in str(stdio["error"])


@pytest.mark.asyncio
async def test_stdio_transport_reuses_a_policy_allowed_process(monkeypatch, tmp_path):
    script = tmp_path / "mcp_stdio.py"
    script.write_text(
        """
import json
import sys

for raw in sys.stdin:
    request = json.loads(raw)
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "stdio-test", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": request["params"]["arguments"]["text"]}], "isError": False}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKAMA_MCP_STDIO_ENABLED", "true")
    monkeypatch.setenv("WORKAMA_MCP_STDIO_ALLOWED_COMMANDS", os.path.basename(sys.executable))
    target = shlex.join([sys.executable, str(script)])
    server = _server(transport="stdio", target=target)

    initialized = await mcp_protocol.bridge_jsonrpc(
        server,
        _request("initialize", {"protocolVersion": "2025-06-18"}),
    )
    session_id = initialized["result"]["session_id"]
    listed = await mcp_protocol.bridge_jsonrpc(
        server,
        _request("tools/list", {"session_id": session_id}, request_id=2),
    )
    called = await mcp_protocol.bridge_jsonrpc(
        server,
        _request("tools/call", {"session_id": session_id, "name": "echo", "arguments": {"text": "stdio"}}, request_id=3),
    )

    assert initialized["result"]["serverInfo"]["name"] == "stdio-test"
    assert listed["result"]["tools"][0]["name"] == "echo"
    assert called["result"]["content"][0]["text"] == "stdio"


class FakeSseStream:
    status_code = 200
    headers = {"content-type": "text/event-stream"}
    is_redirect = False

    def __init__(self, lines):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class FakeSseClient:
    def __init__(self, streams):
        self.streams = iter(streams)
        self.posts = []

    def stream(self, method, url, *, headers):
        assert method == "GET"
        self.stream_headers = headers
        self.stream_url = url
        return next(self.streams)

    async def post(self, url, *, json, headers):
        self.posts.append((url, json, headers))
        return FakeResponse(None, status_code=202)

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_sse_transport_consumes_endpoint_and_response_events(monkeypatch):
    monkeypatch.setattr(mcp_protocol, "validate_endpoint_url", lambda value, resolve=False: value)
    client = FakeSseClient(
        [
            FakeSseStream(
                [
                    "event: endpoint",
                    "data: /messages",
                    "",
                    'event: message',
                    'data: {"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"sse-test"}}}',
                    "",
                ]
            ),
            FakeSseStream(
                [
                    'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"echo"}]}}',
                    "",
                ]
            ),
        ]
    )
    server = _server(transport="sse", target="https://example.com/events")
    initialized = await mcp_protocol.bridge_jsonrpc(
        server,
        _request("initialize", {"protocolVersion": "2025-06-18"}, request_id=1),
        http_client=client,
    )
    session_id = initialized["result"]["session_id"]
    listed = await mcp_protocol.bridge_jsonrpc(
        server,
        _request("tools/list", {"session_id": session_id}, request_id=2),
        http_client=client,
    )

    assert initialized["result"]["serverInfo"] == {"name": "sse-test"}
    assert listed["result"]["tools"] == [{"name": "echo"}]
    assert client.posts[0][0] == "https://example.com/messages"
    assert client.posts[0][1]["method"] == "initialize"
    assert client.stream_headers["MCP-Session-Id"] == session_id


@pytest.mark.asyncio
async def test_sse_transport_rejects_private_endpoint_before_connecting(monkeypatch):
    private_sse = await mcp_protocol.bridge_jsonrpc(
        _server(transport="sse", target="http://127.0.0.1/events"),
        _request("initialize", {"protocolVersion": "2025-06-18"}),
    )

    assert private_sse["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_sse_transport_rejects_redirects_before_reading_events(monkeypatch):
    monkeypatch.setattr(mcp_protocol, "validate_endpoint_url", lambda value, resolve=False: value)
    redirected = FakeSseStream([])
    redirected.status_code = 302
    client = FakeSseClient([redirected])
    result = await mcp_protocol.bridge_jsonrpc(
        _server(transport="sse", target="https://example.com/events"),
        _request("initialize", {"protocolVersion": "2025-06-18"}),
        http_client=client,
    )
    assert result["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_roots_are_limited_to_workspace_file_and_mock_uris():
    server = _server(transport="stdio", target="mock://stdio/roots", workspace_id="ws-roots")
    invalid_server_root = dict(server, roots=["file:///workspaces/ws-roots/../outside"])
    invalid_config = await mcp_protocol.bridge_jsonrpc(
        invalid_server_root,
        _request("initialize", {"protocolVersion": "2025-06-18"}),
    )
    invalid_request_root = await mcp_protocol.bridge_jsonrpc(
        server,
        _request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "roots": ["https://example.com/private"],
            },
        ),
    )
    invalid_traversal = await mcp_protocol.bridge_jsonrpc(
        server,
        _request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "roots": ["file:///workspaces/ws-roots/%2e%2e/outside"],
            },
        ),
    )

    assert invalid_config["error"]["code"] == mcp_protocol.MCP_ROOTS_ERROR
    assert invalid_request_root["error"]["code"] == -32602
    assert invalid_traversal["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_cancel_and_progress_notifications_have_bounded_results():
    server = _server(transport="stdio", target="mock://stdio/notifications")
    initialized = await mcp_protocol.bridge_jsonrpc(
        server,
        _request("initialize", {"protocolVersion": "2025-06-18"}),
    )
    session_id = initialized["result"]["session_id"]
    initialized_notice = await mcp_protocol.bridge_jsonrpc(
        server,
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {"session_id": session_id}},
    )
    progress = await mcp_protocol.bridge_jsonrpc(
        server,
        {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {
                "session_id": session_id,
                "progressToken": "secret-progress-token",
                "progress": 2,
                "total": 5,
            },
        },
    )
    cancelled = await mcp_protocol.bridge_jsonrpc(
        server,
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"mcp-session-id": session_id, "requestId": 42, "reason": "user stopped"},
        },
    )
    after_cancel = await mcp_protocol.bridge_jsonrpc(
        server,
        _request("tools/list", {"session_id": session_id}, request_id=42),
    )
    invalid_progress = await mcp_protocol.bridge_jsonrpc(
        server,
        _request(
            "notifications/progress",
            {"session_id": session_id, "progressToken": "p", "progress": -1},
            request_id=8,
        ),
    )

    assert initialized_notice["result"]["kind"] == "initialized"
    assert progress["result"] == {
        "notification": True,
        "accepted": True,
        "kind": "progress",
        "has_total": True,
    }
    assert "secret-progress-token" not in str(progress)
    assert cancelled["result"]["kind"] == "cancelled"
    assert after_cancel["error"]["code"] == mcp_protocol.MCP_CANCELLED_ERROR
    assert invalid_progress["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_http_session_header_is_forwarded_but_remote_session_handle_is_removed(monkeypatch):
    client = FakeHttpClient(
        FakeResponse(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "result": {"serverInfo": {"name": "remote", "session_id": "remote-secret"}},
            }
        )
    )
    monkeypatch.setattr(mcp_protocol, "validate_endpoint_url", lambda value, resolve=False: value)
    server = _server()
    initialized = await mcp_protocol.bridge_jsonrpc(
        server,
        _request("initialize", {"protocolVersion": "2025-06-18"}, request_id=10),
        http_client=client,
    )
    session_id = initialized["result"]["session_id"]
    assert "session_id" not in initialized["result"]["serverInfo"]

    client.response = FakeResponse(
        {"jsonrpc": "2.0", "id": 11, "result": {"tools": []}},
    )
    message = _request("tools/list", request_id=11)
    message["mcp-session-id"] = session_id
    result = await mcp_protocol.bridge_jsonrpc(server, message, http_client=client)

    assert result["result"] == {"tools": []}
    assert client.calls[1][2]["MCP-Session-Id"] == session_id
    assert "session_id" not in client.calls[1][1]["params"]


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_type", ["oauth", "bearer"])
async def test_oauth_and_bearer_boundaries_apply_to_all_transports(auth_type):
    result = await mcp_protocol.bridge_jsonrpc(
        _server(
            transport="sse",
            target="mock://sse/auth",
            auth_type=auth_type,
            auth_ref="managed-secret-reference",
        ),
        _request("tools/list"),
    )

    assert result["error"]["code"] == -32003
    assert "managed-secret-reference" not in str(result)
    if auth_type == "oauth":
        assert result["error"]["data"] == {"oauth_pending": True}


@pytest.mark.asyncio
async def test_sampling_and_elicitation_are_explicitly_pending_without_external_calls():
    for method in ("sampling/createMessage", "elicitation/create"):
        result = await mcp_protocol.bridge_jsonrpc(
            _server(target="mock://deterministic"),
            _request(method, {"sensitive": "must-not-echo"}),
        )
        assert result["error"]["code"] == mcp_protocol.MCP_PENDING_ERROR
        assert result["error"]["data"] == {"execution": "pending", "external_request_sent": False}
        assert "must-not-echo" not in str(result)


def test_session_registry_ttl_is_explicitly_injectable():
    now = [100.0]
    registry = mcp_protocol.McpSessionRegistry(ttl_seconds=5, clock=lambda: now[0])
    session_id = registry.create(server_key="server-hash", transport="stdio", protocol_version="2025-06-18")
    assert registry.get(session_id) is not None
    now[0] = 106.0
    assert registry.get(session_id) is None
