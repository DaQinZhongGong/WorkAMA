"""P3 IM 通道增强模块测试：离线消息 + 群组管理 + 消息撤回/编辑。

覆盖（≥50 个测试）：
- 离线消息：拉取/分页/游标/过滤/确认/删除/workspace 隔离/鉴权 (16+)
- 群组管理：创建/列表/详情/更新/解散/成员邀请/移除/退出/角色校验 (24+)
- 消息撤回：时间窗口内/外、已撤回、跨用户、跨 workspace (8+)
- 消息编辑：时间窗口内/外、撤回后不可编辑、审计日志 (8+)
- 鉴权：401 未登录、403 跨 workspace / 角色不足 / capability 缺失

所有测试使用 fake pool/connection（参考 test_push_notification.py 的
_RecordingConnection/_Result 实现），不依赖真实 DB。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from workama_platform.core import Actor, get_actor
from workama_platform.modules import messaging as msg


# ============================================================================
# 测试辅助：fake pool / connection / result
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
    capabilities=None,
) -> Actor:
    """构造测试 Actor；默认带 im:* capability。"""
    if capabilities is None:
        capabilities = ("im:*",)
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email=email,
        display_name="User",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _offline_msg_row(**overrides) -> dict[str, Any]:
    """离线消息行。"""
    base = {
        "id": "iom_1",
        "workspace_id": "wsp_test",
        "conversation_id": "imc_1",
        "sender_id": "usr_other",
        "recipient_id": "usr_test",
        "payload": {"content": "hello"},
        "created_at": datetime.now(UTC),
        "delivered_at": None,
    }
    base.update(overrides)
    return base


def _group_row(**overrides) -> dict[str, Any]:
    """群组行。"""
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
    """群成员行。"""
    base = {
        "group_id": "img_1",
        "user_id": "usr_test",
        "role": "owner",
        "joined_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _msg_row(**overrides) -> dict[str, Any]:
    """会话消息行（含联表字段 conv_workspace_id）。"""
    base = {
        "id": "icm_1",
        "conversation_id": "imc_1",
        "sender_id": "usr_test",
        "content": "hello",
        "created_at": datetime.now(UTC),
        "delivered_at": None,
        "conv_workspace_id": "wsp_test",
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    """构造测试 app，挂载 im_router；可注入 actor 覆盖 get_actor。"""
    app = FastAPI()
    app.include_router(msg.im_router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 离线消息：拉取
# ============================================================================


class TestListOfflineMessages:
    """GET /api/v1/im/offline-messages：拉取/过滤/分页/鉴权。"""

    @pytest.mark.asyncio
    async def test_list_offline_messages_basic_success(self, monkeypatch):
        """基本拉取返回当前用户的离线消息列表。"""
        rows = [_offline_msg_row(id="iom_1"), _offline_msg_row(id="iom_2")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/offline-messages")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        # SQL 必须按 recipient_id + workspace_id 过滤
        query, params = conn.calls[0]
        assert "recipient_id = %s" in query
        assert "workspace_id = %s" in query
        assert params[0] == "usr_test"
        assert params[1] == "wsp_test"

    @pytest.mark.asyncio
    async def test_list_offline_messages_filter_by_conversation_id(self, monkeypatch):
        """按 conversation_id 过滤。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/im/offline-messages?conversation_id=imc_42"
            )
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "conversation_id = %s" in query
        assert "imc_42" in params

    @pytest.mark.asyncio
    async def test_list_offline_messages_filter_by_since(self, monkeypatch):
        """按 since 时间过滤。"""
        conn = _RecordingConnection(results=[_Result(rows=[])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/im/offline-messages?since=2026-01-01T00:00:00Z"
            )
        assert resp.status_code == 200
        query, params = conn.calls[0]
        assert "created_at >= %s" in query
        # since 被解析为 datetime 对象
        assert any(isinstance(p, datetime) for p in params)

    @pytest.mark.asyncio
    async def test_list_offline_messages_pagination_with_cursor(self, monkeypatch):
        """使用 cursor 分页：返回 has_more=True 与 next_cursor。"""
        # 多取 1 条 → 返回 3 条（limit=2 → fetch 3）→ has_more=True
        rows = [
            _offline_msg_row(id="iom_1"),
            _offline_msg_row(id="iom_2"),
            _offline_msg_row(id="iom_3"),
        ]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/im/offline-messages?limit=2&cursor=2026-01-01T00:00:00Z"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2  # 截断到 limit
        assert body["has_more"] is True
        assert body["next_cursor"] is not None
        query, params = conn.calls[0]
        assert "created_at < %s" in query

    @pytest.mark.asyncio
    async def test_list_offline_messages_has_more_false_when_no_extra(self, monkeypatch):
        """返回条数 ≤ limit 时 has_more=False。"""
        rows = [_offline_msg_row(id="iom_1")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/offline-messages?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["has_more"] is False
        assert body["next_cursor"] is None

    @pytest.mark.asyncio
    async def test_list_offline_messages_payload_deserialized(self, monkeypatch):
        """payload 字段为 JSON 字符串时被反序列化为 dict。"""
        row = _offline_msg_row(payload='{"content":"hi"}')
        conn = _RecordingConnection(results=[_Result(rows=[row])])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/offline-messages")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"][0]["payload"] == {"content": "hi"}

    @pytest.mark.asyncio
    async def test_list_offline_messages_401_without_auth(self):
        """未登录 → 401（无 actor override）。"""
        app = _app()  # 不注入 actor
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/offline-messages")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_list_offline_messages_403_missing_capability(self, monkeypatch):
        """actor 无 im:read capability → 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=()))  # 无任何 capability
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/offline-messages")
        assert resp.status_code == 403
        # 不应触达 DB
        assert len(conn.calls) == 0


# ============================================================================
# 2. 离线消息：确认已读（ack）
# ============================================================================


class TestAckOfflineMessage:
    """POST /api/v1/im/offline-messages/{id}/ack：幂等确认。"""

    @pytest.mark.asyncio
    async def test_ack_offline_message_success(self, monkeypatch):
        """首次确认 → acked=True。"""
        row = _offline_msg_row(delivered_at=None)
        # 1) SELECT 2) UPDATE RETURNING（命中 → updated row）
        conn = _RecordingConnection(
            results=[_Result(row=row), _Result(row={"id": "iom_1"})]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/offline-messages/iom_1/ack")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "iom_1"
        assert body["acked"] is True
        # UPDATE 必须 WHERE delivered_at IS NULL（幂等保护）
        update_q, update_params = conn.calls[1]
        assert "UPDATE im_offline_message SET delivered_at = now()" in update_q
        assert "delivered_at IS NULL" in update_q
        assert update_params == ("iom_1",)

    @pytest.mark.asyncio
    async def test_ack_offline_message_idempotent_when_already_acked(self, monkeypatch):
        """已确认后再次 ack → acked=False（幂等）。"""
        row = _offline_msg_row(delivered_at=datetime.now(UTC))
        # 1) SELECT 2) UPDATE RETURNING（不命中 → None）
        conn = _RecordingConnection(results=[_Result(row=row), _Result(row=None)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/offline-messages/iom_1/ack")
        assert resp.status_code == 200
        assert resp.json()["acked"] is False

    @pytest.mark.asyncio
    async def test_ack_offline_message_404_when_missing(self, monkeypatch):
        """消息不存在 → 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/offline-messages/iom_missing/ack")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_ack_offline_message_404_cross_workspace(self, monkeypatch):
        """跨 workspace 确认 → 404（不泄露存在性）。"""
        row = _offline_msg_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/offline-messages/iom_1/ack")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_ack_offline_message_403_other_user(self, monkeypatch):
        """确认他人的离线消息 → 403。"""
        row = _offline_msg_row(recipient_id="usr_other")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_self"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/offline-messages/iom_1/ack")
        assert resp.status_code == 403


