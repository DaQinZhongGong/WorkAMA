"""P3 v7.180 IM 增强测试：持久化离线队列 + 每成员投递游标 + 群治理。

覆盖本次新增/改动的后端能力：
- ``_enqueue_offline_messages``：离线成员入队 + pending_count 累加 + 幂等
- ``_deliver_undelivered_messages``：WS 重连补投 + 标记 delivered_at + 推进游标
- ``GET  /api/v1/im/delivery-cursors``
- ``POST /api/v1/im/offline-messages/ack-batch``
- ``POST /api/v1/im/groups/{group_id}/transfer-ownership``
- ``PATCH /api/v1/im/groups/{group_id}/members/{user_id}/role``
- ``GET  /api/v1/im/groups/{group_id}/audit``
- ``GET  /api/v1/im/messages/{message_id}/history``
- 撤回/编辑对未投递离线副本的同步改写（离线成员上线不应读到原文）
- 鉴权（capability 缺失 403 / 非 owner 403）与 workspace 隔离（404）

与既有 messaging 测试同风格：全部使用 fake pool/connection，不依赖真实 DB。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from workama_platform.core import Actor, get_actor
from workama_platform.modules import messaging as msg


# ============================================================================
# 测试辅助
# ============================================================================


class _Result:
    """模拟 psycopg 查询结果。"""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = list(rows) if rows is not None else []

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


class _RecordingManager:
    """记录广播的假 ConnectionManager；online 集合控制在线判定。"""

    def __init__(self, online: set[str] | None = None):
        self.broadcasts: list[tuple[str, list[str], dict]] = []
        self.online: set[str] = set(online or ())

    async def connect(self, websocket, user_id):
        await websocket.accept()

    def disconnect(self, websocket, user_id):
        return None

    async def send_to_user(self, user_id, message):
        return None

    async def broadcast_to_conversation(self, conversation_id, member_ids, message):
        self.broadcasts.append((conversation_id, list(member_ids), message))

    def is_online(self, user_id):
        return user_id in self.online


def _actor(
    *,
    workspace_id="wsp_test",
    user_id="usr_test",
    role="member",
    capabilities=None,
) -> Actor:
    if capabilities is None:
        capabilities = ("im:*", "messaging:*")
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="user@workama.example.com",
        display_name="User",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _app(actor: Actor | None = None) -> FastAPI:
    """挂载 router + im_router 的测试 app。"""
    app = FastAPI()
    app.include_router(msg.router)
    app.include_router(msg.im_router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


def _conv_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "imc_1",
        "workspace_id": "wsp_test",
        "type": "group",
        "title": "Team",
        "created_by": "usr_test",
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _group_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "img_1",
        "workspace_id": "wsp_test",
        "name": "Group A",
        "owner_id": "usr_test",
        "announcement": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _group_member_row(**overrides) -> dict[str, Any]:
    base = {
        "group_id": "img_1",
        "user_id": "usr_test",
        "role": "owner",
        "joined_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _cursor_row(**overrides) -> dict[str, Any]:
    base = {
        "workspace_id": "wsp_test",
        "conversation_id": "imc_1",
        "user_id": "usr_test",
        "last_delivered_message_id": "icm_9",
        "last_delivered_at": datetime.now(UTC),
        "last_acked_message_id": None,
        "last_acked_at": None,
        "pending_count": 3,
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _offline_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "imo_1",
        "workspace_id": "wsp_test",
        "conversation_id": "imc_1",
        "sender_id": "usr_other",
        "recipient_id": "usr_test",
        "message_id": "icm_1",
        "payload": {
            "type": "message",
            "id": "icm_1",
            "conversation_id": "imc_1",
            "sender_id": "usr_other",
            "content": "offline hello",
            "created_at": datetime.now(UTC).isoformat(),
        },
        "created_at": datetime.now(UTC),
        "delivered_at": None,
        "acked_at": None,
    }
    base.update(overrides)
    return base


def _msg_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "icm_1",
        "conversation_id": "imc_1",
        "sender_id": "usr_test",
        "content": "hello",
        "created_at": datetime.now(UTC),
        "delivered_at": None,
        "retracted_at": None,
        "edited_at": None,
        "conv_workspace_id": "wsp_test",
    }
    base.update(overrides)
    return base


async def _decode_ok(token, expected_type="access"):
    return {"sub": "usr_test", "ws": "wsp_test", "type": "access"}


def _sql_of(calls, needle):
    """返回 SQL 中包含 needle 的调用列表。"""
    return [c for c in calls if needle in c[0]]


# ============================================================================
# 1. 离线入队：_enqueue_offline_messages
# ============================================================================


class TestEnqueueOfflineMessages:
    """离线成员入队 + pending_count 累加 + 幂等语义。"""

    @pytest.mark.asyncio
    async def test_enqueue_inserts_one_row_and_cursor_per_recipient(self):
        """每个离线收件人写 1 行队列 + 1 次 pending_count 累加。"""
        conn = _RecordingConnection()
        created_at = datetime.now(UTC)
        await msg._enqueue_offline_messages(
            conn,
            workspace_id="wsp_test",
            conversation_id="imc_1",
            sender_id="usr_sender",
            message_id="icm_1",
            content="hi there",
            created_at=created_at,
            recipient_ids=["usr_a", "usr_b"],
        )
        inserts = _sql_of(conn.calls, "INSERT INTO im_offline_message")
        assert len(inserts) == 2
        cursors = _sql_of(conn.calls, "INSERT INTO im_delivery_cursor")
        assert len(cursors) == 2
        # 收件人分别为 usr_a / usr_b（第 5 个参数是 recipient_id）
        assert [c[1][4] for c in inserts] == ["usr_a", "usr_b"]
        # message_id 同步写入，供唯一索引与后续同步改写使用
        assert all(c[1][6] == "icm_1" for c in inserts)

    @pytest.mark.asyncio
    async def test_enqueue_payload_is_serialized_message_envelope(self):
        """payload 为可直接下发的 WS 信封 JSON。"""
        conn = _RecordingConnection()
        created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        await msg._enqueue_offline_messages(
            conn,
            workspace_id="wsp_test",
            conversation_id="imc_1",
            sender_id="usr_sender",
            message_id="icm_1",
            content="payload check",
            created_at=created_at,
            recipient_ids=["usr_a"],
        )
        payload = json.loads(_sql_of(conn.calls, "INSERT INTO im_offline_message")[0][1][5])
        assert payload["type"] == "message"
        assert payload["id"] == "icm_1"
        assert payload["conversation_id"] == "imc_1"
        assert payload["sender_id"] == "usr_sender"
        assert payload["content"] == "payload check"
        # datetime 必须被序列化成 ISO 字符串，否则 json.dumps 会抛错
        assert payload["created_at"] == created_at.isoformat()

    @pytest.mark.asyncio
    async def test_enqueue_uses_on_conflict_do_nothing_for_idempotency(self):
        """重复入队依赖唯一索引 + ON CONFLICT DO NOTHING，不会写重复行。"""
        conn = _RecordingConnection()
        await msg._enqueue_offline_messages(
            conn,
            workspace_id="wsp_test",
            conversation_id="imc_1",
            sender_id="usr_sender",
            message_id="icm_1",
            content="dup",
            created_at=datetime.now(UTC),
            recipient_ids=["usr_a"],
        )
        insert_sql = _sql_of(conn.calls, "INSERT INTO im_offline_message")[0][0]
        assert "ON CONFLICT DO NOTHING" in insert_sql
        cursor_sql = _sql_of(conn.calls, "INSERT INTO im_delivery_cursor")[0][0]
        assert "ON CONFLICT (conversation_id, user_id) DO UPDATE" in cursor_sql
        assert "pending_count = im_delivery_cursor.pending_count + 1" in cursor_sql

    @pytest.mark.asyncio
    async def test_enqueue_noop_for_empty_recipients(self):
        """无离线收件人时不产生任何 SQL。"""
        conn = _RecordingConnection()
        await msg._enqueue_offline_messages(
            conn,
            workspace_id="wsp_test",
            conversation_id="imc_1",
            sender_id="usr_sender",
            message_id="icm_1",
            content="nobody offline",
            created_at=datetime.now(UTC),
            recipient_ids=[],
        )
        assert conn.calls == []

    @pytest.mark.asyncio
    async def test_send_message_enqueues_only_offline_members(self, monkeypatch):
        """REST 发消息：仅离线成员入队，在线成员走实时广播不入队。"""
        conv = _conv_row()
        sent = {
            "id": "icm_new",
            "conversation_id": "imc_1",
            "sender_id": "usr_test",
            "content": "hello team",
            "created_at": datetime.now(UTC),
        }
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),                       # 会话
                _Result(row={"?column?": 1}),            # 成员校验
                _Result(row=sent),                       # INSERT RETURNING
                _Result(rows=[
                    {"user_id": "usr_test"},
                    {"user_id": "usr_online"},
                    {"user_id": "usr_offline"},
                ]),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        monkeypatch.setattr(msg, "manager", _RecordingManager(online={"usr_online"}))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations/imc_1/messages",
                json={"content": "hello team"},
            )
        assert resp.status_code == 201
        inserts = _sql_of(conn.calls, "INSERT INTO im_offline_message")
        assert len(inserts) == 1
        assert inserts[0][1][4] == "usr_offline"
        # unread_count 也只对离线成员累加
        unread = _sql_of(conn.calls, "SET unread_count = unread_count + 1")
        assert unread[0][1][1] == ["usr_offline"]


# ============================================================================
# 2. 离线补投：_deliver_undelivered_messages
# ============================================================================


class TestDeliverUndeliveredMessages:
    """WS 重连补投：per-member 队列取数 → 推送 → 标记 → 推进游标。"""

    @pytest.mark.asyncio
    async def test_delivery_reads_per_member_queue_with_workspace_filter(
        self, monkeypatch
    ):
        """取数必须同时按 recipient_id + workspace_id + 未投递过滤。"""
        conn = _RecordingConnection(results=[_Result(rows=[_offline_row()])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        ws = _CollectingWS()
        count = await msg._deliver_undelivered_messages(ws, "usr_test", "wsp_test")
        assert count == 1
        select_sql, select_params = conn.calls[0]
        assert "FROM im_offline_message" in select_sql
        assert "recipient_id = %s" in select_sql
        assert "workspace_id = %s" in select_sql
        assert "delivered_at IS NULL" in select_sql
        assert select_params[0] == "usr_test"
        assert select_params[1] == "wsp_test"

    @pytest.mark.asyncio
    async def test_delivery_envelope_has_offline_id_and_backfilled_flag(
        self, monkeypatch
    ):
        """补投信封带 offline_id（用于 ack）与 backfilled=True（用于前端去重）。"""
        conn = _RecordingConnection(results=[_Result(rows=[_offline_row()])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        ws = _CollectingWS()
        await msg._deliver_undelivered_messages(ws, "usr_test", "wsp_test")
        assert len(ws.sent) == 1
        envelope = ws.sent[0]
        assert envelope["type"] == "message"
        assert envelope["content"] == "offline hello"
        assert envelope["offline_id"] == "imo_1"
        assert envelope["backfilled"] is True

    @pytest.mark.asyncio
    async def test_delivery_marks_delivered_and_advances_cursor(self, monkeypatch):
        """投递成功后批量标记 delivered_at 并按会话推进投递游标。"""
        rows = [
            _offline_row(id="imo_1", message_id="icm_1"),
            _offline_row(id="imo_2", message_id="icm_2"),
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        ws = _CollectingWS()
        count = await msg._deliver_undelivered_messages(ws, "usr_test", "wsp_test")
        assert count == 2
        updates = _sql_of(conn.calls, "SET delivered_at = now()")
        assert len(updates) == 1
        assert updates[0][1][0] == ["imo_1", "imo_2"]
        cursors = _sql_of(conn.calls, "INSERT INTO im_delivery_cursor")
        # 同一会话只推进一次，取本批最后一条
        assert len(cursors) == 1
        assert cursors[0][1] == ("wsp_test", "imc_1", "usr_test", "icm_2")

    @pytest.mark.asyncio
    async def test_delivery_stops_and_keeps_rows_when_send_fails(self, monkeypatch):
        """发送失败即停止，未成功的行不标记，保证下次重连继续补投。"""
        rows = [_offline_row(id="imo_1"), _offline_row(id="imo_2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        ws = _CollectingWS(fail_after=1)
        count = await msg._deliver_undelivered_messages(ws, "usr_test", "wsp_test")
        assert count == 1
        updates = _sql_of(conn.calls, "SET delivered_at = now()")
        # 只标记成功投递的那一条
        assert updates[0][1][0] == ["imo_1"]

    @pytest.mark.asyncio
    async def test_delivery_handles_json_string_payload(self, monkeypatch):
        """payload 以 JSON 字符串返回时（非 dict 适配器）同样能解析下发。"""
        row = _offline_row()
        row["payload"] = json.dumps(row["payload"])
        conn = _RecordingConnection(results=[_Result(rows=[row])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        ws = _CollectingWS()
        count = await msg._deliver_undelivered_messages(ws, "usr_test", "wsp_test")
        assert count == 1
        assert ws.sent[0]["content"] == "offline hello"

    @pytest.mark.asyncio
    async def test_delivery_rebuilds_envelope_for_legacy_rows_without_payload(
        self, monkeypatch
    ):
        """payload 列引入前的历史行（payload 为 NULL）用行自身列重建信封。"""
        row = _offline_row(payload=None, content="legacy text")
        conn = _RecordingConnection(results=[_Result(rows=[row])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        ws = _CollectingWS()
        count = await msg._deliver_undelivered_messages(ws, "usr_test", "wsp_test")
        assert count == 1
        assert ws.sent[0]["content"] == "legacy text"
        assert ws.sent[0]["id"] == "icm_1"
        assert ws.sent[0]["backfilled"] is True

    @pytest.mark.asyncio
    async def test_delivery_noop_on_empty_queue(self, monkeypatch):
        """队列为空时不推送、不写库。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        ws = _CollectingWS()
        count = await msg._deliver_undelivered_messages(ws, "usr_test", "wsp_test")
        assert count == 0
        assert ws.sent == []
        assert _sql_of(conn.calls, "SET delivered_at = now()") == []


