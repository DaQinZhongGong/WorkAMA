"""M7 IM 通道基础模块 (messaging) WebSocket 实时推送测试。

覆盖（16 个测试）：
- ConnectionManager：connect / disconnect(含幂等) / send_to_user(多连接+错误吞咽) /
  broadcast_to_conversation / is_online (7)
- WS 握手：无 token close 4401 / decode 失败 close 4401 / 有效 token 收到 connected (3)
- WS ping/pong (1)
- WS 发送消息 → INSERT + 广播回 sender (1)
- WS 非成员发送消息 → 拒绝且不触达 INSERT (1)
- WS typing 事件 → 广播给会话其他在线成员 (1)
- presence 端点返回在线状态 (1)
- REST POST /messages 触发广播（响应结构不变）(1)

WebSocket 端点测试用 starlette.testclient.TestClient（内存 in-process，无需 websockets 库）；
ConnectionManager 单元测试用 _FakeWS；REST 测试用 httpx.AsyncClient + ASGITransport（与
test_messaging.py 同风格）。所有测试使用 fake pool/connection，不依赖真实 DB/Redis。
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, HTTPException, WebSocketDisconnect
from starlette.testclient import TestClient

from workama_platform.core import Actor, get_actor
from workama_platform.modules import messaging as msg


# ============================================================================
# 测试辅助：fake pool / connection / result / websocket（复用 test_messaging.py 模式）
# ============================================================================


class _Result:
    """模拟 psycopg 查询结果。"""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _RecordingConnection:
    """记录 execute 调用并按序返回配置的结果。"""

    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
        self._idx = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None


class _Pool:
    """模拟连接池。"""

    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        return _Ctx()


class _FakeWS:
    """用于 ConnectionManager 单元测试的假 WebSocket。"""

    def __init__(self, *, send_raises: bool = False):
        self.sent: list[dict] = []
        self.accepted = False
        self.closed = False
        self.close_code: int | None = None
        self._send_raises = send_raises

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        if self._send_raises:
            raise RuntimeError("simulated send failure")
        self.sent.append(message)

    async def close(self, code: int = 1000):
        self.closed = True
        self.close_code = code


class _RecordingManager:
    """记录广播调用的假 ConnectionManager（WS 端点测试用）。"""

    def __init__(self):
        self.broadcasts: list[tuple[str, list[str], dict]] = []
        self.connected: list[str] = []
        self.disconnected: list[str] = []
        self.online: set[str] = set()

    async def connect(self, user_id, websocket):
        await websocket.accept()
        self.connected.append(user_id)
        self.online.add(user_id)

    async def disconnect(self, user_id, websocket):
        self.disconnected.append(user_id)
        self.online.discard(user_id)

    async def send_to_user(self, user_id, message):
        pass

    async def broadcast_to_conversation(self, conversation_id, member_ids, message):
        self.broadcasts.append((conversation_id, list(member_ids), message))

    def is_online(self, user_id):
        return user_id in self.online


def _actor(
    *,
    workspace_id="wsp_test",
    user_id="usr_test",
    role="member",
    email="user@workama.example.com",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email=email,
        display_name="User",
        onboarding_completed=True,
        capabilities=("messaging:*",),
    )


def _conv_row(**overrides) -> dict:
    base = {
        "id": "imc_1",
        "workspace_id": "wsp_test",
        "type": "direct",
        "title": None,
        "created_by": "usr_test",
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _msg_row(**overrides) -> dict:
    base = {
        "id": "icm_1",
        "conversation_id": "imc_1",
        "sender_id": "usr_test",
        "content": "hello",
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(msg.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


async def _decode_ok(token, expected_type="access"):
    """假 decode_token_cached：返回固定有效 payload。"""
    return {"sub": "usr_test", "ws": "wsp_test", "type": "access"}


async def _decode_fail(token, expected_type="access"):
    """假 decode_token_cached：模拟无效 token。"""
    raise HTTPException(status_code=401, detail="Invalid or expired token")


# ============================================================================
# 1. ConnectionManager 单元测试
# ============================================================================


class TestConnectionManager:
    """ConnectionManager：connect / disconnect / send_to_user / broadcast / is_online。"""

    async def test_connect_adds_connection_and_accepts(self):
        """connect 将连接加入 active_connections 并调用 accept。"""
        mgr = msg.ConnectionManager()
        ws = _FakeWS()
        await mgr.connect("usr_a", ws)
        assert ws.accepted is True
        assert "usr_a" in mgr.active_connections
        assert ws in mgr.active_connections["usr_a"]

    async def test_disconnect_removes_connection_and_cleans_key(self):
        """disconnect 移除连接；用户无剩余连接时清理 key。"""
        mgr = msg.ConnectionManager()
        ws = _FakeWS()
        await mgr.connect("usr_a", ws)
        await mgr.disconnect("usr_a", ws)
        assert "usr_a" not in mgr.active_connections

    async def test_disconnect_is_idempotent_for_unknown_user(self):
        """disconnect 对未连接用户/连接幂等，不抛异常。"""
        mgr = msg.ConnectionManager()
        ws = _FakeWS()
        # 未连接直接 disconnect，不应抛异常
        await mgr.disconnect("usr_a", ws)
        assert "usr_a" not in mgr.active_connections

    async def test_send_to_user_delivers_to_all_connections(self):
        """一个用户的多个连接均收到消息。"""
        mgr = msg.ConnectionManager()
        ws1 = _FakeWS()
        ws2 = _FakeWS()
        await mgr.connect("usr_a", ws1)
        await mgr.connect("usr_a", ws2)
        await mgr.send_to_user("usr_a", {"type": "ping"})
        assert ws1.sent == [{"type": "ping"}]
        assert ws2.sent == [{"type": "ping"}]

    async def test_send_to_user_swallows_send_error(self):
        """某个连接 send_json 失败不影响其他连接收到消息。"""
        mgr = msg.ConnectionManager()
        ws_broken = _FakeWS(send_raises=True)
        ws_ok = _FakeWS()
        await mgr.connect("usr_a", ws_broken)
        await mgr.connect("usr_a", ws_ok)
        # 不应抛异常
        await mgr.send_to_user("usr_a", {"type": "ping"})
        assert ws_ok.sent == [{"type": "ping"}]

    async def test_broadcast_to_conversation_delivers_to_all_members(self):
        """broadcast_to_conversation 向所有在线成员投递消息。"""
        mgr = msg.ConnectionManager()
        ws_a = _FakeWS()
        ws_b = _FakeWS()
        await mgr.connect("usr_a", ws_a)
        await mgr.connect("usr_b", ws_b)
        await mgr.broadcast_to_conversation(
            "imc_1", ["usr_a", "usr_b", "usr_offline"], {"type": "message"}
        )
        # usr_a / usr_b 在线收到；usr_offline 离线自动跳过
        assert ws_a.sent == [{"type": "message"}]
        assert ws_b.sent == [{"type": "message"}]

    async def test_is_online_reflects_connection_state(self):
        """is_online：connect 后 True，disconnect 后 False。"""
        mgr = msg.ConnectionManager()
        assert mgr.is_online("usr_a") is False
        ws = _FakeWS()
        await mgr.connect("usr_a", ws)
        assert mgr.is_online("usr_a") is True
        await mgr.disconnect("usr_a", ws)
        assert mgr.is_online("usr_a") is False


# ============================================================================
# 2. WebSocket 端点测试（同步 TestClient）
# ============================================================================


class TestWsHandshake:
    """WS 握手：无 token / decode 失败 / 有效 token。"""

    def test_ws_handshake_no_token_closes_4401(self, monkeypatch):
        """无 token query 参数时服务端 close(4401)。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        monkeypatch.setattr(msg, "manager", msg.ConnectionManager())
        monkeypatch.setattr(msg, "decode_token_cached", _decode_ok)

        client = TestClient(_app())
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/v1/messaging/ws") as websocket:
                websocket.receive_json()
        assert exc.value.code == 4401

    def test_ws_handshake_invalid_token_closes_4401(self, monkeypatch):
        """decode_token_cached 抛异常时服务端 close(4401)。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        monkeypatch.setattr(msg, "manager", msg.ConnectionManager())
        monkeypatch.setattr(msg, "decode_token_cached", _decode_fail)

        client = TestClient(_app())
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/api/v1/messaging/ws?token=invalid"
            ) as websocket:
                websocket.receive_json()
        assert exc.value.code == 4401

    def test_ws_handshake_valid_token_receives_connected(self, monkeypatch):
        """有效 token 握手成功后收到 {"type":"connected"} 欢迎消息。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        monkeypatch.setattr(msg, "manager", msg.ConnectionManager())
        monkeypatch.setattr(msg, "decode_token_cached", _decode_ok)

        client = TestClient(_app())
        with client.websocket_connect("/api/v1/messaging/ws?token=valid") as websocket:
            data = websocket.receive_json()
        assert data["type"] == "connected"
        assert data["user_id"] == "usr_test"