# ============================================================================
# 3. 离线消息：删除
# ============================================================================


class TestDeleteOfflineMessage:
    """DELETE /api/v1/im/offline-messages/{id}：仅本人。"""

    @pytest.mark.asyncio
    async def test_delete_offline_message_success(self, monkeypatch):
        """删除自己的离线消息 → 204。"""
        row = _offline_msg_row()
        conn = _RecordingConnection(results=[_Result(row=row), _Result()])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/offline-messages/iom_1")
        assert resp.status_code == 204
        delete_q, delete_params = conn.calls[1]
        assert "DELETE FROM im_offline_message" in delete_q
        assert delete_params == ("iom_1",)

    @pytest.mark.asyncio
    async def test_delete_offline_message_404_when_missing(self, monkeypatch):
        """删除不存在的消息 → 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/offline-messages/iom_x")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_offline_message_403_other_user(self, monkeypatch):
        """删除他人的离线消息 → 403。"""
        row = _offline_msg_row(recipient_id="usr_other")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_self"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/offline-messages/iom_1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_offline_message_404_cross_workspace(self, monkeypatch):
        """跨 workspace 删除 → 404。"""
        row = _offline_msg_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=row)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/offline-messages/iom_1")
        assert resp.status_code == 404


# ============================================================================
# 4. 群组：创建 / 列表 / 详情
# ============================================================================


class TestGroupCreate:
    """POST /api/v1/im/groups：创建群组。"""

    @pytest.mark.asyncio
    async def test_create_group_success(self, monkeypatch):
        """创建群组返回 201，创建者作为 owner 加入。"""
        group = _group_row()
        conn = _RecordingConnection(results=[_Result(row=group)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups",
                json={"name": "Group A", "member_user_ids": ["usr_b", "usr_c"]},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Group A"
        assert body["owner_id"] == "usr_test"
        # 1) INSERT im_group 2) INSERT owner member 3) INSERT usr_b 4) INSERT usr_c
        member_inserts = [
            c for c in conn.calls if "INSERT INTO im_group_member" in c[0]
        ]
        assert len(member_inserts) == 3
        # 第 1 个成员 INSERT 应为 owner（role 作为 SQL 字面量 'owner'，params 为 2-tuple）
        owner_q, owner_p = member_inserts[0]
        assert "'owner'" in owner_q
        assert owner_p[1] == "usr_test"
        # 其他 2 个为 member（role 作为 SQL 字面量 'member'）
        assert "'member'" in member_inserts[1][0]
        assert member_inserts[1][1][1] == "usr_b"
        assert "'member'" in member_inserts[2][0]
        assert member_inserts[2][1][1] == "usr_c"

    @pytest.mark.asyncio
    async def test_create_group_dedup_creator_in_member_list(self, monkeypatch):
        """member_user_ids 中含创建者时去重，不重复插入 owner 行。"""
        group = _group_row()
        conn = _RecordingConnection(results=[_Result(row=group)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups",
                json={
                    "name": "Group A",
                    "member_user_ids": ["usr_test", "usr_b"],
                },
            )
        assert resp.status_code == 201
        member_inserts = [
            c for c in conn.calls if "INSERT INTO im_group_member" in c[0]
        ]
        # 去重后只有 owner + usr_b
        assert len(member_inserts) == 2

    @pytest.mark.asyncio
    async def test_create_group_403_missing_capability(self, monkeypatch):
        """无 im:write capability → 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=()))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups",
                json={"name": "Group A", "member_user_ids": []},
            )
        assert resp.status_code == 403
        assert len(conn.calls) == 0


