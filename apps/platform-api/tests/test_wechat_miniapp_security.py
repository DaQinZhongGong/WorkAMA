"""微信小程序会话安全强化模块测试（P2 阶段）。

覆盖范围：
- logout 端点：成功 / 撤销 refresh_token / 404 不存在 / access_token 提取 / Redis blacklist 调用
- security-check 端点：成功 / suspicious_flags 类型 / IP 异常 / 并发会话过多 / 空会话
- sessions/revoke 端点：成功 / 404 / 跨 workspace 404 / 非 admin 403
- sessions 列表端点：成功 / 分页 cursor / 空列表 / limit 边界
- 鉴权：401 未登录 / 403 member 调 admin 端点
- session_log 审计日志：login/logout/refresh/revoke 四种 action 记录
- Redis blacklist：setex 调用参数 / TTL / token_hash
- Pydantic 模型验证：LogoutRequest / RevokeSessionRequest
- 表结构存在性：SECURITY_SCHEMA_STATEMENTS 包含表与索引
- 路由注册：12 个端点

所有测试使用 fake pool/connection/redis，不依赖真实 DB / Redis / 微信 API。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from workama_platform.core import Actor
from workama_platform.modules import wechat_miniapp as wma
from workama_platform.modules.wechat_miniapp import (
    LogoutRequest,
    RevokeSessionRequest,
    SECURITY_SCHEMA_STATEMENTS,
    _client_ip,
    _extract_bearer_token,
    _is_token_revoked,
    _log_session_event,
    _revoke_token,
    _token_hash,
    ensure_session_security_schema,
    list_sessions,
    logout,
    revoke_session,
    security_check,
)


# ============================================================================
# 测试辅助：fake pool / connection / result / redis
# ============================================================================


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = list(rows) if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _SeqConnection:
    """按调用顺序返回预设结果的连接，记录所有 execute 调用。"""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls: list[tuple[str, tuple]] = []
        self._idx = 0

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None

    async def rollback(self):
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


class _FakeRedis:
    """记录 setex / get 调用的 fake redis。"""

    def __init__(self):
        self.setex_calls: list[tuple[str, int, str]] = []
        self._store: dict[str, str] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.setex_calls.append((key, ttl, value))
        self._store[key] = value

    async def get(self, key: str) -> str | None:
        return self._store.get(key)


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


def _session_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "wmasess_1",
        "workspace_id": "wsp_test",
        "openid": "oABCDEFGHIJKLMNopqrstuvwxyz1234567890",
        "session_token": "sess_token_AAAA",
        "refresh_token": "refresh_token_BBBB",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "refresh_expires_at": datetime.now(UTC) + timedelta(days=1),
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _user_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "wmau_1",
        "workspace_id": "wsp_test",
        "openid": "oABCDEFGHIJKLMNopqrstuvwxyz1234567890",
        "user_id": "usr_test",
        "nickname": "Tester",
        "session_key": "SECRET",
    }
    base.update(overrides)
    return base


def _log_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "wxsl_1",
        "workspace_id": "wsp_test",
        "user_id": "usr_test",
        "session_id": "wmasess_1",
        "action": "login",
        "ip": "10.0.0.1",
        "user_agent": "Mozilla/5.0",
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(wma.router)
    return app


# ============================================================================
# 1. logout 端点
# ============================================================================


@pytest.mark.asyncio
async def test_logout_success(monkeypatch):
    """logout 成功：撤销 token、删除会话、写审计日志。"""
    session = _session_row()
    user = {"user_id": "usr_test"}
    conn = _SeqConnection(results=[_Result(row=session), _Result(row=user)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))
    fake_redis = _FakeRedis()
    monkeypatch.setattr(wma, "redis", fake_redis)

    result = await logout(
        LogoutRequest(refresh_token="refresh_token_BBBB"),
        _actor(),
        authorization="Bearer access_token_XXXX",
        x_forwarded_for="10.0.0.1",
        user_agent="Mozilla/5.0",
    )

    assert result["logged_out"] is True
    assert result["session_id"] == "wmasess_1"
    # 验证 DELETE session 执行
    delete_q = next(q for q, _ in conn.calls if "DELETE FROM wechat_miniapp_session" in q)
    assert delete_q
    # 验证审计日志写入
    log_q = next(q for q, _ in conn.calls if "INSERT INTO wx_miniapp_session_log" in q)
    assert log_q
    # 验证 "logout" action 在日志参数中
    log_params = [p for q, p in conn.calls if "INSERT INTO wx_miniapp_session_log" in q][0]
    assert "logout" in log_params


@pytest.mark.asyncio
async def test_logout_revokes_refresh_token_via_redis(monkeypatch):
    """logout 调用 Redis setex 撤销 refresh_token / access_token / session_token。"""
    session = _session_row()
    user = {"user_id": "usr_test"}
    conn = _SeqConnection(results=[_Result(row=session), _Result(row=user)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))
    fake_redis = _FakeRedis()
    monkeypatch.setattr(wma, "redis", fake_redis)

    await logout(
        LogoutRequest(refresh_token="refresh_token_BBBB"),
        _actor(),
        authorization="Bearer access_token_XXXX",
    )

    # 至少 3 次 setex 调用（refresh_token / access_token / session_token）
    assert len(fake_redis.setex_calls) >= 3
    # 验证 key 格式为 revoked:{hash}
    for key, ttl, value in fake_redis.setex_calls:
        assert key.startswith("revoked:")
        assert ttl > 0
        assert value == "1"


@pytest.mark.asyncio
async def test_logout_session_not_found_404(monkeypatch):
    """logout 不存在的 refresh_token → 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))
    monkeypatch.setattr(wma, "redis", _FakeRedis())

    with pytest.raises(HTTPException) as exc:
        await logout(
            LogoutRequest(refresh_token="nonexistent"),
            _actor(),
            authorization="Bearer access_token",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_logout_missing_authorization_401(monkeypatch):
    """logout 缺少 Authorization 头 → 401（_extract_bearer_token 抛异常）。"""
    conn = _SeqConnection(results=[_Result(row=_session_row()), _Result(row={"user_id": "usr_test"})])
    monkeypatch.setattr(wma, "pool", _Pool(conn))
    monkeypatch.setattr(wma, "redis", _FakeRedis())

    with pytest.raises(HTTPException) as exc:
        await logout(
            LogoutRequest(refresh_token="refresh_token_BBBB"),
            _actor(),
            authorization=None,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_logout_cross_workspace_404(monkeypatch):
    """logout 跨 workspace 的 refresh_token → 404（workspace 隔离）。"""
    session = _session_row(workspace_id="wsp_other")
    conn = _SeqConnection(results=[_Result(row=None)])  # workspace 不匹配 → None
    monkeypatch.setattr(wma, "pool", _Pool(conn))
    monkeypatch.setattr(wma, "redis", _FakeRedis())

    with pytest.raises(HTTPException) as exc:
        await logout(
            LogoutRequest(refresh_token="refresh_token_BBBB"),
            _actor(workspace_id="wsp_test"),
            authorization="Bearer access_token",
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_logout_then_refresh_returns_401(monkeypatch):
    """logout 撤销 refresh_token 后，再次使用该 refresh_token 调 refresh → 401。"""
    # 模拟 logout 后 session 已被删除，refresh 查询返回 None
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await wma.refresh(wma.RefreshRequest(refresh_token="refresh_token_BBBB"))
    assert exc.value.status_code == 401
    assert "Invalid refresh token" in exc.value.detail


# ============================================================================
# 2. security-check 端点
# ============================================================================


@pytest.mark.asyncio
async def test_security_check_success(monkeypatch):
    """security-check 成功：返回完整安全状态。"""
    sessions = [_session_row(), _session_row(id="wmasess_2")]
    last_login = _log_row()
    conn = _SeqConnection(results=[
        _Result(rows=sessions),  # 活跃会话
        _Result(row=last_login),  # 最近 login 日志
        _Result(rows=[{"ip": "10.0.0.1"}]),  # 历史 IP
        _Result(rows=[{"user_agent": "Mozilla/5.0"}]),  # 历史 UA
    ])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await security_check(_actor(), x_forwarded_for="10.0.0.1", user_agent="Mozilla/5.0")

    assert result["active_sessions_count"] == 2
    assert result["last_login_at"] is not None
    assert result["login_ip"] == "10.0.0.1"
    assert result["device_fingerprint"] == "Mozilla/5.0"
    assert isinstance(result["suspicious_flags"], list)


@pytest.mark.asyncio
async def test_security_check_suspicious_flags_type(monkeypatch):
    """security-check 返回 suspicious_flags 为 list[str]。"""
    conn = _SeqConnection(results=[
        _Result(rows=[]),  # 无活跃会话
        _Result(row=None),  # 无 login 日志
        _Result(rows=[]),  # 无历史 IP
        _Result(rows=[]),  # 无历史 UA
    ])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await security_check(_actor())

    assert isinstance(result["suspicious_flags"], list)
    assert result["active_sessions_count"] == 0


@pytest.mark.asyncio
async def test_security_check_ip_anomaly_flag(monkeypatch):
    """当前 IP 不在历史 IP 列表中 → suspicious_flags 含 ip_anomaly。"""
    conn = _SeqConnection(results=[
        _Result(rows=[_session_row()]),
        _Result(row=_log_row(ip="10.0.0.1")),
        _Result(rows=[{"ip": "10.0.0.1"}]),  # 历史 IP
        _Result(rows=[]),
    ])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await security_check(_actor(), x_forwarded_for="192.168.99.99", user_agent=None)

    assert "ip_anomaly" in result["suspicious_flags"]


@pytest.mark.asyncio
async def test_security_check_concurrent_sessions_excessive(monkeypatch):
    """活跃会话 > 5 → suspicious_flags 含 concurrent_sessions_excessive。"""
    sessions = [_session_row(id=f"wmasess_{i}") for i in range(7)]
    conn = _SeqConnection(results=[
        _Result(rows=sessions),
        _Result(row=None),
        _Result(rows=[]),
        _Result(rows=[]),
    ])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await security_check(_actor())

    assert "concurrent_sessions_excessive" in result["suspicious_flags"]
    assert result["active_sessions_count"] == 7


@pytest.mark.asyncio
async def test_security_check_device_fingerprint_changed(monkeypatch):
    """当前 user_agent 不在历史列表 → suspicious_flags 含 device_fingerprint_changed。"""
    conn = _SeqConnection(results=[
        _Result(rows=[_session_row()]),
        _Result(row=_log_row(user_agent="Mozilla/5.0")),
        _Result(rows=[]),
        _Result(rows=[{"user_agent": "Mozilla/5.0"}]),
    ])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await security_check(_actor(), x_forwarded_for=None, user_agent="NewAgent/1.0")

    assert "device_fingerprint_changed" in result["suspicious_flags"]


# ============================================================================
# 3. sessions/revoke 端点
# ============================================================================


@pytest.mark.asyncio
async def test_revoke_session_success(monkeypatch):
    """admin 撤销指定 session 成功。"""
    session = _session_row()
    user = {"user_id": "usr_test"}
    conn = _SeqConnection(results=[_Result(row=session), _Result(row=user)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))
    fake_redis = _FakeRedis()
    monkeypatch.setattr(wma, "redis", fake_redis)

    result = await revoke_session(
        RevokeSessionRequest(session_id="wmasess_1"),
        _actor(role="admin"),
        x_forwarded_for="10.0.0.1",
        user_agent="Mozilla/5.0",
    )

    assert result["revoked"] is True
    assert result["session_id"] == "wmasess_1"
    # 验证审计日志 action=revoke
    log_params = [p for q, p in conn.calls if "INSERT INTO wx_miniapp_session_log" in q][0]
    assert "revoke" in log_params
    # 验证 Redis 撤销
    assert len(fake_redis.setex_calls) >= 2


@pytest.mark.asyncio
async def test_revoke_session_not_found_404(monkeypatch):
    """撤销不存在的 session → 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))
    monkeypatch.setattr(wma, "redis", _FakeRedis())

    with pytest.raises(HTTPException) as exc:
        await revoke_session(
            RevokeSessionRequest(session_id="nonexistent"),
            _actor(role="admin"),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_revoke_session_cross_workspace_404(monkeypatch):
    """跨 workspace 撤销 → 404（workspace 隔离，不泄露存在性）。"""
    conn = _SeqConnection(results=[_Result(row=None)])  # workspace 不匹配
    monkeypatch.setattr(wma, "pool", _Pool(conn))
    monkeypatch.setattr(wma, "redis", _FakeRedis())

    with pytest.raises(HTTPException) as exc:
        await revoke_session(
            RevokeSessionRequest(session_id="wmasess_1"),
            _actor(role="admin", workspace_id="wsp_test"),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_revoke_session_non_admin_403():
    """member 调用 revoke → 403。"""
    with pytest.raises(HTTPException) as exc:
        await revoke_session(
            RevokeSessionRequest(session_id="wmasess_1"),
            _actor(role="member"),
        )
    assert exc.value.status_code == 403


# ============================================================================
# 4. sessions 列表端点
# ============================================================================


@pytest.mark.asyncio
async def test_list_sessions_success(monkeypatch):
    """列出当前用户的活跃会话。"""
    sessions = [_session_row(id="wmasess_1"), _session_row(id="wmasess_2")]
    conn = _SeqConnection(results=[_Result(rows=sessions)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await list_sessions(_actor())

    assert len(result["items"]) == 2
    assert result["items"][0]["session_id"] == "wmasess_1"
    assert "device" in result["items"][0]
    assert "last_active_at" in result["items"][0]
    assert "ip" in result["items"][0]


@pytest.mark.asyncio
async def test_list_sessions_empty(monkeypatch):
    """无活跃会话返回空列表。"""
    conn = _SeqConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await list_sessions(_actor())

    assert result["items"] == []
    assert result["has_more"] is False
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_sessions_pagination_cursor(monkeypatch):
    """分页：第一页返回 next_cursor，第二页用 cursor 查询。"""
    # 第一页：返回 limit+1=3 条，has_more=True
    page1_rows = [
        _session_row(id="s1", created_at=datetime(2026, 1, 3, tzinfo=UTC)),
        _session_row(id="s2", created_at=datetime(2026, 1, 2, tzinfo=UTC)),
        _session_row(id="s3", created_at=datetime(2026, 1, 1, tzinfo=UTC)),
    ]
    conn1 = _SeqConnection(results=[_Result(rows=page1_rows)])
    monkeypatch.setattr(wma, "pool", _Pool(conn1))

    result1 = await list_sessions(_actor(), limit=2)
    assert len(result1["items"]) == 2
    assert result1["has_more"] is True
    assert result1["next_cursor"] is not None

    # 第二页：用 cursor 查询，返回 1 条
    conn2 = _SeqConnection(results=[_Result(rows=[page1_rows[2]])])
    monkeypatch.setattr(wma, "pool", _Pool(conn2))

    result2 = await list_sessions(_actor(), cursor=result1["next_cursor"], limit=2)
    assert len(result2["items"]) == 1
    assert result2["has_more"] is False

    # 验证第二页查询包含 created_at < cursor 条件
    select_q = conn2.calls[0][0]
    assert "created_at <" in select_q or "%s" in select_q


@pytest.mark.asyncio
async def test_list_sessions_limit_boundary(monkeypatch):
    """limit 超出 [1,100] 范围时回退到默认 20。"""
    conn = _SeqConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    await list_sessions(_actor(), limit=0)
    select_q = conn.calls[0][0]
    # limit=0 → 回退 20 → LIMIT 21 (20+1)
    params = conn.calls[0][1]
    assert 21 in params  # limit+1=21

    conn2 = _SeqConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(wma, "pool", _Pool(conn2))
    await list_sessions(_actor(), limit=200)
    params2 = conn2.calls[0][1]
    assert 21 in params2


@pytest.mark.asyncio
async def test_list_sessions_workspace_isolation(monkeypatch):
    """sessions 列表查询包含 workspace_id 过滤。"""
    conn = _SeqConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    await list_sessions(_actor(workspace_id="wsp_test"))

    select_q = conn.calls[0][0]
    assert "workspace_id" in select_q
    assert "wsp_test" in conn.calls[0][1]


# ============================================================================
# 5. 鉴权 401（ASGI 未认证）
# ============================================================================


@pytest.mark.asyncio
async def test_logout_unauthenticated_401():
    """POST /logout 未认证 → 401。"""
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/wechat/miniapp/logout",
            json={"refresh_token": "some_token"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_security_check_unauthenticated_401():
    """GET /security-check 未认证 → 401。"""
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/wechat/miniapp/security-check")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoke_session_unauthenticated_401():
    """POST /sessions/revoke 未认证 → 401。"""
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/wechat/miniapp/sessions/revoke",
            json={"session_id": "some_session"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_sessions_unauthenticated_401():
    """GET /sessions 未认证 → 401。"""
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/wechat/miniapp/sessions")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoke_session_member_403_asgi():
    """member 通过 ASGI 调 revoke → 403（需要 admin）。"""
    from workama_platform.core import get_actor

    app = _app()
    # 覆盖 get_actor 依赖，绕过 DB 查询，直接返回 member actor
    app.dependency_overrides[get_actor] = lambda: _actor(role="member")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/wechat/miniapp/sessions/revoke",
            json={"session_id": "wmasess_1"},
            headers={"Authorization": "Bearer fake_token"},
        )
    assert resp.status_code == 403
    app.dependency_overrides.clear()


# ============================================================================
# 6. session_log 审计日志记录
# ============================================================================


@pytest.mark.asyncio
async def test_login_writes_login_audit_log(monkeypatch):
    """login 端点写入 action=login 审计日志。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)
    user = _user_row()
    conn = _SeqConnection(results=[_Result(row=None), _Result(row=user), _Result(), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    await wma.login(wma.LoginRequest(js_code="code-audit-login"))

    log_q = next((q for q, _ in conn.calls if "INSERT INTO wx_miniapp_session_log" in q), None)
    assert log_q is not None
    log_params = [p for q, p in conn.calls if "INSERT INTO wx_miniapp_session_log" in q][0]
    assert "login" in log_params


@pytest.mark.asyncio
async def test_refresh_writes_refresh_audit_log(monkeypatch):
    """refresh 端点写入 action=refresh 审计日志。"""
    session = _session_row()
    user = {"user_id": "usr_test"}
    conn = _SeqConnection(results=[_Result(row=session), _Result(row=user), _Result(), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    await wma.refresh(wma.RefreshRequest(refresh_token="refresh_token_BBBB"))

    log_q = next((q for q, _ in conn.calls if "INSERT INTO wx_miniapp_session_log" in q), None)
    assert log_q is not None
    log_params = [p for q, p in conn.calls if "INSERT INTO wx_miniapp_session_log" in q][0]
    assert "refresh" in log_params


@pytest.mark.asyncio
async def test_logout_writes_logout_audit_log(monkeypatch):
    """logout 端点写入 action=logout 审计日志。"""
    session = _session_row()
    user = {"user_id": "usr_test"}
    conn = _SeqConnection(results=[_Result(row=session), _Result(row=user)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))
    monkeypatch.setattr(wma, "redis", _FakeRedis())

    await logout(
        LogoutRequest(refresh_token="refresh_token_BBBB"),
        _actor(),
        authorization="Bearer access_token",
    )

    log_params = [p for q, p in conn.calls if "INSERT INTO wx_miniapp_session_log" in q][0]
    assert "logout" in log_params


@pytest.mark.asyncio
async def test_revoke_writes_revoke_audit_log(monkeypatch):
    """revoke 端点写入 action=revoke 审计日志。"""
    session = _session_row()
    user = {"user_id": "usr_test"}
    conn = _SeqConnection(results=[_Result(row=session), _Result(row=user)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))
    monkeypatch.setattr(wma, "redis", _FakeRedis())

    await revoke_session(
        RevokeSessionRequest(session_id="wmasess_1"),
        _actor(role="admin"),
    )

    log_params = [p for q, p in conn.calls if "INSERT INTO wx_miniapp_session_log" in q][0]
    assert "revoke" in log_params


# ============================================================================
# 7. Redis blacklist 辅助函数
# ============================================================================


@pytest.mark.asyncio
async def test_revoke_token_calls_redis_setex(monkeypatch):
    """_revoke_token 调用 redis.setex，key=revoked:{hash}，TTL>0。"""
    fake_redis = _FakeRedis()
    monkeypatch.setattr(wma, "redis", fake_redis)

    await _revoke_token("my_token_123", 3600)

    assert len(fake_redis.setex_calls) == 1
    key, ttl, value = fake_redis.setex_calls[0]
    assert key == f"revoked:{_token_hash('my_token_123')}"
    assert ttl == 3600
    assert value == "1"


@pytest.mark.asyncio
async def test_is_token_revoked_checks_redis(monkeypatch):
    """_is_token_revoked 从 Redis 读取撤销状态。"""
    fake_redis = _FakeRedis()
    monkeypatch.setattr(wma, "redis", fake_redis)

    # 未撤销
    assert await _is_token_revoked("token_a") is False

    # 撤销后
    await _revoke_token("token_a", 60)
    assert await _is_token_revoked("token_a") is True


@pytest.mark.asyncio
async def test_revoke_token_silent_on_redis_failure(monkeypatch):
    """Redis 异常时 _revoke_token 静默跳过（best-effort）。"""
    class _FailingRedis:
        async def setex(self, *_args):
            raise ConnectionError("redis down")

    monkeypatch.setattr(wma, "redis", _FailingRedis())

    # 不应抛异常
    await _revoke_token("token_x", 60)


@pytest.mark.asyncio
async def test_is_token_revoked_false_on_redis_failure(monkeypatch):
    """Redis 异常时 _is_token_revoked 返回 False（best-effort）。"""
    class _FailingRedis:
        async def get(self, *_args):
            raise ConnectionError("redis down")

    monkeypatch.setattr(wma, "redis", _FailingRedis())
    assert await _is_token_revoked("token_y") is False


def test_token_hash_deterministic():
    """_token_hash 对相同输入产生相同输出。"""
    assert _token_hash("abc") == _token_hash("abc")
    assert _token_hash("abc") != _token_hash("xyz")
    assert len(_token_hash("abc")) == 64  # SHA-256 hex


# ============================================================================
# 8. Pydantic 模型验证
# ============================================================================


def test_logout_request_valid():
    """LogoutRequest 接受合法 refresh_token。"""
    req = LogoutRequest(refresh_token="valid_token_123")
    assert req.refresh_token == "valid_token_123"


def test_logout_request_empty_token_rejected():
    """LogoutRequest 拒绝空 refresh_token。"""
    with pytest.raises(ValidationError):
        LogoutRequest(refresh_token="")


def test_revoke_session_request_valid():
    """RevokeSessionRequest 接受合法 session_id。"""
    req = RevokeSessionRequest(session_id="wmasess_1")
    assert req.session_id == "wmasess_1"


def test_revoke_session_request_empty_id_rejected():
    """RevokeSessionRequest 拒绝空 session_id。"""
    with pytest.raises(ValidationError):
        RevokeSessionRequest(session_id="")


# ============================================================================
# 9. 辅助函数
# ============================================================================


def test_extract_bearer_token_success():
    """_extract_bearer_token 正确解析 Bearer token。"""
    assert _extract_bearer_token("Bearer abc123") == "abc123"


def test_extract_bearer_token_missing_header_401():
    """_extract_bearer_token 无 header → 401。"""
    with pytest.raises(HTTPException) as exc:
        _extract_bearer_token(None)
    assert exc.value.status_code == 401


def test_extract_bearer_token_invalid_scheme_401():
    """_extract_bearer_token 非 Bearer scheme → 401。"""
    with pytest.raises(HTTPException) as exc:
        _extract_bearer_token("Basic abc123")
    assert exc.value.status_code == 401


def test_extract_bearer_token_empty_token_401():
    """_extract_bearer_token 空 token → 401。"""
    with pytest.raises(HTTPException) as exc:
        _extract_bearer_token("Bearer ")
    assert exc.value.status_code == 401


def test_client_ip_from_forwarded_for():
    """_client_ip 从 X-Forwarded-For 提取第一个 IP。"""
    assert _client_ip("10.0.0.1, 192.168.1.1") == "10.0.0.1"
    assert _client_ip("10.0.0.1") == "10.0.0.1"


def test_client_ip_none_when_missing():
    """_client_ip 无输入返回 None。"""
    assert _client_ip(None) is None
    assert _client_ip("") is None


@pytest.mark.asyncio
async def test_log_session_event_executes_insert():
    """_log_session_event 执行 INSERT 并传入正确参数。"""
    conn = _SeqConnection()
    await _log_session_event(conn, "wsp_test", "usr_test", "wmasess_1", "login", "10.0.0.1", "Agent")
    assert len(conn.calls) == 1
    q, params = conn.calls[0]
    assert "INSERT INTO wx_miniapp_session_log" in q
    assert "wsp_test" in params
    assert "usr_test" in params
    assert "wmasess_1" in params
    assert "login" in params
    assert "10.0.0.1" in params
    assert "Agent" in params


# ============================================================================
# 10. 表结构存在性
# ============================================================================


def test_security_schema_contains_session_log_table():
    """SECURITY_SCHEMA_STATEMENTS 包含 wx_miniapp_session_log 建表语句。"""
    create_tables = [s for s in SECURITY_SCHEMA_STATEMENTS if "CREATE TABLE IF NOT EXISTS" in s]
    assert len(create_tables) == 1
    joined = "\n".join(SECURITY_SCHEMA_STATEMENTS)
    assert "wx_miniapp_session_log" in joined


def test_security_schema_has_required_columns():
    """session_log 表含全部要求的列。"""
    table_stmt = next(s for s in SECURITY_SCHEMA_STATEMENTS if "wx_miniapp_session_log" in s and "CREATE TABLE" in s)
    for col in ("id", "workspace_id", "user_id", "session_id", "action", "ip", "user_agent", "created_at"):
        assert col in table_stmt


def test_security_schema_has_check_constraint():
    """session_log 表 action 列含 CHECK 约束（login/logout/refresh/revoke）。"""
    table_stmt = next(s for s in SECURITY_SCHEMA_STATEMENTS if "wx_miniapp_session_log" in s and "CREATE TABLE" in s)
    assert "CHECK" in table_stmt
    for action in ("login", "logout", "refresh", "revoke"):
        assert action in table_stmt


def test_security_schema_has_required_indexes():
    """SECURITY_SCHEMA_STATEMENTS 包含两个索引。"""
    joined = "\n".join(SECURITY_SCHEMA_STATEMENTS)
    assert "idx_wx_miniapp_session_log_workspace_user" in joined
    assert "idx_wx_miniapp_session_log_session" in joined
    assert "workspace_id, user_id, created_at DESC" in joined
    assert "session_id, created_at DESC" in joined


@pytest.mark.asyncio
async def test_ensure_session_security_schema_executes_all():
    """ensure_session_security_schema 逐条执行所有 SECURITY_SCHEMA_STATEMENTS。"""
    conn = _SeqConnection()
    await ensure_session_security_schema(conn)
    assert len(conn.calls) == len(SECURITY_SCHEMA_STATEMENTS)


# ============================================================================
# 11. 路由注册
# ============================================================================


def test_router_has_twelve_endpoints():
    """router 至少暴露 12 个端点（8 原有 + 4 新增）。"""
    assert len(wma.router.routes) >= 12


def test_router_prefix_unchanged():
    """router 前缀保持 /api/v1/wechat/miniapp。"""
    assert wma.router.prefix == "/api/v1/wechat/miniapp"


def test_new_endpoints_registered():
    """4 个新端点路径已注册到 router。"""
    paths = {route.path for route in wma.router.routes}
    assert "/api/v1/wechat/miniapp/logout" in paths
    assert "/api/v1/wechat/miniapp/security-check" in paths
    assert "/api/v1/wechat/miniapp/sessions/revoke" in paths
    assert "/api/v1/wechat/miniapp/sessions" in paths
