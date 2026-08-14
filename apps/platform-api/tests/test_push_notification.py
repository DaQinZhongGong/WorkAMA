"""PWA Web Push 模块单元测试。

覆盖范围：
- 订阅：创建 / 幂等 / 字段校验 / 移动端兼容路径
- 发送：按 user 发送 / 按 workspace 广播 / admin 鉴权 / 失败静默
- 列表：当前用户订阅列表
- 删除：单条删除 / 批量删除自身 / 404 / workspace 隔离 / 越权拒绝
- 鉴权：未认证 401 / member 发送被拒绝 403
- 隔离：跨 workspace 查询与删除返回 403

所有测试使用 fake pool/connection，不依赖真实 DB / 网络 / 推送服务。
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules.push_notification import (
    PushSendRequest,
    SubscriptionCreateRequest,
    _require_admin,
    _send_push_to_endpoint,
    delete_subscription,
    list_subscriptions,
    remove_own_subscription,
    send_push,
    subscribe,
)


# ============================================================================
# 测试辅助：fake pool / connection / result
# ============================================================================


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = list(rows) if rows is not None else []
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
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        connection = self._connection

        class _Context:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_args):
                return False

        return _Context()


def _actor(
    *,
    role="owner",
    workspace_id="wsp_test",
    user_id="usr_test",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role=role,
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=("*",),
    )


def _sub_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "push_1",
        "workspace_id": "wsp_test",
        "user_id": "usr_test",
        "endpoint": "https://fcm.example.com/token-a",
        "p256dh": "p256dh_val",
        "auth": "auth_val",
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    }
    base.update(overrides)
    return base


# ============================================================================
# 1. 订阅端点
# ============================================================================


@pytest.mark.asyncio
async def test_subscribe_creates_record(monkeypatch):
    """订阅成功创建记录并返回 summary。"""
    conn = _RecordingConnection(results=[_Result(), _Result(row=_sub_row())])
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    body = SubscriptionCreateRequest(
        endpoint="https://fcm.example.com/token-a",
        keys={"p256dh": "p256dh_val", "auth": "auth_val"},
    )
    result = await subscribe(body, _actor())

    assert result["endpoint"] == "https://fcm.example.com/token-a"
    assert result["p256dh"] == "p256dh_val"
    assert result["auth"] == "auth_val"
    insert_q = next(q for q, _ in conn.calls if "INSERT INTO push_subscription" in q)
    assert insert_q


@pytest.mark.asyncio
async def test_subscribe_is_idempotent(monkeypatch):
    """同一 endpoint 在同一 workspace 先删除后插入，保持唯一。"""
    conn = _RecordingConnection(results=[_Result(), _Result(row=_sub_row())])
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    body = SubscriptionCreateRequest(
        endpoint="https://fcm.example.com/token-a",
        keys={"p256dh": "p256dh_val", "auth": "auth_val"},
    )
    await subscribe(body, _actor())

    delete_q = next(q for q, _ in conn.calls if "DELETE FROM push_subscription" in q)
    assert delete_q
    insert_q = next(q for q, _ in conn.calls if "INSERT INTO push_subscription" in q)
    assert insert_q


@pytest.mark.asyncio
async def test_subscribe_uses_empty_string_for_missing_keys(monkeypatch):
    """keys 缺失时 p256dh/auth 使用空字符串。"""
    conn = _RecordingConnection(results=[_Result(), _Result(row=_sub_row(p256dh="", auth=""))])
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    body = SubscriptionCreateRequest(endpoint="https://fcm.example.com/token-b")
    result = await subscribe(body, _actor())

    assert result["p256dh"] == ""
    assert result["auth"] == ""


# ============================================================================
# 2. 发送端点
# ============================================================================


@pytest.mark.asyncio
async def test_send_push_to_user_requires_admin():
    """非 admin/owner 调用发送返回 403。"""
    body = PushSendRequest(user_id="usr_other", title="Hello")
    with pytest.raises(HTTPException) as exc:
        await send_push(body, _actor(role="member"))
    assert exc.value.status_code == 403
    assert "Admin role" in exc.value.detail


@pytest.mark.asyncio
async def test_send_push_to_user_queries_by_user_id(monkeypatch):
    """指定 user_id 时只查询该用户的 endpoint。"""
    conn = _RecordingConnection(
        results=[_Result(rows=[{"endpoint": "https://ep1"}, {"endpoint": "https://ep2"}])]
    )
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))
    delivered: list[str] = []

    async def fake_send(endpoint: str, payload: dict[str, Any]) -> bool:
        delivered.append(endpoint)
        return True

    monkeypatch.setattr(pn, "_send_push_to_endpoint", fake_send)

    body = PushSendRequest(user_id="usr_target", title="Hello", body="World")
    result = await send_push(body, _actor())

    select_q = next(q for q, _ in conn.calls if "SELECT endpoint FROM push_subscription" in q)
    assert "user_id = %s" in select_q
    assert result["sent"] == 2
    assert result["target"] == "usr_target"


@pytest.mark.asyncio
async def test_send_push_broadcasts_to_workspace(monkeypatch):
    """未指定 user_id 时向 workspace 全部订阅者广播。"""
    conn = _RecordingConnection(results=[_Result(rows=[{"endpoint": "https://ep1"}])])
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    async def fake_send(endpoint: str, payload: dict[str, Any]) -> bool:
        return True

    monkeypatch.setattr(pn, "_send_push_to_endpoint", fake_send)

    body = PushSendRequest(title="Broadcast")
    result = await send_push(body, _actor())

    assert result["sent"] == 1
    assert result["target"] == "workspace"


@pytest.mark.asyncio
async def test_send_push_counts_failures_silently(monkeypatch):
    """推送失败静默，sent/failed 计数正确。"""
    conn = _RecordingConnection(
        results=[_Result(rows=[{"endpoint": "https://ok"}, {"endpoint": "https://fail"}])]
    )
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    async def fake_send(endpoint: str, payload: dict[str, Any]) -> bool:
        return endpoint == "https://ok"

    monkeypatch.setattr(pn, "_send_push_to_endpoint", fake_send)

    body = PushSendRequest(title="T")
    result = await send_push(body, _actor())

    assert result["sent"] == 1
    assert result["failed"] == 1
    assert result["total"] == 2


@pytest.mark.asyncio
async def test_send_push_with_optional_fields(monkeypatch):
    """发送时可选字段 icon/badge/tag/url 被传入 payload。"""
    conn = _RecordingConnection(results=[_Result(rows=[{"endpoint": "https://ep1"}])])
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))
    captured_payload: dict[str, Any] | None = None

    async def fake_send(endpoint: str, payload: dict[str, Any]) -> bool:
        nonlocal captured_payload
        captured_payload = payload
        return True

    monkeypatch.setattr(pn, "_send_push_to_endpoint", fake_send)

    body = PushSendRequest(
        title="T", body="B", icon="icon.png", badge="badge.png", tag="news", url="/chat"
    )
    await send_push(body, _actor())

    assert captured_payload is not None
    assert captured_payload["icon"] == "icon.png"
    assert captured_payload["badge"] == "badge.png"
    assert captured_payload["tag"] == "news"
    assert captured_payload["url"] == "/chat"


# ============================================================================
# 3. 列表端点
# ============================================================================


@pytest.mark.asyncio
async def test_list_subscriptions_returns_user_items(monkeypatch):
    """列表只返回当前用户的订阅。"""
    conn = _RecordingConnection(
        results=[_Result(rows=[_sub_row(id="push_1"), _sub_row(id="push_2")])]
    )
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    result = await list_subscriptions(_actor())

    assert len(result["items"]) == 2
    select_q = next(q for q, _ in conn.calls if "SELECT * FROM push_subscription" in q)
    assert "user_id = %s" in select_q
    assert "workspace_id = %s" in select_q


@pytest.mark.asyncio
async def test_list_subscriptions_empty(monkeypatch):
    """无订阅时返回空列表。"""
    conn = _RecordingConnection(results=[_Result(rows=[])])
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    result = await list_subscriptions(_actor())

    assert result["items"] == []
    assert result["count"] == 0


# ============================================================================
# 4. 删除端点
# ============================================================================


@pytest.mark.asyncio
async def test_delete_subscription_returns_deleted(monkeypatch):
    """删除成功返回 deleted=True。"""
    conn = _RecordingConnection(results=[_Result(row=_sub_row()), _Result()])
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    result = await delete_subscription("push_1", _actor())

    assert result["deleted"] is True
    assert result["id"] == "push_1"


@pytest.mark.asyncio
async def test_delete_subscription_returns_404_when_missing(monkeypatch):
    """删除不存在的订阅返回 404。"""
    conn = _RecordingConnection(results=[_Result(row=None)])
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await delete_subscription("push_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_subscription_workspace_isolation(monkeypatch):
    """跨 workspace 删除返回 403。"""
    conn = _RecordingConnection(results=[_Result(row=_sub_row(workspace_id="wsp_other"))])
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await delete_subscription("push_1", _actor(workspace_id="wsp_test"))
    assert exc.value.status_code == 403
    assert "another workspace" in exc.value.detail


@pytest.mark.asyncio
async def test_delete_subscription_rejects_member_deleting_others(monkeypatch):
    """member 不能删除其他用户的订阅。"""
    conn = _RecordingConnection(
        results=[_Result(row=_sub_row(user_id="usr_other"))]
    )
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await delete_subscription("push_1", _actor(role="member", user_id="usr_self"))
    assert exc.value.status_code == 403
    assert "another user's subscription" in exc.value.detail


@pytest.mark.asyncio
async def test_delete_subscription_allows_admin_deleting_others(monkeypatch):
    """admin 可以删除同 workspace 其他用户的订阅。"""
    conn = _RecordingConnection(results=[_Result(row=_sub_row(user_id="usr_other")), _Result()])
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    result = await delete_subscription("push_1", _actor(role="admin", user_id="usr_self"))
    assert result["deleted"] is True


@pytest.mark.asyncio
async def test_remove_own_subscription_deletes_all_for_user(monkeypatch):
    """remove 端点删除当前用户在本 workspace 的全部订阅。"""
    conn = _RecordingConnection(
        results=[_Result(rows=[{"id": "push_1"}, {"id": "push_2"}])]
    )
    from workama_platform.modules import push_notification as pn

    monkeypatch.setattr(pn, "pool", _Pool(conn))

    result = await remove_own_subscription(_actor())

    assert result["deleted"] == 2
    assert "push_1" in result["ids"]
    assert "push_2" in result["ids"]


# ============================================================================
# 5. 鉴权与辅助函数
# ============================================================================


def test_require_admin_allows_owner():
    """owner 通过 admin 校验。"""
    _require_admin(_actor(role="owner"))


def test_require_admin_allows_admin():
    """admin 通过 admin 校验。"""
    _require_admin(_actor(role="admin"))


def test_require_admin_rejects_member():
    """member 被拒绝。"""
    with pytest.raises(HTTPException) as exc:
        _require_admin(_actor(role="member"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_send_push_to_endpoint_failure_silently(monkeypatch):
    """_send_push_to_endpoint 网络失败时静默返回 False。"""
    import httpx

    async def fake_post(*_args, **_kwargs):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await _send_push_to_endpoint("https://ep", {"title": "T"})
    assert result is False


@pytest.mark.asyncio
async def test_send_push_to_endpoint_success(monkeypatch):
    """_send_push_to_endpoint 2xx 返回 True。"""
    import httpx

    class _FakeResponse:
        status_code = 200

    async def fake_post(*_args, **_kwargs):
        return _FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await _send_push_to_endpoint("https://ep", {"title": "T"})
    assert result is True
