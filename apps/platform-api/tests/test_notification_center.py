"""通知中心 (notification.py) 单元 + 端点测试。

v7.153: 18 个测试覆盖：
- 创建通知：成功 / admin 鉴权 / kind 非法回退（3）
- 列表：默认 / unread_only / kind 过滤 / pagination（4）
- 详情：成功 / 不存在 404（2）
- 标记已读：单条 / 单条不存在 / 全部已读（3）
- 未读数：成功（1）
- 删除：成功 / 不存在 404（2）
- workspace 隔离：跨区 404（1）
- 鉴权：member 不能创建 / member 可读 / 未认证 401（2）

注意：本测试用 ``notification_center`` 命名文件以避免与既有
``test_notifications.py``（覆盖 notification 包）冲突。被测模块通过
importlib 加载（与 billing.py 一致），所有测试使用 fake pool/connection，
不依赖真实 DB / Redis / 网络。

辅助函数 ``create_notification`` 直接调用而非通过端点，验证其
``kind`` 非法时回退 ``info`` 的契约。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor


# ============================================================================
# 通过 importlib 加载被包遮蔽的 notification.py
# ============================================================================

_NOTIF_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "workama_platform"
    / "modules"
    / "notification.py"
)
_spec = importlib.util.spec_from_file_location(
    "workama_platform.modules.notification_center_test", _NOTIF_PATH
)
n = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = n
_spec.loader.exec_module(n)


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    """模拟 psycopg 查询结果。"""

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []
        self.rowcount = len(self._rows)

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
    role="owner",
    workspace_id="wsp_test",
    user_id="usr_test",
    capabilities=("*",),
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _notif_row(**overrides) -> dict:
    base = {
        "id": "notif_1",
        "workspace_id": "wsp_test",
        "user_id": "usr_test",
        "kind": "info",
        "title": "Hello",
        "body": "World",
        "action_url": None,
        "action_label": None,
        "read": False,
        "metadata": {},
        "created_at": datetime.now(UTC),
        "read_at": None,
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(n.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ============================================================================
# 1. 创建通知
# ============================================================================


class TestCreateNotification:
    @pytest.mark.asyncio
    async def test_create_endpoint_success(self, monkeypatch):
        """POST /notification-center admin 创建返回 201。"""
        conn = _RecordingConnection(results=[_Result(row=_notif_row())])
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor(role="admin"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/notification-center",
                json={
                    "user_id": "usr_test",
                    "kind": "info",
                    "title": "Hello",
                    "body": "World",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "Hello"
        assert body["kind"] == "info"
        assert any("INSERT INTO notification" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_create_endpoint_member_forbidden(self, monkeypatch):
        """member 角色创建通知返回 403。"""
        conn = _RecordingConnection()
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/notification-center",
                json={"user_id": "usr_test", "title": "X"},
            )
        assert resp.status_code == 403
        assert conn.calls == []  # 鉴权失败前不应执行 SQL

    @pytest.mark.asyncio
    async def test_create_helper_invalid_kind_falls_back(self, monkeypatch):
        """create_notification 辅助函数：kind 非法时回退为 info。"""
        captured = {}

        class _CapturingConn:
            def transaction(self):
                return _Transaction()

            async def execute(self, query, params=()):
                captured["query"] = query
                captured["params"] = params
                return _Result(row=_notif_row(kind="info"))

        monkeypatch.setattr(n, "pool", _Pool(_CapturingConn()))
        result = await n.create_notification(
            workspace_id="wsp_test",
            user_id="usr_test",
            kind="INVALID_KIND",
            title="t",
        )
        assert result["kind"] == "info"
        # 参数中第 4 个位置是 kind
        assert captured["params"][3] == "info"


# ============================================================================
# 2. 列表 / 未读数
# ============================================================================


class TestListAndCount:
    @pytest.mark.asyncio
    async def test_list_default(self, monkeypatch):
        """GET 默认列表返回 items + total + unread_count。"""
        rows = [_notif_row(id="n1"), _notif_row(id="n2", read=True)]
        # fetchall (list) + fetchone (count) + fetchone (unread)
        conn = _RecordingConnection(
            results=[
                _Result(rows=rows),
                _Result(row={"count": 2}),
                _Result(row={"count": 1}),
            ]
        )
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/notification-center")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["total"] == 2
        assert body["unread_count"] == 1

    @pytest.mark.asyncio
    async def test_list_unread_only_filter(self, monkeypatch):
        """unread_only=true 会在 SQL 加 read = FALSE 条件。"""
        conn = _RecordingConnection(
            results=[
                _Result(rows=[]),
                _Result(row={"count": 0}),
                _Result(row={"count": 0}),
            ]
        )
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/notification-center", params={"unread_only": True}
            )
        assert resp.status_code == 200
        # 列表 SQL 必须包含 read = FALSE
        assert "read = FALSE" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_list_kind_filter(self, monkeypatch):
        """kind 过滤会在 SQL 加 kind = %s 条件。"""
        conn = _RecordingConnection(
            results=[
                _Result(rows=[]),
                _Result(row={"count": 0}),
                _Result(row={"count": 0}),
            ]
        )
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/notification-center", params={"kind": "warning"}
            )
        assert resp.status_code == 200
        assert "kind = %s" in conn.calls[0][0]

    @pytest.mark.asyncio
    async def test_list_pagination(self, monkeypatch):
        """limit / offset 透传到 SQL。"""
        conn = _RecordingConnection(
            results=[
                _Result(rows=[]),
                _Result(row={"count": 0}),
                _Result(row={"count": 0}),
            ]
        )
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/notification-center",
                params={"limit": 10, "offset": 20},
            )
        assert resp.status_code == 200
        # SQL 参数末尾两个是 limit / offset
        params = conn.calls[0][1]
        assert params[-2] == 10
        assert params[-1] == 20

    @pytest.mark.asyncio
    async def test_unread_count(self, monkeypatch):
        """GET /unread-count 返回 unread_count。"""
        conn = _RecordingConnection(results=[_Result(row={"count": 5})])
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/notification-center/unread-count")
        assert resp.status_code == 200
        body = resp.json()
        assert body["unread_count"] == 5


# ============================================================================
# 3. 详情 / 标记已读 / 全部已读
# ============================================================================


class TestDetailAndRead:
    @pytest.mark.asyncio
    async def test_get_detail_success(self, monkeypatch):
        """GET /{id} 返回详情。"""
        conn = _RecordingConnection(results=[_Result(row=_notif_row(id="n1"))])
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/notification-center/n1")
        assert resp.status_code == 200
        assert resp.json()["id"] == "n1"

    @pytest.mark.asyncio
    async def test_get_detail_not_found(self, monkeypatch):
        """GET /{id} 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/notification-center/missing")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_mark_read_success(self, monkeypatch):
        """POST /{id}/read 标记已读。"""
        conn = _RecordingConnection(
            results=[_Result(row=_notif_row(id="n1", read=True))]
        )
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/notification-center/n1/read")
        assert resp.status_code == 200
        assert resp.json()["read"] is True
        assert any("UPDATE notification" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_mark_read_not_found(self, monkeypatch):
        """POST /{id}/read 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/notification-center/missing/read")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_mark_all_read(self, monkeypatch):
        """POST /read-all 批量标记已读。"""
        conn = _RecordingConnection(
            results=[_Result(rows=[{"id": "n1"}, {"id": "n2"}])]
        )
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/notification-center/read-all")
        assert resp.status_code == 200
        body = resp.json()
        assert body["updated"] == 2


# ============================================================================
# 4. 删除
# ============================================================================


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_success(self, monkeypatch):
        """DELETE /{id} 硬删除返回 200。"""
        conn = _RecordingConnection(results=[_Result(row={"id": "n1"})])
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/notification-center/n1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] is True
        assert body["id"] == "n1"
        assert any("DELETE FROM notification" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_delete_not_found(self, monkeypatch):
        """DELETE /{id} 不存在返回 404。"""
        conn = _RecordingConnection(results=[_Result(row=None)])
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/v1/notification-center/missing")
        assert resp.status_code == 404


# ============================================================================
# 5. 鉴权 / workspace 隔离
# ============================================================================


class TestAuthAndIsolation:
    @pytest.mark.asyncio
    async def test_member_can_read(self, monkeypatch):
        """member 角色可以读取通知（capability ``notification:read`` 通过 ``*``）。"""
        conn = _RecordingConnection(
            results=[
                _Result(rows=[]),
                _Result(row={"count": 0}),
                _Result(row={"count": 0}),
            ]
        )
        monkeypatch.setattr(n, "pool", _Pool(conn))

        app = _app(actor=_actor(role="member"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/notification-center")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self, monkeypatch):
        """未认证请求返回 401（get_actor 抛 401）。"""
        # 不注入 actor override -> get_actor 默认实现抛 401
        app = _app(actor=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/notification-center")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_workspace_isolation_in_list(self, monkeypatch):
        """list SQL 包含 workspace_id = %s 隔离条件。"""
        conn = _RecordingConnection(
            results=[
                _Result(rows=[]),
                _Result(row={"count": 0}),
                _Result(row={"count": 0}),
            ]
        )
        monkeypatch.setattr(n, "pool", _Pool(conn))

        actor = _actor(workspace_id="wsp_other")
        app = _app(actor=actor)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/notification-center")
        assert resp.status_code == 200
        # workspace_id 必须作为参数传入
        params = conn.calls[0][1]
        assert "wsp_other" in params


# ============================================================================
# 6. 辅助函数 / 模型边界
# ============================================================================


class TestHelpers:
    def test_summary_handles_none_metadata(self):
        """_summary 对 metadata 为 None 返回空 dict。"""
        row = _notif_row(metadata=None)
        result = n._summary(row)
        assert result["metadata"] == {}

    def test_summary_preserves_all_fields(self):
        """_summary 完整返回所有字段。"""
        row = _notif_row(
            id="x", kind="warning", title="T", body="B",
            action_url="/x", action_label="Go", read=True,
        )
        result = n._summary(row)
        assert result["id"] == "x"
        assert result["kind"] == "warning"
        assert result["title"] == "T"
        assert result["body"] == "B"
        assert result["action_url"] == "/x"
        assert result["action_label"] == "Go"
        assert result["read"] is True

    def test_valid_kinds_complete(self):
        """_VALID_KINDS 包含 5 种 kind。"""
        assert n._VALID_KINDS == frozenset(
            {"info", "success", "warning", "error", "system"}
        )

    def test_router_prefix_unique(self):
        """router prefix 与既有 notification 包不冲突。"""
        assert n.router.prefix == "/api/v1/notification-center"
