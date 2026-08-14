"""M7 IM 通道基础模块 (messaging) 测试。

覆盖（13 个测试）：
- 创建会话：direct 成功 / direct 成员数≠2 报错 / group 成功 (3)
- 列出会话（仅自己参与的） (1)
- 列出消息：非成员 403 / 成员成功 (2)
- 发送消息：成功 / 非成员 403 (2)
- 标记已读 (1)
- 退出会话：无剩余成员删会话 / 有剩余成员保留会话 (2)
- 跨 workspace 隔离 404 (1)
- ensure_messaging_schema 执行所有语句 (1)

所有测试使用 fake pool/connection（与 test_audit_enterprise.py 同风格），不依赖真实 DB。
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import messaging as msg


# ============================================================================
# 测试辅助：fake pool / connection / result（复用 test_audit_enterprise.py 模式）
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
    """会话行。"""
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
    """消息行。"""
    base = {
        "id": "imm_1",
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


# ============================================================================
# 1. 创建会话
# ============================================================================


class TestCreateConversation:
    """POST /conversations：direct 成功 / direct 成员数≠2 / group 成功。"""

    @pytest.mark.asyncio
    async def test_create_direct_conversation_success(self, monkeypatch):
        """direct 会话创建成功返回 201，创建者与 1 个其他成员各插入一条成员行。"""
        conv = _conv_row(type="direct")
        conn = _RecordingConnection(results=[_Result(row=conv)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations",
                json={"type": "direct", "member_user_ids": ["usr_other"]},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["type"] == "direct"
        assert body["workspace_id"] == "wsp_test"
        # 第 1 次 execute 为 INSERT INTO im_conversation RETURNING *
        assert "INSERT INTO im_conversation" in conn.calls[0][0]
        # 会话 ID 由服务端 new_id("imc") 生成后写入，成员行必须复用同一个 ID
        created_id = conn.calls[0][1][0]
        assert created_id.startswith("imc_")
        # 第 2、3 次为成员 INSERT（创建者 + 其他成员）
        member_inserts = [c for c in conn.calls if "INSERT INTO im_conversation_member" in c[0]]
        assert len(member_inserts) == 2
        # 创建者成员行参数包含 usr_test
        assert member_inserts[0][1] == (created_id, "usr_test")
        # 其他成员行参数包含 usr_other
        assert member_inserts[1][1] == (created_id, "usr_other")

    @pytest.mark.asyncio
    async def test_create_direct_conversation_wrong_member_count(self, monkeypatch):
        """direct 类型 member_user_ids 给 2 个其他成员（总 3 人）返回 422，不触达数据库。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations",
                json={"type": "direct", "member_user_ids": ["usr_a", "usr_b"]},
            )
        assert resp.status_code == 422
        assert len(conn.calls) == 0

    @pytest.mark.asyncio
    async def test_create_group_conversation_success(self, monkeypatch):
        """group 会话创建成功返回 201，支持 title 与多个其他成员。"""
        conv = _conv_row(type="group", title="Team")
        conn = _RecordingConnection(results=[_Result(row=conv)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations",
                json={
                    "type": "group",
                    "title": "Team",
                    "member_user_ids": ["usr_a", "usr_b"],
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["type"] == "group"
        assert body["title"] == "Team"
        # 创建者 + 2 个其他成员 = 3 条成员 INSERT
        member_inserts = [c for c in conn.calls if "INSERT INTO im_conversation_member" in c[0]]
        assert len(member_inserts) == 3

    @pytest.mark.asyncio
    async def test_create_direct_conversation_dedup_creator(self, monkeypatch):
        """member_user_ids 中包含创建者自身时去重，不重复插入创建者成员行。"""
        conv = _conv_row(type="direct")
        conn = _RecordingConnection(results=[_Result(row=conv)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations",
                # 创建者 usr_test + usr_other（列表里也放了 usr_test，应去重）
                json={"type": "direct", "member_user_ids": ["usr_test", "usr_other"]},
            )
        assert resp.status_code == 201
        # 去重后只有 2 条成员 INSERT（创建者 + usr_other）
        member_inserts = [c for c in conn.calls if "INSERT INTO im_conversation_member" in c[0]]
        assert len(member_inserts) == 2


# ============================================================================
# 2. 列出会话
# ============================================================================


class TestListConversations:
    """GET /conversations：仅返回当前用户参与的会话，分页。"""

    @pytest.mark.asyncio
    async def test_list_conversations_only_self_participated(self, monkeypatch):
        """列出会话 SQL 包含 user_id 与 workspace_id 过滤，回显分页参数。"""
        rows = [_conv_row(id="imc_1"), _conv_row(id="imc_2", type="group")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_test", workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/messaging/conversations?limit=10&offset=0"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["limit"] == 10
        assert body["offset"] == 0
        # SQL 必须同时按 user_id 和 workspace_id 过滤
        query, params = conn.calls[0]
        assert "m.user_id = %s" in query
        assert "c.workspace_id = %s" in query
        assert params[0] == "usr_test"
        assert params[1] == "wsp_test"


# ============================================================================
# 3. 列出消息
# ============================================================================


class TestListMessages:
    """GET /conversations/{id}/messages：非成员 403 / 成员成功。"""

    @pytest.mark.asyncio
    async def test_list_messages_non_member_403(self, monkeypatch):
        """非会话成员列出消息返回 403。"""
        conv = _conv_row()
        # 第 1 次：_get_owned_conversation 返回会话；第 2 次：_assert_member 返回 None
        conn = _RecordingConnection(
            results=[_Result(row=conv), _Result(row=None)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_outsider"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/messaging/conversations/imc_1/messages")
        assert resp.status_code == 403
        # 校验 _assert_member SQL 包含 user_id 过滤
        assert "im_conversation_member" in conn.calls[1][0]
        assert conn.calls[1][1] == ("imc_1", "usr_outsider")

    @pytest.mark.asyncio
    async def test_list_messages_member_success(self, monkeypatch):
        """会话成员列出消息成功，按 created_at ASC 排序。"""
        conv = _conv_row()
        msgs = [
            _msg_row(id="imm_1", content="first"),
            _msg_row(id="imm_2", content="second"),
        ]
        # 1) 会话查询 2) 成员校验 3) 消息查询
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(rows=msgs),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/messaging/conversations/imc_1/messages")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["items"][0]["content"] == "first"
        assert body["items"][1]["content"] == "second"
        # 消息查询 SQL 按 created_at ASC
        msg_query = conn.calls[2][0]
        assert "ORDER BY created_at ASC" in msg_query


# ============================================================================
# 4. 发送消息
# ============================================================================


class TestSendMessage:
    """POST /conversations/{id}/messages：成功 / 非成员 403。"""

    @pytest.mark.asyncio
    async def test_send_message_success(self, monkeypatch):
        """会话成员发送消息成功返回 201。"""
        conv = _conv_row()
        sent = _msg_row(content="hi", sender_id="usr_test")
        # 1) 会话查询 2) 成员校验 3) INSERT RETURNING
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(row=sent),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations/imc_1/messages",
                json={"content": "hi"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["content"] == "hi"
        assert body["sender_id"] == "usr_test"
        assert body["conversation_id"] == "imc_1"
        # INSERT 包含 conversation_id/sender_id/content
        insert_query, insert_params = conn.calls[2]
        assert "INSERT INTO im_conv_message" in insert_query
        assert insert_params[2] == "usr_test"  # sender_id
        assert insert_params[3] == "hi"  # content

    @pytest.mark.asyncio
    async def test_send_message_non_member_403(self, monkeypatch):
        """非会话成员发送消息返回 403，不触达 INSERT。"""
        conv = _conv_row()
        conn = _RecordingConnection(
            results=[_Result(row=conv), _Result(row=None)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_outsider"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/messaging/conversations/imc_1/messages",
                json={"content": "hi"},
            )
        assert resp.status_code == 403
        # 不应有 INSERT INTO im_conv_message
        inserts = [c for c in conn.calls if "INSERT INTO im_conv_message" in c[0]]
        assert len(inserts) == 0


# ============================================================================
# 5. 标记已读
# ============================================================================


class TestMarkRead:
    """POST /conversations/{id}/read：更新 last_read_at。"""

    @pytest.mark.asyncio
    async def test_mark_read_success(self, monkeypatch):
        """会话成员标记已读成功，UPDATE last_read_at。"""
        conv = _conv_row()
        now = datetime.now(UTC)
        read_row = {
            "conversation_id": "imc_1",
            "user_id": "usr_test",
            "last_read_at": now,
        }
        # 1) 会话查询 2) 成员校验 3) UPDATE RETURNING
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
        assert body["conversation_id"] == "imc_1"
        assert body["user_id"] == "usr_test"
        assert body["last_read_at"] is not None
        # UPDATE SQL 包含 last_read_at = now()
        update_query, update_params = conn.calls[2]
        assert "UPDATE im_conversation_member" in update_query
        assert "last_read_at = now()" in update_query
        assert update_params == ("imc_1", "usr_test")


# ============================================================================
# 6. 退出会话
# ============================================================================


class TestLeaveConversation:
    """DELETE /conversations/{id}：无剩余成员删会话 / 有剩余成员保留。"""

    @pytest.mark.asyncio
    async def test_leave_conversation_deletes_when_no_remaining_members(self, monkeypatch):
        """退出后无剩余成员时删除会话本身。"""
        conv = _conv_row()
        # 1) 会话查询 2) 成员校验 3) DELETE member 4) SELECT 剩余成员(None) 5) DELETE 会话
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(),
                _Result(row=None),
                _Result(),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/messaging/conversations/imc_1")
        assert resp.status_code == 204
        # 校验 DELETE member 与 DELETE conversation 均被调用
        delete_member = [c for c in conn.calls if "DELETE FROM im_conversation_member" in c[0]]
        assert len(delete_member) == 1
        assert delete_member[0][1] == ("imc_1", "usr_test")
        # 注意：必须用 "im_conversation WHERE" 精确匹配，否则会同时命中
        # "DELETE FROM im_conversation_member ..."
        delete_conv = [c for c in conn.calls if "DELETE FROM im_conversation WHERE" in c[0]]
        assert len(delete_conv) == 1
        assert delete_conv[0][1] == ("imc_1",)

    @pytest.mark.asyncio
    async def test_leave_conversation_keeps_when_remaining_members(self, monkeypatch):
        """退出后仍有剩余成员时不删除会话。"""
        conv = _conv_row(type="group")
        # 1) 会话查询 2) 成员校验 3) DELETE member 4) SELECT 剩余成员(有) → 不删会话
        conn = _RecordingConnection(
            results=[
                _Result(row=conv),
                _Result(row={"?column?": 1}),
                _Result(),
                _Result(row={"?column?": 1}),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/messaging/conversations/imc_1")
        assert resp.status_code == 204
        # DELETE member 被调用
        delete_member = [c for c in conn.calls if "DELETE FROM im_conversation_member" in c[0]]
        assert len(delete_member) == 1
        # DELETE FROM im_conversation 不应被调用（仅 DELETE FROM im_conversation_member）
        delete_conv = [c for c in conn.calls if "DELETE FROM im_conversation " in c[0]]
        assert len(delete_conv) == 0


# ============================================================================
# 7. 跨 workspace 隔离
# ============================================================================


class TestCrossWorkspaceIsolation:
    """跨 workspace：会话属于其他 workspace 时返回 404。"""

    @pytest.mark.asyncio
    async def test_cross_workspace_returns_404(self, monkeypatch):
        """会话属于其他 workspace 时返回 404，不泄露存在性。"""
        # 会话存在但 workspace_id 不匹配
        conv = _conv_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=conv)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/messaging/conversations/imc_1/messages")
        assert resp.status_code == 404
        # 仅触达 _get_owned_conversation 的 SELECT，不应触达成员校验或消息查询
        assert len(conn.calls) == 1
        assert "SELECT * FROM im_conversation" in conn.calls[0][0]


# ============================================================================
# 8. ensure_messaging_schema
# ============================================================================


class TestEnsureSchema:
    """ensure_messaging_schema 执行所有 SCHEMA_STATEMENTS。"""

    @pytest.mark.asyncio
    async def test_ensure_messaging_schema_executes_all_statements(self):
        """ensure_messaging_schema 逐条执行 SCHEMA_STATEMENTS 中的所有语句。"""
        conn = _RecordingConnection()
        await msg.ensure_messaging_schema(conn)
        # 调用次数应等于 SCHEMA_STATEMENTS 长度
        assert len(conn.calls) == len(msg.SCHEMA_STATEMENTS)
        # 每条调用的 SQL 应与 SCHEMA_STATEMENTS 中的对应语句一致（顺序一致）
        for idx, statement in enumerate(msg.SCHEMA_STATEMENTS):
            assert conn.calls[idx][0] == statement
        # 校验包含关键建表与索引语句
        all_sql = "\n".join(c[0] for c in conn.calls)
        assert "CREATE TABLE IF NOT EXISTS im_conversation" in all_sql
        assert "CREATE TABLE IF NOT EXISTS im_conversation_member" in all_sql
        assert "CREATE TABLE IF NOT EXISTS im_conv_message" in all_sql
        assert "idx_im_conv_workspace" in all_sql
        assert "idx_im_conv_member_user" in all_sql
        assert "idx_im_conv_message_conv_created" in all_sql
