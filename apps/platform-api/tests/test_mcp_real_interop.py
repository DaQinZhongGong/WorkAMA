"""Real external MCP server interoperability tests.

These tests spawn a real MCP server (the bundled ``mcp_test_server`` module) as
a subprocess and over SSE via ``uvicorn``, then drive the full JSON-RPC 2.0
handshake through :class:`workama_platform.modules.mcp_protocol.MCPClient`.

They complement the deterministic ``test_mcp_protocol.py`` /
``test_mcp_enhancements.py`` suites by exercising the wire protocol against a
live stdio subprocess and a live SSE server.
"""
from __future__ import annotations

import asyncio
import shlex
import socket
import subprocess
import sys

import httpx
import pytest
import pytest_asyncio

from workama_platform.modules import mcp_protocol
from workama_platform.modules.mcp_protocol import (
    MCPClient,
    McpClientError,
    McpServerError,
    McpTimeoutError,
)
import workama_platform.modules.mcp_test_server as mcp_test_server

SERVER_PATH = mcp_test_server.__file__


def _stdio_command() -> str:
    return shlex.join([sys.executable, SERVER_PATH])


def _make_stdio_client(scenario: str | None = None, **kwargs) -> MCPClient:
    env = {}
    if scenario:
        env["MCP_TEST_SCENARIO"] = scenario
    return MCPClient(
        transport="stdio",
        endpoint_or_command=_stdio_command(),
        env=env or None,
        **kwargs,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

async def _wait_for_tcp(host: str, port: int, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_exc: Exception | None = None
    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=0.5
            )
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass
            return
        except (OSError, asyncio.TimeoutError) as exc:
            last_exc = exc
            await asyncio.sleep(0.15)
    raise RuntimeError(f"MCP SSE server did not start on {host}:{port}: {last_exc}")


@pytest_asyncio.fixture
async def sse_server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, SERVER_PATH, "--sse", "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await _wait_for_tcp("127.0.0.1", port)
        yield f"http://127.0.0.1:{port}/sse"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

# --------------------------------------------------------------------------- #
# stdio subprocess interop
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stdio_client_full_handshake_and_echo():
    """initialize -> tools/list -> tools/call against a live subprocess."""
    async with _make_stdio_client(timeout=15.0) as client:
        info = await client.connect()
        assert info["serverInfo"]["name"] == "workama-mcp-test-server"
        assert info["protocolVersion"] == "2025-06-18"
        assert "tools" in info["capabilities"]

        tools = await client.list_tools()
        assert tools["tools"][0]["name"] == "echo"
        assert tools["tools"][0]["inputSchema"]["required"] == ["text"]

        result = await client.call_tool("echo", {"text": "hello-stdio"})
        assert result["isError"] is False
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "hello-stdio"


@pytest.mark.asyncio
async def test_stdio_client_reuses_one_process_across_calls():
    """A single subprocess must serve multiple sequential requests."""
    client = _make_stdio_client(timeout=15.0)
    await client.connect()
    try:
        proc = client._process
        assert proc is not None
        pid = proc.pid
        assert proc.returncode is None
        for i in range(4):
            result = await client.call_tool("echo", {"text": f"call-{i}"})
            assert result["content"][0]["text"] == f"call-{i}"
        # Same process object throughout.
        assert client._process is proc
        assert client._process.pid == pid
        assert client._process.returncode is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_stdio_client_context_manager_terminates_process():
    """Exiting the async context manager must terminate the subprocess."""
    async with _make_stdio_client(timeout=15.0) as client:
        await client.connect()
        proc = client._process
        assert proc is not None
        assert proc.returncode is None
    # After __aexit__ the process reference is cleared and the OS process is gone.
    assert client._process is None
    assert proc.returncode is not None

@pytest.mark.asyncio
async def test_stdio_client_server_crash_raises_and_cleans_up():
    """A crashing subprocess must surface a client error and be cleaned up."""
    client = _make_stdio_client(scenario="crash", timeout=8.0)
    with pytest.raises(McpClientError):
        await client.connect()
    proc = client._process
    await client.aclose()
    assert client._process is None
    # The crashed subprocess has exited.
    assert proc is not None
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_stdio_client_timeout_on_slow_server():
    """A server that sleeps past the client timeout must raise McpTimeoutError."""
    client = _make_stdio_client(scenario="timeout", timeout=1.0)
    try:
        with pytest.raises(McpTimeoutError):
            await client.connect()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_stdio_client_invalid_response_raises():
    """Malformed JSON from the server must raise a client error."""
    client = _make_stdio_client(scenario="invalid", timeout=8.0)
    try:
        with pytest.raises(McpClientError):
            await client.connect()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_stdio_client_unknown_tool_returns_server_error():
    """A JSON-RPC error response must be raised as McpServerError."""
    async with _make_stdio_client(timeout=15.0) as client:
        await client.connect()
        with pytest.raises(McpServerError) as exc_info:
            await client.call_tool("does-not-exist", {})
        assert exc_info.value.code == -32601
        assert "does-not-exist" in exc_info.value.message

# --------------------------------------------------------------------------- #
# SSE transport interop
# --------------------------------------------------------------------------- #
def _make_sse_client(url: str, **kwargs) -> MCPClient:
    return MCPClient(transport="sse", endpoint_or_command=url, **kwargs)


@pytest.mark.asyncio
async def test_sse_client_full_handshake_and_echo(sse_server):
    """initialize -> tools/list -> tools/call against a live SSE server."""
    async with _make_sse_client(sse_server, timeout=15.0) as client:
        info = await client.connect()
        assert info["serverInfo"]["name"] == "workama-mcp-test-server"
        assert info["protocolVersion"] == "2025-06-18"
        assert "tools" in info["capabilities"]

        tools = await client.list_tools()
        assert tools["tools"][0]["name"] == "echo"
        assert tools["tools"][0]["inputSchema"]["required"] == ["text"]

        result = await client.call_tool("echo", {"text": "hello-sse"})
        assert result["isError"] is False
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "hello-sse"

@pytest.mark.asyncio
async def test_sse_client_reuses_one_session_across_calls(sse_server):
    """A single SSE session must serve multiple sequential requests."""
    async with _make_sse_client(sse_server, timeout=15.0) as client:
        await client.connect()
        session_id = client._sse_session_id
        assert session_id is not None
        for i in range(4):
            result = await client.call_tool("echo", {"text": f"sse-{i}"})
            assert result["content"][0]["text"] == f"sse-{i}"
        assert client._sse_session_id == session_id


@pytest.mark.asyncio
async def test_sse_client_context_manager_closes_http(sse_server):
    """Exiting the async context manager must close the SSE stream and HTTP client."""
    async with _make_sse_client(sse_server, timeout=15.0) as client:
        await client.connect()
        assert client._http_client is not None
        assert client._sse_stream is not None
        assert client._sse_task is not None
    assert client._http_client is None
    assert client._sse_stream is None
    assert client._sse_task is None

@pytest.mark.asyncio
async def test_sse_client_unknown_tool_returns_server_error(sse_server):
    """A JSON-RPC error delivered over SSE must be raised as McpServerError."""
    async with _make_sse_client(sse_server, timeout=15.0) as client:
        await client.connect()
        with pytest.raises(McpServerError) as exc_info:
            await client.call_tool("does-not-exist", {})
        assert exc_info.value.code == -32601
        assert "does-not-exist" in exc_info.value.message


@pytest.mark.asyncio
async def test_sse_client_concurrent_requests(sse_server):
    """Multiple concurrent requests must be demultiplexed by id over one stream."""
    async with _make_sse_client(sse_server, timeout=15.0) as client:
        await client.connect()
        texts = [f"concurrent-{i}" for i in range(5)]
        results = await asyncio.gather(
            *(client.call_tool("echo", {"text": t}) for t in texts)
        )
        assert len(results) == len(texts)
        for expected, result in zip(texts, results):
            assert result["content"][0]["text"] == expected
            assert result["isError"] is False


@pytest.mark.asyncio
async def test_sse_client_connect_to_dead_endpoint_raises():
    """Connecting to an unreachable SSE endpoint must raise McpClientError."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}/sse"
    client = _make_sse_client(url, timeout=2.0)
    try:
        with pytest.raises(McpClientError):
            await client.connect()
    finally:
        await client.aclose()
    assert client._http_client is None