class _CollectingWS:
    """收集 send_json 的假 WebSocket；fail_after 条之后抛错模拟断连。"""

    def __init__(self, fail_after: int | None = None):
        self.sent: list[dict] = []
        self._fail_after = fail_after

    async def send_json(self, message):
        if self._fail_after is not None and len(self.sent) >= self._fail_after:
            raise RuntimeError("connection lost")
        self.sent.append(message)


# ============================================================================
# 3. GET /api/v1/im/delivery-cursors
# ============================================================================


class TestListDeliveryCursors:
    """投递游标查询：workspace + 用户隔离、会话过滤、鉴权。"""

    @pytest.mark.asyncio
    async def test_list_cursors_filters_by_workspace_and_user(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(rows=[_cursor_row()])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/delivery-cursors")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["items"][0]["conversation_id"] == "imc_1"
        assert body["items"][0]["pending_count"] == 3
        query, params = conn.calls[0]
        assert "FROM im_delivery_cursor" in query
        assert "workspace_id = %s" in query
        assert "user_id = %s" in query
        assert params[0] == "wsp_test"
        assert params[1] == "usr_test"

    @pytest.mark.asyncio
    async def test_list_cursors_conversation_filter_is_applied(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/im/delivery-cursors?conversation_id=imc_9"
            )
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "conversation_id = %s" in query
        assert "imc_9" in params

    @pytest.mark.asyncio
    async def test_list_cursors_requires_read_capability(self, monkeypatch):
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=("workflow:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/delivery-cursors")
        assert resp.status_code == 403
        # fail-closed：鉴权失败不触达数据库
        assert conn.calls == []


# ============================================================================
# 4. POST /api/v1/im/offline-messages/ack-batch
# ============================================================================


class TestAckOfflineMessagesBatch:
    """批量确认：只影响本人本 workspace 的行；推进 acked 游标；幂等。"""

    @pytest.mark.asyncio
    async def test_ack_batch_by_message_ids_scopes_to_actor(self, monkeypatch):
        rows = [
            {
                "id": "imo_1",
                "conversation_id": "imc_1",
                "message_id": "icm_1",
                "created_at": datetime.now(UTC) - timedelta(seconds=5),
            },
            {
                "id": "imo_2",
                "conversation_id": "imc_1",
                "message_id": "icm_2",
                "created_at": datetime.now(UTC),
            },
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/offline-messages/ack-batch",
                json={"message_ids": ["imo_1", "imo_2"]},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["acked"] == 2
        assert body["message_ids"] == ["imo_1", "imo_2"]
        assert body["conversations"] == ["imc_1"]
        update_sql, update_params = conn.calls[0]
        assert "UPDATE im_offline_message" in update_sql
        # 越权防护：只更新本人 + 本 workspace 的行
        assert "recipient_id = %s" in update_sql
        assert "workspace_id = %s" in update_sql
        assert update_params[0] == "usr_test"
        assert update_params[1] == "wsp_test"

    @pytest.mark.asyncio
    async def test_ack_batch_advances_acked_cursor_once_per_conversation(
        self, monkeypatch
    ):
        """同会话多条只推进一次游标，取 created_at 最大的那条。"""
        older = datetime.now(UTC) - timedelta(minutes=1)
        newer = datetime.now(UTC)
        rows = [
            {"id": "imo_1", "conversation_id": "imc_1", "message_id": "icm_1",
             "created_at": older},
            {"id": "imo_2", "conversation_id": "imc_1", "message_id": "icm_2",
             "created_at": newer},
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/offline-messages/ack-batch",
                json={"conversation_id": "imc_1"},
            )
        assert resp.status_code == 200
        cursors = _sql_of(conn.calls, "INSERT INTO im_delivery_cursor")
        assert len(cursors) == 1
        # 游标指向最新一条，pending_count 扣减本批条数
        assert cursors[0][1][3] == "icm_2"
        assert cursors[0][1][4] == 2
        assert "GREATEST(" in cursors[0][0]

    @pytest.mark.asyncio
    async def test_ack_batch_is_idempotent_when_already_acked(self, monkeypatch):
        """已确认的行因 acked_at IS NULL 条件不再匹配 → acked=0，不推进游标。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/offline-messages/ack-batch",
                json={"message_ids": ["imo_1"]},
            )
        assert resp.status_code == 200
        assert resp.json()["acked"] == 0
        assert "acked_at IS NULL" in conn.calls[0][0]
        assert _sql_of(conn.calls, "INSERT INTO im_delivery_cursor") == []

    @pytest.mark.asyncio
    async def test_ack_batch_up_to_adds_time_bound(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/offline-messages/ack-batch",
                json={
                    "conversation_id": "imc_1",
                    "up_to": "2026-01-01T00:00:00+00:00",
                },
            )
        assert resp.status_code == 200
        assert "created_at <= %s" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_ack_batch_422_without_selector(self, monkeypatch):
        """既无 message_ids 又无 conversation_id → 422 且不触达数据库。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/offline-messages/ack-batch", json={}
            )
        assert resp.status_code == 422
        assert conn.calls == []

    @pytest.mark.asyncio
    async def test_ack_batch_requires_write_capability(self, monkeypatch):
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=("im:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/offline-messages/ack-batch",
                json={"message_ids": ["imo_1"]},
            )
        assert resp.status_code == 403
        assert conn.calls == []


# ============================================================================
# 5. POST /api/v1/im/groups/{group_id}/transfer-ownership
# ============================================================================


class TestTransferGroupOwnership:
    """群主转让：owner 校验、成员校验、审计、workspace 隔离。"""

    @pytest.mark.asyncio
    async def test_transfer_ownership_success_writes_roles_and_audit(
        self, monkeypatch
    ):
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row()),                                   # 群
                _Result(row=_group_member_row(role="owner")),                # 调用者
                _Result(row=_group_member_row(user_id="usr_new", role="member")),
                _Result(),                                                   # UPDATE 新 owner
                _Result(),                                                   # UPDATE 原 owner
                _Result(row=_group_row(owner_id="usr_new")),                 # UPDATE im_group
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/transfer-ownership",
                json={"new_owner_id": "usr_new", "reason": "handover"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["new_owner_id"] == "usr_new"
        assert body["previous_owner_id"] == "usr_test"
        assert body["group"]["owner_id"] == "usr_new"
        # 新 owner 升级、原 owner 降为 admin
        role_updates = _sql_of(conn.calls, "UPDATE im_group_member SET role")
        assert len(role_updates) == 2
        assert "'owner'" in role_updates[0][0]
        assert role_updates[0][1] == ("img_1", "usr_new")
        assert "'admin'" in role_updates[1][0]
        assert role_updates[1][1] == ("img_1", "usr_test")
        # im_group.owner_id 同步
        assert _sql_of(conn.calls, "UPDATE im_group SET owner_id = %s")
        # 审计：1 条转让 + 2 条角色变更
        assert len(_sql_of(conn.calls, "INSERT INTO im_group_ownership_transfer")) == 1
        assert len(_sql_of(conn.calls, "INSERT INTO im_group_role_change")) == 2

    @pytest.mark.asyncio
    async def test_transfer_ownership_audit_records_reason_and_actor(
        self, monkeypatch
    ):
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row()),
                _Result(row=_group_member_row(role="owner")),
                _Result(row=_group_member_row(user_id="usr_new", role="admin")),
                _Result(),
                _Result(),
                _Result(row=_group_row(owner_id="usr_new")),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/transfer-ownership",
                json={"new_owner_id": "usr_new", "reason": "leaving team"},
            )
        assert resp.status_code == 200
        _, params = _sql_of(conn.calls, "INSERT INTO im_group_ownership_transfer")[0]
        assert params[1] == "wsp_test"     # workspace_id
        assert params[2] == "img_1"        # group_id
        assert params[3] == "usr_test"     # from_user_id
        assert params[4] == "usr_new"      # to_user_id
        assert params[5] == "usr_test"     # performed_by
        assert params[6] == "leaving team"
        # 角色变更审计保留原角色 admin → owner
        role_audits = _sql_of(conn.calls, "INSERT INTO im_group_role_change")
        assert role_audits[0][1][4] == "admin"
        assert role_audits[0][1][5] == "owner"

    @pytest.mark.asyncio
    async def test_transfer_ownership_403_for_non_owner(self, monkeypatch):
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row(owner_id="usr_owner")),
                _Result(row=_group_member_row(role="admin")),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/transfer-ownership",
                json={"new_owner_id": "usr_new"},
            )
        assert resp.status_code == 403
        # fail-closed：未执行任何写操作
        assert _sql_of(conn.calls, "UPDATE im_group_member SET role") == []

    @pytest.mark.asyncio
    async def test_transfer_ownership_404_when_target_not_member(self, monkeypatch):
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row()),
                _Result(row=_group_member_row(role="owner")),
                _Result(row=None),  # 目标不是成员
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/transfer-ownership",
                json={"new_owner_id": "usr_stranger"},
            )
        assert resp.status_code == 404
        assert _sql_of(conn.calls, "UPDATE im_group_member SET role") == []

    @pytest.mark.asyncio
    async def test_transfer_ownership_404_cross_workspace(self, monkeypatch):
        """跨 workspace 的群 → 404，不泄露存在性。"""
        conn = _RecordingConnection(
            results=[_Result(row=_group_row(workspace_id="wsp_other"))]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/transfer-ownership",
                json={"new_owner_id": "usr_new"},
            )
        assert resp.status_code == 404
        assert _sql_of(conn.calls, "UPDATE im_group") == []

    @pytest.mark.asyncio
    async def test_transfer_ownership_422_to_self(self, monkeypatch):
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/transfer-ownership",
                json={"new_owner_id": "usr_test"},
            )
        assert resp.status_code == 422
        assert conn.calls == []

    @pytest.mark.asyncio
    async def test_transfer_ownership_requires_write_capability(self, monkeypatch):
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=("im:read",)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/transfer-ownership",
                json={"new_owner_id": "usr_new"},
            )
        assert resp.status_code == 403
        assert conn.calls == []