class TestGroupList:
    """GET /api/v1/im/groups：列出我的群组。"""

    @pytest.mark.asyncio
    async def test_list_my_groups_success(self, monkeypatch):
        """列出当前用户参与的群组，分页。"""
        rows = [_group_row(id="img_1"), _group_row(id="img_2", name="Group B")]
        conn = _RecordingConnection(results=[_Result(rows=rows)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups?limit=10&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["limit"] == 10
        # SQL 必须 JOIN im_group_member + workspace 过滤
        query, params = conn.calls[0]
        assert "im_group_member" in query
        assert "gm.user_id = %s" in query
        assert "g.workspace_id = %s" in query


class TestGroupDetail:
    """GET /api/v1/im/groups/{group_id}：群详情。"""

    @pytest.mark.asyncio
    async def test_get_group_detail_success(self, monkeypatch):
        """群成员查看详情成功。"""
        group = _group_row()
        member = _group_member_row()
        # 1) SELECT group 2) SELECT member
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result(row=member)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups/img_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "img_1"
        assert body["owner_id"] == "usr_test"

    @pytest.mark.asyncio
    async def test_get_group_detail_404_cross_workspace(self, monkeypatch):
        """跨 workspace 查询 → 404。"""
        group = _group_row(workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=group)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups/img_1")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_group_detail_403_non_member(self, monkeypatch):
        """非群成员查看详情 → 403。"""
        group = _group_row()
        # 1) SELECT group 2) SELECT member（None）
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result(row=None)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_outsider"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups/img_1")
        assert resp.status_code == 403


# ============================================================================
# 5. 群组：更新 / 解散
# ============================================================================


class TestGroupUpdate:
    """PATCH /api/v1/im/groups/{group_id}：更新群信息（owner/admin）。"""

    @pytest.mark.asyncio
    async def test_update_group_name_success(self, monkeypatch):
        """owner 更新群名成功。"""
        group = _group_row()
        member = _group_member_row(role="owner")
        updated = _group_row(name="New Name")
        conn = _RecordingConnection(
            results=[
                _Result(row=group),
                _Result(row=member),
                _Result(row=updated),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1",
                json={"name": "New Name"},
            )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"
        # UPDATE 必须包含 updated_at = now()
        update_q, _ = conn.calls[2]
        assert "UPDATE im_group" in update_q
        assert "updated_at = now()" in update_q

    @pytest.mark.asyncio
    async def test_update_group_announcement_success(self, monkeypatch):
        """admin 更新群公告成功。"""
        group = _group_row()
        member = _group_member_row(role="admin", user_id="usr_admin")
        updated = _group_row(announcement="Hello everyone")
        conn = _RecordingConnection(
            results=[
                _Result(row=group),
                _Result(row=member),
                _Result(row=updated),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1",
                json={"announcement": "Hello everyone"},
            )
        assert resp.status_code == 200
        assert resp.json()["announcement"] == "Hello everyone"

    @pytest.mark.asyncio
    async def test_update_group_422_no_fields(self, monkeypatch):
        """未提供任何字段 → 422。"""
        group = _group_row()
        member = _group_member_row(role="owner")
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result(row=member)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1",
                json={},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_update_group_403_member_role(self, monkeypatch):
        """member 角色不能更新 → 403。"""
        group = _group_row()
        member = _group_member_row(role="member", user_id="usr_mem")
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result(row=member)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_mem"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/groups/img_1",
                json={"name": "X"},
            )
        assert resp.status_code == 403


class TestGroupDissolve:
    """DELETE /api/v1/im/groups/{group_id}：解散群组（仅 owner）。"""

    @pytest.mark.asyncio
    async def test_dissolve_group_success_by_owner(self, monkeypatch):
        """owner 解散群组 → 204。"""
        group = _group_row(owner_id="usr_owner")
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result()]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_owner"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/groups/img_1")
        assert resp.status_code == 204
        delete_q, delete_params = conn.calls[1]
        assert "DELETE FROM im_group" in delete_q
        assert delete_params == ("img_1",)

    @pytest.mark.asyncio
    async def test_dissolve_group_403_admin(self, monkeypatch):
        """admin 不能解散 → 403。"""
        group = _group_row(owner_id="usr_owner")
        conn = _RecordingConnection(results=[_Result(row=group)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/groups/img_1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_dissolve_group_403_member(self, monkeypatch):
        """member 不能解散 → 403。"""
        group = _group_row(owner_id="usr_owner")
        conn = _RecordingConnection(results=[_Result(row=group)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_mem"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/groups/img_1")
        assert resp.status_code == 403


# ============================================================================
# 6. 群组：成员邀请 / 移除 / 退出
# ============================================================================


class TestGroupInviteMembers:
    """POST /api/v1/im/groups/{group_id}/members：邀请成员（owner/admin）。"""

    @pytest.mark.asyncio
    async def test_invite_members_success(self, monkeypatch):
        """owner 邀请新成员成功。"""
        group = _group_row()
        admin = _group_member_row(role="owner")
        # 1) group 2) caller member 3) target member None 4) INSERT
        conn = _RecordingConnection(
            results=[
                _Result(row=group),
                _Result(row=admin),
                _Result(row=None),
                _Result(),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/members",
                json={"user_ids": ["usr_new"], "role": "member"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["inserted"] == ["usr_new"]
        assert body["skipped"] == []
        # INSERT 调用必须存在
        inserts = [c for c in conn.calls if "INSERT INTO im_group_member" in c[0]]
        assert len(inserts) == 1
        assert inserts[0][1] == ("img_1", "usr_new", "member")

    @pytest.mark.asyncio
    async def test_invite_members_skip_existing(self, monkeypatch):
        """已存在的成员跳过（幂等）。"""
        group = _group_row()
        admin = _group_member_row(role="owner")
        existing_member = _group_member_row(user_id="usr_existing", role="member")
        conn = _RecordingConnection(
            results=[
                _Result(row=group),
                _Result(row=admin),
                _Result(row=existing_member),  # 已存在
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/members",
                json={"user_ids": ["usr_existing"], "role": "member"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["inserted"] == []
        assert body["skipped"] == ["usr_existing"]
        # 不应有 INSERT
        inserts = [c for c in conn.calls if "INSERT INTO im_group_member" in c[0]]
        assert len(inserts) == 0

    @pytest.mark.asyncio
    async def test_invite_members_403_member_role(self, monkeypatch):
        """member 角色不能邀请 → 403。"""
        group = _group_row()
        caller = _group_member_row(role="member", user_id="usr_mem")
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result(row=caller)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_mem"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/members",
                json={"user_ids": ["usr_new"], "role": "member"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_invite_members_dedup_user_ids(self, monkeypatch):
        """user_ids 内重复 ID 去重，只 INSERT 一次。"""
        group = _group_row()
        admin = _group_member_row(role="owner")
        conn = _RecordingConnection(
            results=[
                _Result(row=group),
                _Result(row=admin),
                _Result(row=None),
                _Result(),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/members",
                json={"user_ids": ["usr_new", "usr_new"], "role": "admin"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["inserted"] == ["usr_new"]
        # INSERT 应只调用一次
        inserts = [c for c in conn.calls if "INSERT INTO im_group_member" in c[0]]
        assert len(inserts) == 1
        assert inserts[0][1] == ("img_1", "usr_new", "admin")


class TestGroupRemoveMember:
    """DELETE /api/v1/im/groups/{group_id}/members/{user_id}：移除成员。"""

    @pytest.mark.asyncio
    async def test_remove_member_success(self, monkeypatch):
        """owner 移除 member 成功。"""
        group = _group_row()
        admin = _group_member_row(role="owner")
        target = _group_member_row(user_id="usr_target", role="member")
        conn = _RecordingConnection(
            results=[
                _Result(row=group),
                _Result(row=admin),
                _Result(row=target),
                _Result(),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/groups/img_1/members/usr_target")
        assert resp.status_code == 204
        delete_q, delete_params = conn.calls[3]
        assert "DELETE FROM im_group_member" in delete_q
        assert delete_params == ("img_1", "usr_target")

    @pytest.mark.asyncio
    async def test_remove_member_422_remove_self(self, monkeypatch):
        """移除自己 → 422。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_self"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/groups/img_1/members/usr_self")
        assert resp.status_code == 422
        assert len(conn.calls) == 0  # 不触达 DB

    @pytest.mark.asyncio
    async def test_remove_member_403_remove_owner(self, monkeypatch):
        """不能移除 owner → 403。"""
        group = _group_row()
        admin = _group_member_row(role="admin", user_id="usr_admin")
        target = _group_member_row(user_id="usr_owner", role="owner")
        conn = _RecordingConnection(
            results=[
                _Result(row=group),
                _Result(row=admin),
                _Result(row=target),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/groups/img_1/members/usr_owner")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_remove_member_404_not_in_group(self, monkeypatch):
        """被移除者不在群中 → 404。"""
        group = _group_row()
        admin = _group_member_row(role="owner")
        conn = _RecordingConnection(
            results=[
                _Result(row=group),
                _Result(row=admin),
                _Result(row=None),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/groups/img_1/members/usr_ghost")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_remove_member_403_member_role(self, monkeypatch):
        """member 角色不能移除他人 → 403。"""
        group = _group_row()
        caller = _group_member_row(role="member", user_id="usr_mem")
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result(row=caller)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_mem"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/groups/img_1/members/usr_target")
        assert resp.status_code == 403


class TestGroupLeave:
    """POST /api/v1/im/groups/{group_id}/leave：主动退出。"""

    @pytest.mark.asyncio
    async def test_leave_group_success(self, monkeypatch):
        """member 主动退出成功。"""
        group = _group_row()
        caller = _group_member_row(role="member", user_id="usr_mem")
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result(row=caller), _Result()]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_mem"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/groups/img_1/leave")
        assert resp.status_code == 204
        delete_q, delete_params = conn.calls[2]
        assert "DELETE FROM im_group_member" in delete_q
        assert delete_params == ("img_1", "usr_mem")

    @pytest.mark.asyncio
    async def test_leave_group_403_owner_cannot_leave(self, monkeypatch):
        """owner 不能退出 → 403（应使用解散）。"""
        group = _group_row(owner_id="usr_owner")
        caller = _group_member_row(role="owner", user_id="usr_owner")
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result(row=caller)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_owner"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/groups/img_1/leave")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_leave_group_403_non_member(self, monkeypatch):
        """非成员退出 → 403。"""
        group = _group_row()
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result(row=None)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_outsider"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/groups/img_1/leave")
        assert resp.status_code == 403


# ============================================================================
# 7. 群组：列出成员
# ============================================================================


class TestGroupListMembers:
    """GET /api/v1/im/groups/{group_id}/members：列出群成员。"""

    @pytest.mark.asyncio
    async def test_list_group_members_success(self, monkeypatch):
        """列出群成员，按 role 排序（owner → admin → member）。"""
        group = _group_row()
        caller = _group_member_row(role="member", user_id="usr_test")
        rows = [
            {"user_id": "usr_owner", "role": "owner", "joined_at": datetime.now(UTC)},
            {"user_id": "usr_admin", "role": "admin", "joined_at": datetime.now(UTC)},
            {"user_id": "usr_test", "role": "member", "joined_at": datetime.now(UTC)},
        ]
        conn = _RecordingConnection(
            results=[
                _Result(row=group),
                _Result(row=caller),
                _Result(rows=rows),
            ]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups/img_1/members?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 3
        # SQL 必须包含 role 排序 CASE
        query, _ = conn.calls[2]
        assert "CASE role" in query
        # 响应包含 online 字段
        assert "online" in body["members"][0]

    @pytest.mark.asyncio
    async def test_list_group_members_403_non_member(self, monkeypatch):
        """非成员查询 → 403。"""
        group = _group_row()
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result(row=None)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_outsider"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups/img_1/members")
        assert resp.status_code == 403


# ============================================================================
# 8. 消息撤回
# ============================================================================


class TestRetractMessage:
    """POST /api/v1/im/messages/{message_id}/retract：撤回（5 分钟内）。"""

    @pytest.mark.asyncio
    async def test_retract_message_success_within_window(self, monkeypatch):
        """时间窗口内撤回成功，写审计日志。"""
        recent = _msg_row(
            content="hi",
            created_at=datetime.now(UTC),
            sender_id="usr_test",
        )
        conn = _RecordingConnection(
            results=[_Result(row=recent), _Result(), _Result()]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 200
        body = resp.json()
        assert body["retracted"] is True
        # 1) UPDATE content + retracted_at 2) INSERT im_message_edit_log
        update_q, update_params = conn.calls[1]
        assert "UPDATE im_conv_message SET content = %s" in update_q
        # 结构化撤回状态列必须同步写入，供客户端与历史查询使用
        assert "retracted_at = now()" in update_q
        assert update_params == (msg.RETRACTED_CONTENT, "icm_1")
        audit_q, audit_params = conn.calls[2]
        assert "INSERT INTO im_message_edit_log" in audit_q
        assert audit_params[1] == "icm_1"  # message_id
        assert audit_params[2] == "wsp_test"  # workspace_id
        assert audit_params[3] == "usr_test"  # edited_by
        # action='retract' 在 SQL 中
        assert "'retract'" in audit_q

    @pytest.mark.asyncio
    async def test_retract_message_409_outside_window(self, monkeypatch):
        """超过 5 分钟时间窗口 → 409。"""
        old_msg = _msg_row(
            content="hi",
            created_at=datetime.now(UTC) - timedelta(minutes=10),
            sender_id="usr_test",
        )
        conn = _RecordingConnection(results=[_Result(row=old_msg)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 409
        # 不应有 UPDATE/INSERT
        assert all("UPDATE" not in c[0] for c in conn.calls)
        assert all("INSERT" not in c[0] for c in conn.calls)

    @pytest.mark.asyncio
    async def test_retract_message_409_already_retracted(self, monkeypatch):
        """已撤回的消息再次撤回 → 409。"""
        retracted_msg = _msg_row(
            content="__retracted__",
            created_at=datetime.now(UTC),
            sender_id="usr_test",
        )
        conn = _RecordingConnection(results=[_Result(row=retracted_msg)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_retract_message_403_not_sender(self, monkeypatch):
        """非发送者撤回 → 403。"""
        msg_row = _msg_row(sender_id="usr_other")
        conn = _RecordingConnection(results=[_Result(row=msg_row)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_self"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_retract_message_404_cross_workspace(self, monkeypatch):
        """跨 workspace 撤回 → 404。"""
        msg_row = _msg_row(conv_workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=msg_row)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_retract_message_403_missing_capability(self, monkeypatch):
        """无 im:write capability → 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=()))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 403
        assert len(conn.calls) == 0


# ============================================================================
# 9. 消息编辑
# ============================================================================


class TestEditMessage:
    """PATCH /api/v1/im/messages/{message_id}：编辑（5 分钟内）。"""

    @pytest.mark.asyncio
    async def test_edit_message_success_within_window(self, monkeypatch):
        """时间窗口内编辑成功，写审计日志。"""
        recent = _msg_row(
            content="old",
            created_at=datetime.now(UTC),
            sender_id="usr_test",
        )
        updated = _msg_row(content="new content")
        # 1) SELECT message 2) UPDATE RETURNING 3) INSERT audit log
        conn = _RecordingConnection(
            results=[_Result(row=recent), _Result(row=updated), _Result()]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/messages/icm_1",
                json={"content": "new content"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["content"] == "new content"
        # UPDATE 包含新 content
        update_q, update_params = conn.calls[1]
        assert "UPDATE im_conv_message SET content = %s" in update_q
        assert update_params == ("new content", "icm_1")
        # 审计日志 INSERT
        audit_q, audit_params = conn.calls[2]
        assert "INSERT INTO im_message_edit_log" in audit_q
        assert "'edit'" in audit_q
        # old_payload 包含旧内容，new_payload 包含新内容
        assert "old" in audit_params[4]
        assert "new content" in audit_params[5]

    @pytest.mark.asyncio
    async def test_edit_message_409_outside_window(self, monkeypatch):
        """超过 5 分钟时间窗口 → 409。"""
        old_msg = _msg_row(
            content="hi",
            created_at=datetime.now(UTC) - timedelta(minutes=10),
            sender_id="usr_test",
        )
        conn = _RecordingConnection(results=[_Result(row=old_msg)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/messages/icm_1",
                json={"content": "edited"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_edit_message_409_retracted(self, monkeypatch):
        """已撤回的消息不可编辑 → 409。"""
        retracted_msg = _msg_row(
            content="__retracted__",
            created_at=datetime.now(UTC),
            sender_id="usr_test",
        )
        conn = _RecordingConnection(results=[_Result(row=retracted_msg)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/messages/icm_1",
                json={"content": "edited"},
            )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_edit_message_403_not_sender(self, monkeypatch):
        """非发送者编辑 → 403。"""
        msg_row = _msg_row(sender_id="usr_other")
        conn = _RecordingConnection(results=[_Result(row=msg_row)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_self"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/messages/icm_1",
                json={"content": "edited"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_edit_message_404_cross_workspace(self, monkeypatch):
        """跨 workspace 编辑 → 404。"""
        msg_row = _msg_row(conv_workspace_id="wsp_other")
        conn = _RecordingConnection(results=[_Result(row=msg_row)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(workspace_id="wsp_test"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/messages/icm_1",
                json={"content": "edited"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_edit_message_403_missing_capability(self, monkeypatch):
        """无 im:write capability → 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(capabilities=()))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/messages/icm_1",
                json={"content": "edited"},
            )
        assert resp.status_code == 403
        assert len(conn.calls) == 0


# ============================================================================
# 10. 撤回 + 编辑联动 + 审计日志
# ============================================================================


class TestRetractEditInteraction:
    """撤回/编辑联动 + 审计日志校验。"""

    @pytest.mark.asyncio
    async def test_retract_then_edit_returns_409(self, monkeypatch):
        """撤回后再编辑 → 409。"""
        retracted_msg = _msg_row(
            content="__retracted__",
            created_at=datetime.now(UTC),
            sender_id="usr_test",
        )
        conn = _RecordingConnection(results=[_Result(row=retracted_msg)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/messages/icm_1",
                json={"content": "try edit after retract"},
            )
        assert resp.status_code == 409
        assert "retracted" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_retract_writes_audit_with_old_and_new_payload(self, monkeypatch):
        """撤回审计日志包含 old_payload（原 content）与 new_payload（__retracted__）。"""
        recent = _msg_row(
            content="original text",
            created_at=datetime.now(UTC),
            sender_id="usr_test",
        )
        conn = _RecordingConnection(
            results=[_Result(row=recent), _Result(), _Result()]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 200
        audit_q, audit_params = conn.calls[2]
        # old_payload JSON 包含原 content
        assert "original text" in audit_params[4]
        # new_payload JSON 包含 __retracted__
        assert "__retracted__" in audit_params[5]

    @pytest.mark.asyncio
    async def test_edit_writes_audit_with_old_and_new_payload(self, monkeypatch):
        """编辑审计日志包含 old_payload（原 content）与 new_payload（新 content）。"""
        recent = _msg_row(
            content="before edit",
            created_at=datetime.now(UTC),
            sender_id="usr_test",
        )
        updated = _msg_row(content="after edit")
        conn = _RecordingConnection(
            results=[_Result(row=recent), _Result(row=updated), _Result()]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/messages/icm_1",
                json={"content": "after edit"},
            )
        assert resp.status_code == 200
        audit_q, audit_params = conn.calls[2]
        assert "before edit" in audit_params[4]  # old_payload
        assert "after edit" in audit_params[5]  # new_payload

    @pytest.mark.asyncio
    async def test_retract_just_at_window_boundary_409(self, monkeypatch):
        """恰好超过 5 分钟（301 秒）→ 409。"""
        boundary_msg = _msg_row(
            content="hi",
            created_at=datetime.now(UTC) - timedelta(seconds=301),
            sender_id="usr_test",
        )
        conn = _RecordingConnection(results=[_Result(row=boundary_msg)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_edit_just_within_window_success(self, monkeypatch):
        """恰好 4 分 59 秒（299 秒）→ 200。"""
        boundary_msg = _msg_row(
            content="hi",
            created_at=datetime.now(UTC) - timedelta(seconds=299),
            sender_id="usr_test",
        )
        updated = _msg_row(content="edited")
        conn = _RecordingConnection(
            results=[_Result(row=boundary_msg), _Result(row=updated), _Result()]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                "/api/v1/im/messages/icm_1",
                json={"content": "edited"},
            )
        assert resp.status_code == 200


# ============================================================================
# 11. 鉴权：401 未登录、403 跨 workspace / 角色不足
# ============================================================================


class TestAuthAndAuthorization:
    """鉴权矩阵：401 / 403。"""

    @pytest.mark.asyncio
    async def test_groups_endpoint_401_without_auth(self):
        """未登录访问群组列表 → 401。"""
        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/im/groups")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_create_group_401_without_auth(self):
        """未登录创建群组 → 401。"""
        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups",
                json={"name": "X", "member_user_ids": []},
            )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_offline_ack_401_without_auth(self):
        """未登录确认离线消息 → 401。"""
        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/offline-messages/iom_1/ack")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_retract_401_without_auth(self):
        """未登录撤回消息 → 401。"""
        app = _app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/im/messages/icm_1/retract")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_dissolve_group_403_admin_role(self, monkeypatch):
        """admin 不能解散（仅 owner）→ 403。"""
        group = _group_row(owner_id="usr_owner")
        conn = _RecordingConnection(results=[_Result(row=group)])
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_admin", role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/im/groups/img_1")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_invite_members_403_when_not_group_admin(self, monkeypatch):
        """member 角色邀请成员 → 403。"""
        group = _group_row()
        caller = _group_member_row(role="member", user_id="usr_mem")
        conn = _RecordingConnection(
            results=[_Result(row=group), _Result(row=caller)]
        )
        monkeypatch.setattr(msg, "pool", _Pool(conn))

        app = _app(actor=_actor(user_id="usr_mem"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/im/groups/img_1/members",
                json={"user_ids": ["usr_new"], "role": "member"},
            )
        assert resp.status_code == 403


# ============================================================================
# 12. schema 与导出
# ============================================================================


class TestSchemaAndExports:
    """SCHEMA_STATEMENTS 包含新表 + 导出 im_router。"""

    def test_schema_contains_im_offline_message_table(self):
        """SCHEMA_STATEMENTS 包含 im_offline_message 建表。"""
        assert any(
            "CREATE TABLE IF NOT EXISTS im_offline_message" in stmt
            for stmt in msg.SCHEMA_STATEMENTS
        )

    def test_schema_contains_im_group_table(self):
        """SCHEMA_STATEMENTS 包含 im_group 建表。"""
        assert any(
            "CREATE TABLE IF NOT EXISTS im_group" in stmt
            for stmt in msg.SCHEMA_STATEMENTS
        )

    def test_schema_contains_im_group_member_table(self):
        """SCHEMA_STATEMENTS 包含 im_group_member 建表 + UNIQUE 约束。"""
        member_stmt = next(
            (
                s
                for s in msg.SCHEMA_STATEMENTS
                if "CREATE TABLE IF NOT EXISTS im_group_member" in s
            ),
            None,
        )
        assert member_stmt is not None
        assert "PRIMARY KEY (group_id, user_id)" in member_stmt
        assert "role IN ('owner', 'admin', 'member')" in member_stmt

    def test_schema_contains_im_message_edit_log_table(self):
        """SCHEMA_STATEMENTS 包含 im_message_edit_log 建表 + action 约束。"""
        audit_stmt = next(
            (
                s
                for s in msg.SCHEMA_STATEMENTS
                if "CREATE TABLE IF NOT EXISTS im_message_edit_log" in s
            ),
            None,
        )
        assert audit_stmt is not None
        assert "action IN ('retract', 'edit')" in audit_stmt

    def test_module_exports_im_router(self):
        """模块导出 im_router 与新 Pydantic 模型。"""
        assert hasattr(msg, "im_router")
        assert msg.im_router.prefix == "/api/v1/im"
        for cls_name in (
            "GroupCreateRequest",
            "GroupUpdateRequest",
            "GroupInviteRequest",
            "MessageEditRequest",
        ):
            assert cls_name in msg.__all__

    @pytest.mark.asyncio
    async def test_ensure_messaging_schema_executes_all_statements(self, monkeypatch):
        """ensure_messaging_schema 跑完所有 SCHEMA_STATEMENTS。"""
        conn = _RecordingConnection()
        await msg.ensure_messaging_schema(conn)
        # 调用次数 = SCHEMA_STATEMENTS 长度
        assert len(conn.calls) == len(msg.SCHEMA_STATEMENTS)


# ============================================================================
# 13. capability 校验函数
# ============================================================================


class TestRequireCapability:
    """_require_capability 辅助函数。"""

    def test_wildcard_passes(self):
        """capabilities=('*',) 通过任意 capability。"""
        msg._require_capability(_actor(capabilities=("*",)), "im:write")

    def test_domain_wildcard_passes(self):
        """capabilities=('im:*',) 通过 im:read / im:write。"""
        msg._require_capability(_actor(capabilities=("im:*",)), "im:read")
        msg._require_capability(_actor(capabilities=("im:*",)), "im:write")

    def test_exact_capability_passes(self):
        """capabilities=('im:read',) 通过 im:read。"""
        msg._require_capability(_actor(capabilities=("im:read",)), "im:read")

    def test_missing_capability_raises_403(self):
        """无相关 capability → 403。"""
        with pytest.raises(HTTPException) as exc:
            msg._require_capability(_actor(capabilities=()), "im:write")
        assert exc.value.status_code == 403

    def test_other_domain_capability_raises_403(self):
        """仅有 messaging:* 不通过 im:write。"""
        with pytest.raises(HTTPException):
            msg._require_capability(
                _actor(capabilities=("messaging:*",)), "im:write"
            )
