"""M7 IM 通道增强模块测试：离线消息队列 + 群组管理。

覆盖（16 个测试）：
- 离线消息：发送消息时离线成员 unread_count+1 (1)
- 离线消息：WS 连接时自动投递未读消息 (1)
- get_unread_count 返回正确计数 (1)
- mark_read 清零 unread_count (1)
- add_member 成功（group）(1)
- add_member 403（非创建者）(1)
- add_member 422（direct 类型不允许）(1)
- add_member 409（已在成员中）(1)
- remove_member 成功 (1)
- remove_member 403（非创建者）(1)
- remove_member 422（不能移除自己）(1)
- remove_member 后成员=0 删会话 (1)
- update_conversation 成功 (1)
- update_conversation 403（非创建者）(1)
- list_members 返回成员 + 在线状态 (1)
- schema 包含 ALTER TABLE 离线消息扩展 (1)

所有测试使用 fake pool/connection（与 test_messaging.py 同风格），不依赖真实 DB。
WS 测试使用 starlette.testclient.TestClient（内存 in-process）。
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
# 测试辅助：fake pool / connection / result（复用 test_messaging.py 模式）
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
    """记录广播调用的假 ConnectionManager。"""

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
        "sender_id": "usr_other",
        "content": "hello",
        "created_at": datetime.now(UTC),
        "delivered_at": None,
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


# ============================================================================
# 1. 离线消息：发送消息时离线成员 unread_count+1
# ============================================================================


class TestOfflineMessageIncrement:
    """POST /conversations/{id}/messages：离线成员 unread_count +1。"""

    @pytest.mark.asyncio
    async def test_send_message_increments_offline_member_unread(self, monkeypatch):
        """发送消息时，对离线成员（非发送者）执行 unread_count +1 的 UPDATE。"""
        conv = _conv_row(type="group")
        sent = _msg_row(sender_id="usr_test", content="hi")
        # 1) 会话查询 2) 成员校验 3) INSERT RETURNING 4) 查询成员列表
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(row=sent),
                _Result(
                    rows=[
                        {"user_id": "usr_test"},
                        {"user_id": "usr_other"},
                    ]
                ),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        # 用 _RecordingManager，usr_other 不在线
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
        assert resp.status_code == 201
        # 找到 unread_count UPDATE 调用
        unread_updates = [
            c for c in conn.calls if "unread_count = unread_count + 1" in c[0]
        ]
        assert len(unread_updates) == 1
        # 参数：conversation_id + offline_member_ids 列表
        query, params = unread_updates[0]
        assert params[0] == "imc_1"
        # usr_other 在离线列表中（usr_test 是发送者被排除）
        assert "usr_other" in params[1]
        assert "usr_test" not in params[1]


# ============================================================================
# 2. 离线消息：WS 连接时自动投递未读消息
# ============================================================================


class TestWsDeliverUndelivered:
    """WS 连接时自动投递 delivered_at IS NULL 的消息并标记 delivered_at。"""

    def test_ws_connect_delivers_undelivered_messages(self, monkeypatch):
        """WS 连接后自动推送未投递消息，并执行 UPDATE delivered_at。"""
        undelivered = _msg_row(
            id="icm_old",
            sender_id="usr_other",
            content="undelivered hi",
            delivered_at=None,
        )
        # _deliver_undelivered_messages 的 2 次 pool.connection() 调用：
        # call 0: SELECT 未投递消息 → 返回 1 条
        # call 1: UPDATE delivered_at → 默认空 _Result
        conn = _RecordingConnection(results=[_Result(rows=[undelivered])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        monkeypatch.setattr(msg, "manager", msg.ConnectionManager())
        monkeypatch.setattr(msg, "decode_token_cached", _decode_ok)

        client = TestClient(_app())
        with client.websocket_connect("/api/v1/messaging/ws?token=valid") as websocket:
            data = websocket.receive_json()  # connected
            assert data["type"] == "connected"
            msg_data = websocket.receive_json()  # 未投递消息
            assert msg_data["type"] == "message"
            assert msg_data["content"] == "undelivered hi"
            assert msg_data["sender_id"] == "usr_other"
            assert msg_data.get("backfilled") is True
            # 用 ping/pong 作为同步屏障
            websocket.send_json({"type": "ping"})
            pong = websocket.receive_json()
            assert pong == {"type": "pong"}

        # 校验 SELECT 未投递消息 SQL 包含 delivered_at IS NULL
        select_calls = [c for c in conn.calls if "delivered_at IS NULL" in c[0]]
        assert len(select_calls) == 1
        # 校验 UPDATE delivered_at 被调用
        update_calls = [
            c for c in conn.calls if "SET delivered_at = now()" in c[0]
        ]
        assert len(update_calls) == 1
        # UPDATE 参数包含未投递消息的 ID
        assert "icm_old" in update_calls[0][1][0]


# ============================================================================
# 3. get_unread_count 返回正确计数
# ============================================================================


class TestGetUnreadCount:
    """GET /conversations/{id}/unread：返回当前用户未读消息数。"""

    @pytest.mark.asyncio
    async def test_get_unread_count_returns_correct_count(self, monkeypatch):
        """返回 unread_count 字段值。"""
        conv = _conv_row()
        # 1) 会话查询 2) 成员校验 3) 查询 unread_count
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(row={"unread_count": 5}),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/messaging/conversations/imc_1/unread"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["conversation_id"] == "imc_1"
        assert body["unread_count"] == 5
        # SQL 包含 unread_count 字段
        unread_query = conn.calls[2][0]
        assert "unread_count" in unread_query
        assert conn.calls[2][1] == ("imc_1", "usr_test")


# ============================================================================
# 4. mark_read 清零 unread_count
# ============================================================================


class TestMarkReadClearsUnread:
    """POST /conversations/{id}/read：同时清零 unread_count（响应结构不变）。"""

    @pytest.mark.asyncio
    async def test_mark_read_clears_unread_count(self, monkeypatch):
        """标记已读时 UPDATE SQL 同时 SET unread_count = 0，响应结构不变。"""
        conv = _conv_row()
        now = datetime.now(UTC)
        read_row = {
            "conversation_id": "imc_1",
            "user_id": "usr_test",
            "last_read_at": now,
        }
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(row=read_row),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/messaging/conversations/imc_1/read")
        assert resp.status_code == 200
        body = resp.json()
        # 响应结构不变：conversation_id / user_id / last_read_at
        assert body["conversation_id"] == "imc_1"
        assert body["user_id"] == "usr_test"
        assert body["last_read_at"] is not None
        # UPDATE SQL 同时包含 last_read_at = now() 和 unread_count = 0
        update_query = conn.calls[2][0]
        assert "last_read_at = now()" in update_query
        assert "unread_count = 0" in update_query


# ============================================================================
# 5. add_member 成功（group）
# ============================================================================


class TestAddMember:
    """POST /conversations/{id}/members：添加群组成员。"""

    @pytest.mark.asyncio
    async def test_add_member_success_group(self, monkeypatch):
        """创建者向 group 会话添加新成员成功返回 201。"""
        conv = _conv_row(type="group", created_by="usr_test")
        # 1) 会话查询 2) 已在成员中查询(返回 None) 3) INSERT
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row=None),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations/imc_1/members",
                json={"user_id": "usr_new"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["conversation_id"] == "imc_1"
        assert body["user_id"] == "usr_new"
        # INSERT 成员被调用
        inserts = [
            c for c in conn.calls if "INSERT INTO im_conversation_member" in c[0]
        ]
        assert len(inserts) == 1
        assert inserts[0][1] == ("imc_1", "usr_new")

    @pytest.mark.asyncio
    async def test_add_member_forbidden_non_creator(self, monkeypatch):
        """非创建者（member 角色）添加成员返回 403。"""
        conv = _conv_row(type="group", created_by="usr_other")
        conn = _RecordingConnection(results=[_Result(row=conv)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_test", role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations/imc_1/members",
                json={"user_id": "usr_new"},
            )
        assert resp.status_code == 403
        # 不应触达 INSERT
        inserts = [
            c for c in conn.calls if "INSERT INTO im_conversation_member" in c[0]
        ]
        assert len(inserts) == 0

    @pytest.mark.asyncio
    async def test_add_member_unprocessable_direct(self, monkeypatch):
        """创建者向 direct 会话添加成员返回 422。"""
        conv = _conv_row(type="direct", created_by="usr_test")
        conn = _RecordingConnection(results=[_Result(row=conv)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations/imc_1/members",
                json={"user_id": "usr_new"},
            )
        assert resp.status_code == 422
        # 不应触达 INSERT
        inserts = [
            c for c in conn.calls if "INSERT INTO im_conversation_member" in c[0]
        ]
        assert len(inserts) == 0

    @pytest.mark.asyncio
    async def test_add_member_conflict_already_member(self, monkeypatch):
        """添加已是成员的用户返回 409。"""
        conv = _conv_row(type="group", created_by="usr_test")
        # 1) 会话查询 2) 已在成员中查询(返回 row) → 409
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row={"?column?": 1}),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations/imc_1/members",
                json={"user_id": "usr_existing"},
            )
        assert resp.status_code == 409
        # 不应触达 INSERT
        inserts = [
            c for c in conn.calls if "INSERT INTO im_conversation_member" in c[0]
        ]
        assert len(inserts) == 0


# ============================================================================
# 6. remove_member
# ============================================================================


class TestRemoveMember:
    """DELETE /conversations/{id}/members/{user_id}：移除群组成员。"""

    @pytest.mark.asyncio
    async def test_remove_member_success(self, monkeypatch):
        """创建者移除其他成员成功返回 204，会话保留（仍有剩余成员）。"""
        conv = _conv_row(type="group", created_by="usr_test")
        # 1) 会话查询 2) DELETE member 3) SELECT 剩余成员(有)
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(),
                _Result(row={"?column?": 1}),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete(
                "/api/v1/messaging/conversations/imc_1/members/usr_other"
            )
        assert resp.status_code == 204
        # DELETE member 被调用
        delete_member = [
            c for c in conn.calls if "DELETE FROM im_conversation_member" in c[0]
        ]
        assert len(delete_member) == 1
        assert delete_member[0][1] == ("imc_1", "usr_other")
        # 不应删除会话本身
        delete_conv = [
            c for c in conn.calls if "DELETE FROM im_conversation " in c[0]
        ]
        assert len(delete_conv) == 0

    @pytest.mark.asyncio
    async def test_remove_member_forbidden_non_creator(self, monkeypatch):
        """非创建者移除成员返回 403。"""
        conv = _conv_row(type="group", created_by="usr_other")
        conn = _RecordingConnection(results=[_Result(row=conv)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_test", role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete(
                "/api/v1/messaging/conversations/imc_1/members/usr_target"
            )
        assert resp.status_code == 403
        # 不应触达 DELETE
        deletes = [
            c for c in conn.calls if "DELETE FROM im_conversation_member" in c[0]
        ]
        assert len(deletes) == 0

    @pytest.mark.asyncio
    async def test_remove_member_unprocessable_self(self, monkeypatch):
        """移除自己返回 422，不触达数据库。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete(
                "/api/v1/messaging/conversations/imc_1/members/usr_test"
            )
        assert resp.status_code == 422
        # 不应触达任何数据库调用（self 检查在 DB 之前）
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_remove_member_deletes_conversation_when_empty(self, monkeypatch):
        """移除后无剩余成员时删除会话本身。"""
        conv = _conv_row(type="group", created_by="usr_test")
        # 1) 会话查询 2) DELETE member 3) SELECT 剩余(None) 4) DELETE 会话
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(),
                _Result(row=None),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete(
                "/api/v1/messaging/conversations/imc_1/members/usr_other"
            )
        assert resp.status_code == 204
        # DELETE member 被调用
        delete_member = [
            c for c in conn.calls if "DELETE FROM im_conversation_member" in c[0]
        ]
        assert len(delete_member) == 1
        # DELETE 会话也被调用
        delete_conv = [
            c for c in conn.calls if "DELETE FROM im_conversation " in c[0]
        ]
        assert len(delete_conv) == 1
        assert delete_conv[0][1] == ("imc_1",)