# ============================================================================
# 6. PATCH /api/v1/im/groups/{group_id}/members/{user_id}/role
# ============================================================================


class TestUpdateGroupMemberRole:
    """成员角色管理：仅 owner；owner 角色不可经此接口变更；审计留痕。"""

    @pytest.mark.asyncio
    async def test_promote_member_to_admin_writes_audit(self, monkeypatch):
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row()),
                _Result(row=_group_member_row(role="owner")),
                _Result(row=_group_member_row(user_id="usr_m", role="member")),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1/members/usr_m/role",
                json={"role": "admin"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["changed"] is True
        assert body["role"] == "admin"
        assert body["previous_role"] == "member"
        update = _sql_of(conn.calls, "UPDATE im_group_member SET role = %s")[0]
        assert update[1] == ("admin", "img_1", "usr_m")
        audit = _sql_of(conn.calls, "INSERT INTO im_group_role_change")[0]
        assert audit[1][3] == "usr_m"
        assert audit[1][4] == "member"
        assert audit[1][5] == "admin"
        assert audit[1][6] == "usr_test"

    @pytest.mark.asyncio
    async def test_demote_admin_to_member(self, monkeypatch):
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row()),
                _Result(row=_group_member_row(role="owner")),
                _Result(row=_group_member_row(user_id="usr_m", role="admin")),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1/members/usr_m/role",
                json={"role": "member"},
            )
        assert resp.status_code == 200
        assert resp.json()["previous_role"] == "admin"
        assert _sql_of(conn.calls, "UPDATE im_group_member SET role = %s")[0][1][0] == "member"

    @pytest.mark.asyncio
    async def test_role_unchanged_is_noop_without_audit(self, monkeypatch):
        """目标已是该角色 → changed=false，不写库不写审计。"""
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row()),
                _Result(row=_group_member_row(role="owner")),
                _Result(row=_group_member_row(user_id="usr_m", role="admin")),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1/members/usr_m/role",
                json={"role": "admin"},
            )
        assert resp.status_code == 200
        assert resp.json()["changed"] is False
        assert _sql_of(conn.calls, "UPDATE im_group_member SET role = %s") == []
        assert _sql_of(conn.calls, "INSERT INTO im_group_role_change") == []

    @pytest.mark.asyncio
    async def test_role_change_403_for_admin_caller(self, monkeypatch):
        """admin 不能改他人角色（fail-closed，避免权限横向扩散）。"""
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row(owner_id="usr_owner")),
                _Result(row=_group_member_row(role="admin")),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1/members/usr_m/role",
                json={"role": "admin"},
            )
        assert resp.status_code == 403
        assert _sql_of(conn.calls, "UPDATE im_group_member SET role = %s") == []

    @pytest.mark.asyncio
    async def test_role_change_403_when_target_is_owner(self, monkeypatch):
        """owner 角色只能通过 transfer-ownership 变更。"""
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row()),
                _Result(row=_group_member_row(role="owner")),
                _Result(row=_group_member_row(user_id="usr_o", role="owner")),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1/members/usr_o/role",
                json={"role": "member"},
            )
        assert resp.status_code == 403
        assert _sql_of(conn.calls, "UPDATE im_group_member SET role = %s") == []

    @pytest.mark.asyncio
    async def test_role_change_404_when_target_not_member(self, monkeypatch):
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row()),
                _Result(row=_group_member_row(role="owner")),
                _Result(row=None),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1/members/usr_x/role",
                json={"role": "admin"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_role_change_422_for_self(self, monkeypatch):
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1/members/usr_test/role",
                json={"role": "member"},
            )
        assert resp.status_code == 422
        assert conn.calls == []

    @pytest.mark.asyncio
    async def test_role_change_422_for_invalid_role(self, monkeypatch):
        """role 只接受 admin/member；owner 会被 pydantic 拒绝。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1/members/usr_m/role",
                json={"role": "owner"},
            )
        assert resp.status_code == 422
        assert conn.calls == []

    @pytest.mark.asyncio
    async def test_role_change_404_cross_workspace(self, monkeypatch):
        conn = _RecordingConnection(
            results=[_Result(row=_group_row(workspace_id="wsp_other"))]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1/members/usr_m/role",
                json={"role": "admin"},
            )
        assert resp.status_code == 404


# ============================================================================
# 7. GET /api/v1/im/groups/{group_id}/audit
# ============================================================================


class TestListGroupAudit:
    """群治理审计查询：owner/admin 可看，member 403，跨 workspace 404。"""

    @pytest.mark.asyncio
    async def test_group_audit_returns_role_changes_and_transfers(self, monkeypatch):
        now = datetime.now(UTC)
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row()),
                _Result(row=_group_member_row(role="owner")),
                _Result(rows=[{
                    "id": "imgr_1", "target_user_id": "usr_m",
                    "old_role": "member", "new_role": "admin",
                    "changed_by": "usr_test", "changed_at": now,
                }]),
                _Result(rows=[{
                    "id": "imgt_1", "from_user_id": "usr_old",
                    "to_user_id": "usr_test", "performed_by": "usr_old",
                    "reason": "handover", "created_at": now,
                }]),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups/img_1/audit")
        assert resp.status_code == 200
        body = resp.json()
        assert body["group_id"] == "img_1"
        assert len(body["role_changes"]) == 1
        assert body["role_changes"][0]["new_role"] == "admin"
        assert len(body["ownership_transfers"]) == 1
        assert body["ownership_transfers"][0]["reason"] == "handover"
        # 两条查询都必须带 workspace_id 过滤
        for needle in ("FROM im_group_role_change", "FROM im_group_ownership_transfer"):
            query, params = _sql_of(conn.calls, needle)[0]
            assert "workspace_id = %s" in query
            assert params[1] == "wsp_test"

    @pytest.mark.asyncio
    async def test_group_audit_allows_admin(self, monkeypatch):
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row(owner_id="usr_owner")),
                _Result(row=_group_member_row(role="admin")),
                _Result(rows=[]),
                _Result(rows=[]),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups/img_1/audit")
        assert resp.status_code == 200
        assert resp.json()["role_changes"] == []

    @pytest.mark.asyncio
    async def test_group_audit_403_for_plain_member(self, monkeypatch):
        conn = _RecordingConnection(
            results=[
                _Result(row=_group_row(owner_id="usr_owner")),
                _Result(row=_group_member_row(role="member")),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups/img_1/audit")
        assert resp.status_code == 403
        assert _sql_of(conn.calls, "FROM im_group_role_change") == []

    @pytest.mark.asyncio
    async def test_group_audit_403_for_non_member(self, monkeypatch):
        conn = _RecordingConnection(
            results=[_Result(row=_group_row(owner_id="usr_owner")), _Result(row=None)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups/img_1/audit")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_group_audit_404_cross_workspace(self, monkeypatch):
        conn = _RecordingConnection(
            results=[_Result(row=_group_row(workspace_id="wsp_other"))]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups/img_1/audit")
        assert resp.status_code == 404


# ============================================================================
# 8. GET /api/v1/im/messages/{message_id}/history
# ============================================================================


class TestListMessageHistory:
    """消息撤回/编辑轨迹：成员可见、跨 workspace 404、payload 反序列化。"""

    @pytest.mark.asyncio
    async def test_message_history_returns_ordered_entries(self, monkeypatch):
        now = datetime.now(UTC)
        conn = _RecordingConnection(
            results=[
                _Result(row=_msg_row(content="edited", edited_at=now)),
                _Result(row={"?column?": 1}),  # 成员校验
                _Result(rows=[
                    {
                        "id": "imel_1", "action": "edit", "edited_by": "usr_test",
                        "edited_at": now,
                        "old_payload": {"content": "hello"},
                        "new_payload": {"content": "edited"},
                    },
                ]),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/messages/icm_1/history")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["message"]["id"] == "icm_1"
        assert body["message"]["edited_at"] is not None
        assert body["entries"][0]["action"] == "edit"
        assert body["entries"][0]["old_payload"]["content"] == "hello"
        # 审计按时间正序，便于前端按顺序渲染轨迹
        log_query = _sql_of(conn.calls, "FROM im_message_edit_log")[0][0]
        assert "ORDER BY edited_at ASC" in log_query

    @pytest.mark.asyncio
    async def test_message_history_parses_json_string_payloads(self, monkeypatch):
        now = datetime.now(UTC)
        conn = _RecordingConnection(
            results=[
                _Result(row=_msg_row()),
                _Result(row={"?column?": 1}),
                _Result(rows=[{
                    "id": "imel_1", "action": "retract", "edited_by": "usr_test",
                    "edited_at": now,
                    "old_payload": json.dumps({"content": "secret"}),
                    "new_payload": json.dumps({"content": msg.RETRACTED_CONTENT}),
                }]),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/messages/icm_1/history")
        assert resp.status_code == 200
        entry = resp.json()["entries"][0]
        assert entry["old_payload"] == {"content": "secret"}
        assert entry["new_payload"] == {"content": msg.RETRACTED_CONTENT}

    @pytest.mark.asyncio
    async def test_message_history_404_cross_workspace(self, monkeypatch):
        conn = _RecordingConnection(
            results=[_Result(row=_msg_row(conv_workspace_id="wsp_other"))]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/messages/icm_1/history")
        assert resp.status_code == 404
        assert _sql_of(conn.calls, "FROM im_message_edit_log") == []

    @pytest.mark.asyncio
    async def test_message_history_403_for_non_member(self, monkeypatch):
        conn = _RecordingConnection(
            results=[_Result(row=_msg_row()), _Result(row=None)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/messages/icm_1/history")
        assert resp.status_code == 403
        assert _sql_of(conn.calls, "FROM im_message_edit_log") == []

    @pytest.mark.asyncio
    async def test_message_history_404_when_missing(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/messages/icm_nope/history")
        assert resp.status_code == 404


# ============================================================================
# 9. 撤回/编辑同步未投递离线副本 + 广播
# ============================================================================


class TestMutationSyncsOfflineCopies:
    """撤回/编辑必须改写尚未投递的离线副本，否则离线成员上线读到原文。"""

    @pytest.mark.asyncio
    async def test_retract_rewrites_pending_offline_payload(self, monkeypatch):
        conn = _RecordingConnection(
            results=[
                _Result(row=_msg_row(content="secret")),
                _Result(),                                    # UPDATE 消息
                _Result(),                                    # INSERT 审计
                _Result(rows=[{"user_id": "usr_test"}, {"user_id": "usr_b"}]),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        recording = _RecordingManager()
        monkeypatch.setattr(msg, "manager", recording)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 200
        sync = _sql_of(conn.calls, "UPDATE im_offline_message")[0]
        assert "jsonb_set" in sync[0]
        # 只改尚未投递的副本
        assert "delivered_at IS NULL" in sync[0]
        assert sync[1][0] == msg.RETRACTED_CONTENT
        assert sync[1][1] == "message_retracted"
        assert sync[1][2] == "icm_1"
        # 广播撤回事件给全体成员
        assert len(recording.broadcasts) == 1
        conv_id, member_ids, payload = recording.broadcasts[0]
        assert conv_id == "imc_1"
        assert member_ids == ["usr_test", "usr_b"]
        assert payload["type"] == "message_retracted"
        assert payload["actor_id"] == "usr_test"

    @pytest.mark.asyncio
    async def test_edit_rewrites_pending_offline_payload_with_new_content(
        self, monkeypatch
    ):
        conn = _RecordingConnection(
            results=[
                _Result(row=_msg_row(content="old text")),
                _Result(row=_msg_row(content="new text")),   # UPDATE RETURNING
                _Result(),                                   # INSERT 审计
                _Result(rows=[{"user_id": "usr_test"}]),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        recording = _RecordingManager()
        monkeypatch.setattr(msg, "manager", recording)

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/messages/icm_1", json={"content": "new text"}
            )
        assert resp.status_code == 200
        sync = _sql_of(conn.calls, "UPDATE im_offline_message")[0]
        assert sync[1][0] == "new text"
        assert sync[1][1] == "message_edited"
        assert recording.broadcasts[0][2]["type"] == "message_edited"
        assert recording.broadcasts[0][2]["content"] == "new text"

    @pytest.mark.asyncio
    async def test_retract_sets_structured_retracted_at_column(self, monkeypatch):
        """撤回同时写结构化列 retracted_at，前端不必比对 content 哨兵值。"""
        conn = _RecordingConnection(
            results=[
                _Result(row=_msg_row(content="secret")),
                _Result(),
                _Result(),
                _Result(rows=[{"user_id": "usr_test"}]),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        monkeypatch.setattr(msg, "manager", _RecordingManager())

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 200
        update = _sql_of(conn.calls, "UPDATE im_conv_message SET content = %s")[0]
        assert "retracted_at = now()" in update[0]

    @pytest.mark.asyncio
    async def test_edit_409_when_already_retracted_by_column(self, monkeypatch):
        """即使 content 已被改写过，只要 retracted_at 非空就拒绝编辑。"""
        conn = _RecordingConnection(
            results=[_Result(row=_msg_row(content="anything",
                                          retracted_at=datetime.now(UTC)))]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        monkeypatch.setattr(msg, "manager", _RecordingManager())

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/messages/icm_1", json={"content": "sneaky"}
            )
        assert resp.status_code == 409
        assert _sql_of(conn.calls, "UPDATE im_conv_message SET content = %s") == []

    @pytest.mark.asyncio
    async def test_retract_409_when_already_retracted_by_column(self, monkeypatch):
        conn = _RecordingConnection(
            results=[_Result(row=_msg_row(retracted_at=datetime.now(UTC)))]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        monkeypatch.setattr(msg, "manager", _RecordingManager())

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 409
        assert _sql_of(conn.calls, "UPDATE im_offline_message") == []


# ============================================================================
# 10. Schema 幂等性：SCHEMA_STATEMENTS 与迁移文件保持一致
# ============================================================================


class TestSchemaStatements:
    """新表/新列必须是幂等 DDL，且与 081 迁移一致。"""

    def test_schema_contains_offline_delivery_objects(self):
        all_sql = "\n".join(msg.SCHEMA_STATEMENTS)
        assert "ALTER TABLE im_offline_message ADD COLUMN IF NOT EXISTS message_id" in all_sql
        assert "ALTER TABLE im_offline_message ADD COLUMN IF NOT EXISTS acked_at" in all_sql
        assert "uq_im_offline_msg_message_recipient" in all_sql
        assert "CREATE TABLE IF NOT EXISTS im_delivery_cursor" in all_sql
        assert "CREATE TABLE IF NOT EXISTS im_group_ownership_transfer" in all_sql
        assert "CREATE TABLE IF NOT EXISTS im_group_role_change" in all_sql
        assert "ALTER TABLE im_conv_message ADD COLUMN IF NOT EXISTS retracted_at" in all_sql
        assert "ALTER TABLE im_conv_message ADD COLUMN IF NOT EXISTS edited_at" in all_sql

    def test_all_new_ddl_is_idempotent(self):
        """所有 DDL 语句都必须带 IF NOT EXISTS，支持重复执行。"""
        for stmt in msg.SCHEMA_STATEMENTS:
            normalized = " ".join(stmt.split())
            if normalized.startswith("CREATE TABLE"):
                assert "IF NOT EXISTS" in normalized, normalized
            if normalized.startswith("CREATE INDEX") or normalized.startswith(
                "CREATE UNIQUE INDEX"
            ):
                assert "IF NOT EXISTS" in normalized, normalized
            if normalized.startswith("ALTER TABLE") and "ADD COLUMN" in normalized:
                assert "IF NOT EXISTS" in normalized, normalized

    def test_new_request_models_are_exported(self):
        for name in (
            "OfflineAckBatchRequest",
            "GroupOwnershipTransferRequest",
            "GroupMemberRoleRequest",
            "RETRACTED_CONTENT",
        ):
            assert name in msg.__all__
            assert hasattr(msg, name)


# ============================================================================
# 11. WS 连接时触发补投（端到端）
# ============================================================================


class TestWsBackfillOnConnect:
    """WS 握手成功后立即补投离线队列。"""

    def test_ws_connect_backfills_offline_queue(self, monkeypatch):
        conn = _RecordingConnection(results=[_Result(rows=[_offline_row()])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))
        monkeypatch.setattr(msg, "manager", msg.ConnectionManager())
        monkeypatch.setattr(msg, "decode_token_cached", _decode_ok)

        app = FastAPI()
        app.include_router(msg.router)
        client = TestClient(app)
        with client.websocket_connect("/api/v1/messaging/ws?token=valid") as websocket:
            assert websocket.receive_json()["type"] == "connected"
            backfilled = websocket.receive_json()
            assert backfilled["type"] == "message"
            assert backfilled["content"] == "offline hello"
            assert backfilled["backfilled"] is True
            assert backfilled["offline_id"] == "imo_1"
            websocket.send_json({"type": "ping"})
            assert websocket.receive_json() == {"type": "pong"}
        # 队列取数按 recipient + workspace 隔离
        select_params = conn.calls[0][1]
        assert select_params[0] == "usr_test"
        assert select_params[1] == "wsp_test"