class TestWsMessageHandling:
    """WS 消息处理：ping/pong / 发送消息 / 非成员拒绝 / typing。"""

    def test_ws_ping_pong(self, monkeypatch):
        """{"type":"ping"} → {"type":"pong"}。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        monkeypatch.setattr(msg, "manager", msg.ConnectionManager())
        monkeypatch.setattr(msg, "decode_token_cached", _decode_ok)

        client = TestClient(_app())
        with client.websocket_connect("/api/v1/messaging/ws?token=valid") as websocket:
            websocket.receive_json()  # connected
            websocket.send_json({"type": "ping"})
            data = websocket.receive_json()
        assert data == {"type": "pong"}

    def test_ws_send_message_inserts_and_broadcasts_to_sender(self, monkeypatch):
        """成员发送消息 → INSERT im_conv_message → 广播回 sender（sender 是成员）。"""
        conv = _conv_row()
        sent = _msg_row(content="hi", sender_id="usr_test")
        # 0) WS 连接后的离线补投 SELECT（空队列）
        # 1) SELECT 会话 2) 成员校验 3) INSERT RETURNING 4) 查询成员列表
        conn = _RecordingConnection(
            results=[
                _Result(rows=[]),
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(row=sent),
                _Result(rows=[{"user_id": "usr_test"}]),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        # 用真实 ConnectionManager（fresh），sender 在线 → 广播回 sender
        monkeypatch.setattr(msg, "manager", msg.ConnectionManager())
        monkeypatch.setattr(msg, "decode_token_cached", _decode_ok)

        client = TestClient(_app())
        with client.websocket_connect("/api/v1/messaging/ws?token=valid") as websocket:
            websocket.receive_json()  # connected
            websocket.send_json(
                {"type": "message", "conversation_id": "imc_1", "content": "hi"}
            )
            data = websocket.receive_json()
        # 广播回 sender 的消息
        assert data["type"] == "message"
        assert data["conversation_id"] == "imc_1"
        assert data["sender_id"] == "usr_test"
        assert data["content"] == "hi"
        assert "created_at" in data
        # INSERT 已触达
        inserts = [c for c in conn.calls if "INSERT INTO im_conv_message" in c[0]]
        assert len(inserts) == 1
        assert inserts[0][1][2] == "usr_test"  # sender_id
        assert inserts[0][1][3] == "hi"  # content

    def test_ws_non_member_send_message_rejected_without_insert(self, monkeypatch):
        """非会话成员发送消息 → 收到 error，且不触达 INSERT。"""
        conv = _conv_row()
        # 0) 离线补投 SELECT（空队列）
        # 1) SELECT 会话(workspace 匹配) 2) 成员校验返回 None
        conn = _RecordingConnection(
            results=[_Result(rows=[]), _Result(row=conv), _Result(row=None)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        monkeypatch.setattr(msg, "manager", msg.ConnectionManager())
        monkeypatch.setattr(msg, "decode_token_cached", _decode_ok)

        client = TestClient(_app())
        with client.websocket_connect("/api/v1/messaging/ws?token=valid") as websocket:
            websocket.receive_json()  # connected
            websocket.send_json(
                {"type": "message", "conversation_id": "imc_1", "content": "hi"}
            )
            data = websocket.receive_json()
        assert data["type"] == "error"
        assert "not a member" in data["detail"]
        # 不应触达 INSERT
        inserts = [c for c in conn.calls if "INSERT INTO im_conv_message" in c[0]]
        assert len(inserts) == 0

    def test_ws_typing_broadcasts_to_other_members(self, monkeypatch):
        """typing 事件 → 广播给会话其他在线成员（不含 sender）。"""
        conv = _conv_row()
        # 0) 离线补投 SELECT（空队列）
        # 1) SELECT 会话 2) 成员校验 3) 查询其他成员(usr_other)
        conn = _RecordingConnection(
            results=[
                _Result(rows=[]),
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(rows=[{"user_id": "usr_other"}]),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        recording = _RecordingManager()
        monkeypatch.setattr(msg, "manager", recording)
        monkeypatch.setattr(msg, "decode_token_cached", _decode_ok)

        client = TestClient(_app())
        with client.websocket_connect("/api/v1/messaging/ws?token=valid") as websocket:
            websocket.receive_json()  # connected
            websocket.send_json(
                {"type": "typing", "conversation_id": "imc_1"}
            )
            # typing 不回传给 sender，用 ping/pong 作为同步屏障确保服务端处理完成
            websocket.send_json({"type": "ping"})
            pong = websocket.receive_json()
            assert pong == {"type": "pong"}
        # 广播记录：1 条 typing 事件，目标为其他成员 usr_other
        assert len(recording.broadcasts) == 1
        conv_id, member_ids, payload = recording.broadcasts[0]
        assert conv_id == "imc_1"
        assert member_ids == ["usr_other"]
        assert payload["type"] == "typing"
        assert payload["sender_id"] == "usr_test"
        # typing 查询 SQL 必须排除 sender
        typing_queries = [
            c for c in conn.calls if "user_id <> %s" in c[0]
        ]
        assert len(typing_queries) == 1
        assert typing_queries[0][1] == ("imc_1", "usr_test")


# ============================================================================
# 3. 在线状态查询端点
# ============================================================================


class TestPresenceEndpoint:
    """GET /conversations/{id}/presence：返回成员在线状态。"""

    async def test_presence_returns_online_status(self, monkeypatch):
        """presence 返回每个成员的 online 标记（usr_a 在线 / usr_b 离线）。"""
        conv = _conv_row()
        members = [{"user_id": "usr_a"}, {"user_id": "usr_b"}]
        # 1) 会话查询 2) 成员校验 3) 成员列表
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(rows=members),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        # 用真实 ConnectionManager，连接 usr_a 的假 ws
        mgr = msg.ConnectionManager()
        ws_a = _FakeWS()
        await mgr.connect("usr_a", ws_a)
        monkeypatch.setattr(msg, "manager", mgr)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/messaging/conversations/imc_1/presence")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "members": [
                {"user_id": "usr_a", "online": True},
                {"user_id": "usr_b", "online": False},
            ]
        }
        # 成员列表 SQL 按 conversation_id 过滤
        member_query = conn.calls[2][0]
        assert "im_conversation_member" in member_query
        assert conn.calls[2][1] == ("imc_1",)


# ============================================================================
# 4. REST POST /messages 触发广播
# ============================================================================


class TestRestMessageBroadcast:
    """POST /conversations/{id}/messages：INSERT 后广播（响应结构不变）。"""

    async def test_rest_send_message_triggers_broadcast(self, monkeypatch):
        """REST 发送消息成功返回 201 且响应结构不变，并触发 manager.broadcast。"""
        conv = _conv_row()
        sent = _msg_row(content="hi", sender_id="usr_test")
        # 1) 会话查询 2) 成员校验 3) INSERT RETURNING 4) 查询成员列表
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(row=sent),
                _Result(rows=[{"user_id": "usr_test"}, {"user_id": "usr_other"}]),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        recording = _RecordingManager()
        monkeypatch.setattr(msg, "manager", recording)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations/imc_1/messages",
                json={"content": "hi"},
            )
        # 响应结构不变（与 test_messaging.test_send_message_success 一致）
        assert resp.status_code == 201
        body = resp.json()
        assert body["content"] == "hi"
        assert body["sender_id"] == "usr_test"
        assert body["conversation_id"] == "imc_1"
        assert body["id"] == "icm_1"
        assert "created_at" in body
        # 广播被触发，目标为会话所有成员
        assert len(recording.broadcasts) == 1
        conv_id, member_ids, payload = recording.broadcasts[0]
        assert conv_id == "imc_1"
        assert member_ids == ["usr_test", "usr_other"]
        assert payload["type"] == "message"
        assert payload["content"] == "hi"
        assert payload["sender_id"] == "usr_test"
