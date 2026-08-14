"""测试 WebSocket 终端流端点 /internal/sandboxes/{sandbox_id}/terminal/stream。

覆盖三个场景：
1. 非活跃沙箱以 4009 关闭码拒绝连接
2. 活跃沙箱成功建立连接并调用 docker exec_run 启动 agentd stream
3. 客户端 input chunk 被正确转发到 docker exec socket

使用原始 ASGI 接口（asyncio.Queue）而非 Starlette TestClient，
以避免 pytest-asyncio 事件循环与 TestClient portal 线程的冲突。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from unittest.mock import MagicMock

import pytest

from workama_sandbox import main


# ---------------------------------------------------------------------------
# 假对象：模拟 psycopg 连接池 / 连接 / 结果
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row


class _FakeConn:
    """支持配置 fetchone 返回值的假连接。"""

    def __init__(self, row=None):
        self._row = row

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._row)

    async def commit(self):
        pass


class _FakePool:
    """Stand-in for AsyncConnectionPool that yields a fake connection。"""

    def __init__(self, row=None):
        self._conn = _FakeConn(row)

    def connection(self):
        @contextlib.asynccontextmanager
        async def _cm():
            yield self._conn

        return _cm()


# ---------------------------------------------------------------------------
# ASGI WebSocket 测试辅助：直接通过 ASGI 接口与 FastAPI app 交互
# ---------------------------------------------------------------------------


class _WSSession:
    """直接通过 ASGI 接口与 FastAPI app 交互的 WebSocket 测试会话。"""

    def __init__(self, app, path: str):
        self._app = app
        self._scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
            "app": app,
            "session": {},
        }
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._outgoing: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def connect(self):
        """发送 websocket.connect 并等待第一条出站消息（accept 或 close）。"""
        async def _receive():
            return await self._incoming.get()

        async def _send(message):
            await self._outgoing.put(message)

        self._task = asyncio.create_task(self._app(self._scope, _receive, _send))
        await self._incoming.put({"type": "websocket.connect"})
        return await self._outgoing.get()

    async def send_text(self, text: str):
        """模拟客户端发送文本消息。"""
        await self._incoming.put({"type": "websocket.receive", "text": text})

    async def receive(self):
        """读取一条来自端点的出站消息。"""
        return await self._outgoing.get()

    async def disconnect(self, code: int = 1000):
        """模拟客户端断开连接并清理端点 task。"""
        await self._incoming.put({"type": "websocket.disconnect", "code": code})
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def _disable_nats(monkeypatch):
    """关闭 nats 以免触发副作用。"""
    monkeypatch.setattr(main, "nats_client", None)


# ---------------------------------------------------------------------------
# 测试 1：非活跃沙箱以 4009 关闭码拒绝
# ---------------------------------------------------------------------------


async def test_terminal_stream_rejects_non_active_sandbox(monkeypatch, _disable_nats):
    """非活跃沙箱应该以 4009 关闭码拒绝 WebSocket 连接。"""
    row = {"id": "sbx_test", "status": "sleeping", "container_id": "c1"}
    monkeypatch.setattr(main, "pool", _FakePool(row=row))

    session = _WSSession(main.app, "/internal/sandboxes/sbx_test/terminal/stream")
    msg = await session.connect()
    assert msg["type"] == "websocket.accept"

    # 端点应该立即关闭连接，code=4009
    msg = await asyncio.wait_for(session.receive(), timeout=2.0)
    assert msg["type"] == "websocket.close"
    assert msg["code"] == 4009

    await session.disconnect()


# ---------------------------------------------------------------------------
# 测试 2：活跃沙箱成功建立连接并调用 docker exec_run
# ---------------------------------------------------------------------------


async def test_terminal_stream_connects_and_invokes_docker_exec(monkeypatch, _disable_nats):
    """活跃沙箱应该成功建立 WebSocket 连接，并以正确参数调用 container.exec_run。"""
    row = {"id": "sbx_test", "status": "active", "container_id": "c1"}
    monkeypatch.setattr(main, "pool", _FakePool(row=row))

    # 用真实 socket pair 模拟 docker exec 的 raw socket
    server_sock, client_sock = socket.socketpair()
    try:
        fake_exec_result = MagicMock()
        fake_exec_result.output = server_sock
        fake_container = MagicMock()
        fake_container.exec_run = MagicMock(return_value=fake_exec_result)
        fake_docker = MagicMock()
        fake_docker.containers.get = MagicMock(return_value=fake_container)
        monkeypatch.setattr(main, "docker_client", fake_docker)

        session = _WSSession(main.app, "/internal/sandboxes/sbx_test/terminal/stream")
        msg = await session.connect()
        assert msg["type"] == "websocket.accept"

        # 向 server_sock 写入 exit chunk，让 sock_to_ws 转发后触发断开
        client_sock.sendall(b'{"type":"exit","exit_code":0}\n')
        # 接收端点转发的 exit chunk
        msg = await asyncio.wait_for(session.receive(), timeout=2.0)
        assert msg["type"] == "websocket.send"
        assert "exit" in msg["text"]

        await session.disconnect()

        # 验证 docker exec_run 被调用，参数正确
        fake_container.exec_run.assert_called_once()
        call_args = fake_container.exec_run.call_args
        assert call_args.args[0] == ["/usr/local/bin/sandbox-agentd", "stream"]
        assert call_args.kwargs.get("user") == "10001:10001"
        assert call_args.kwargs.get("stdin") is True
        assert call_args.kwargs.get("socket") is True
    finally:
        client_sock.close()
        server_sock.close()


# ---------------------------------------------------------------------------
# 测试 3：客户端 input chunk 被转发到 docker exec socket
# ---------------------------------------------------------------------------


async def test_terminal_stream_forwards_input_chunk_to_docker_socket(monkeypatch, _disable_nats):
    """WebSocket 收到的 input chunk 应被转发到 docker exec socket。"""
    row = {"id": "sbx_test", "status": "active", "container_id": "c1"}
    monkeypatch.setattr(main, "pool", _FakePool(row=row))

    server_sock, client_sock = socket.socketpair()
    try:
        fake_exec_result = MagicMock()
        fake_exec_result.output = server_sock
        fake_container = MagicMock()
        fake_container.exec_run = MagicMock(return_value=fake_exec_result)
        fake_docker = MagicMock()
        fake_docker.containers.get = MagicMock(return_value=fake_container)
        monkeypatch.setattr(main, "docker_client", fake_docker)

        session = _WSSession(main.app, "/internal/sandboxes/sbx_test/terminal/stream")
        msg = await session.connect()
        assert msg["type"] == "websocket.accept"

        # 发送一个 input chunk
        await session.send_text(json.dumps({"type": "input", "data": "aGVsbG8="}))
        # 向 server_sock 写入 exit chunk，触发端点关闭
        client_sock.sendall(b'{"type":"exit","exit_code":0}\n')
        # 接收端点转发的 exit chunk
        msg = await asyncio.wait_for(session.receive(), timeout=2.0)
        assert msg["type"] == "websocket.send"
        assert "exit" in msg["text"]

        await session.disconnect()

        # 读取 client_sock 收到的数据，验证 input chunk 被转发
        client_sock.settimeout(1.0)
        forwarded = b""
        try:
            while True:
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                forwarded += chunk
        except (socket.timeout, OSError):
            pass

        assert b'"type"' in forwarded and b"input" in forwarded
        assert b"aGVsbG8=" in forwarded
    finally:
        client_sock.close()
        server_sock.close()