# ============================================================================
# 7. update_conversation
# ============================================================================


class TestUpdateConversation:
    """PATCH /conversations/{id}：更新会话信息。"""

    @pytest.mark.asyncio
    async def test_update_conversation_success(self, monkeypatch):
        """创建者更新 title 成功返回 200。"""
        conv = _conv_row(type="group", created_by="usr_test")
        updated = _conv_row(type="group", title="New Title", created_by="usr_test")
        # 1) 会话查询 2) UPDATE RETURNING
        conn = _RecordingConnection(
            results=[_Result(row=conv), _Result(row=updated)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/messaging/conversations/imc_1",
                json={"title": "New Title"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "New Title"
        assert body["id"] == "imc_1"
        # UPDATE SQL 包含 title
        update_query, update_params = conn.calls[1]
        assert "UPDATE im_conversation SET title" in update_query
        assert update_params == ("New Title", "imc_1")

    @pytest.mark.asyncio
    async def test_update_conversation_forbidden_non_creator(self, monkeypatch):
        """非创建者更新会话返回 403。"""
        conv = _conv_row(created_by="usr_other")
        conn = _RecordingConnection(results=[_Result(row=conv)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_test", role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/messaging/conversations/imc_1",
                json={"title": "Hacked"},
            )
        assert resp.status_code == 403
        # 不应触达 UPDATE
        updates = [c for c in conn.calls if "UPDATE im_conversation" in c[0]]
        assert len(updates) == 0


# ============================================================================
# 8. list_members 返回成员 + 在线状态
# ============================================================================


class TestListMembers:
    """GET /conversations/{id}/members：列出成员含在线状态。"""

    @pytest.mark.asyncio
    async def test_list_members_returns_members_with_online_status(self, monkeypatch):
        """返回成员列表，每个成员含 user_id / online / unread_count 等字段。"""
        conv = _conv_row(type="group")
        now = datetime.now(UTC)
        member_rows = [
            {
                "user_id": "usr_a",
                "joined_at": now,
                "last_read_at": now,
                "unread_count": 0,
            },
            {
                "user_id": "usr_b",
                "joined_at": now,
                "last_read_at": None,
                "unread_count": 3,
            },
        ]
        # 1) 会话查询 2) 成员校验 3) 查询成员列表
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(rows=member_rows),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        # 用真实 ConnectionManager，连接 usr_a
        mgr = msg.ConnectionManager()
        ws_a = _FakeWS()
        await mgr.connect("usr_a", ws_a)
        monkeypatch.setattr(msg, "manager", mgr)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/messaging/conversations/imc_1/members"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["members"]) == 2
        # usr_a 在线
        assert body["members"][0]["user_id"] == "usr_a"
        assert body["members"][0]["online"] is True
        assert body["members"][0]["unread_count"] == 0
        # usr_b 离线
        assert body["members"][1]["user_id"] == "usr_b"
        assert body["members"][1]["online"] is False
        assert body["members"][1]["unread_count"] == 3
        # 成员查询 SQL 包含 unread_count 字段
        member_query = conn.calls[2][0]
        assert "unread_count" in member_query
        assert "ORDER BY joined_at ASC" in member_query


# ============================================================================
# 9. Schema 扩展验证
# ============================================================================


class TestSchemaEnhanced:
    """SCHEMA_STATEMENTS 包含离线消息扩展的 ALTER TABLE 语句。"""

    @pytest.mark.asyncio
    async def test_schema_includes_alter_statements_for_offline_messages(self):
        """SCHEMA_STATEMENTS 包含 unread_count 和 delivered_at 的 ALTER TABLE。"""
        all_sql = "\n".join(msg.SCHEMA_STATEMENTS)
        # ALTER TABLE ADD COLUMN IF NOT EXISTS 保证幂等
        assert "ALTER TABLE im_conversation_member ADD COLUMN IF NOT EXISTS unread_count" in all_sql
        assert "ALTER TABLE im_conv_message ADD COLUMN IF NOT EXISTS delivered_at" in all_sql
        # 部分索引
        assert "idx_im_conv_message_undelivered" in all_sql
        assert "idx_im_conv_member_unread" in all_sql

    @pytest.mark.asyncio
    async def test_ensure_messaging_schema_executes_all_including_alters(self):
        """ensure_messaging_schema 执行所有语句（含 ALTER TABLE）。"""
        conn = _RecordingConnection()
        await msg.ensure_messaging_schema(conn)
        # 调用次数应等于 SCHEMA_STATEMENTS 长度
        assert len(conn.calls) == len(msg.SCHEMA_STATEMENTS)
        # 每条调用的 SQL 应与 SCHEMA_STATEMENTS 一致
        for idx, statement in enumerate(msg.SCHEMA_STATEMENTS):
            assert conn.calls[idx][0] == statement
