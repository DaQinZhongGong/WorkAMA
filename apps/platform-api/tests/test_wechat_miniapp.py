"""微信小程序登录闭环模块单元 + 端点测试。

覆盖范围：
- 登录：code2Session 成功/新建用户/已有用户更新/session_key 更新/token 签发/
        未配置 appid 走 mock/mock openid 确定性/code 为空拒绝/微信 API 失败 502/网络错误 502
- 刷新 token：成功/refresh_token 无效 401/refresh_token 过期 401
- 会话查询：成功/session 过期 401/session 不存在 404/openid 脱敏
- 订阅授权：记录成功/重复订阅幂等/多模板/不存在 openid 404
- 发送订阅消息：成功/无订阅记录跳过/未配置 appid mock 成功/微信 API 失败/非 admin 403
- 模板管理：添加/列表/删除/不存在 404/跨 workspace 403/admin 鉴权
- session_key 不泄露：登录响应与 _user_view 不含 session_key
- 辅助函数：_user_view/_session_view/_template_view/_mask_openid
- SCHEMA_STATEMENTS 包含 4 张表与索引；ensure_schema 执行全部语句
- 路由注册：prefix 与端点数量

所有测试使用 fake pool/connection，对 urllib.request 调用用 monkeypatch mock，
不依赖真实 DB / 网络 / 微信 API。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from workama_platform.core import Actor, get_actor
from workama_platform.modules import wechat_miniapp as wma
from workama_platform.modules.wechat_miniapp import (
    LoginRequest,
    SCHEMA_STATEMENTS,
    _call_code2session,
    _get_wechat_access_token,
    _mask_openid,
    _mock_openid,
    _send_subscribe_message,
    _session_view,
    _template_view,
    _user_view,
    _wechat_configured,
    create_template,
    delete_template,
    ensure_wechat_miniapp_schema,
    get_session,
    list_templates,
    login,
    refresh,
    send_notify,
    subscribe,
)


# ============================================================================
# 测试辅助：fake pool / connection / result
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


def _user_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "wmau_1",
        "workspace_id": "wsp_test",
        "openid": "oABCDEFGHIJKLMNopqrstuvwxyz1234567890",
        "unionid": "u_123",
        "user_id": "usr_link_1",
        "nickname": "小王",
        "avatar_url": "https://example.com/a.png",
        "session_key": "SECRET_SESSION_KEY",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _session_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "wmasess_1",
        "workspace_id": "wsp_test",
        "openid": "oABCDEFGHIJKLMNopqrstuvwxyz1234567890",
        "session_token": "sess_token AAAA",
        "refresh_token": "refresh_token BBBB",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "refresh_expires_at": datetime.now(UTC) + timedelta(days=1),
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _sub_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "wmasub_1",
        "workspace_id": "wsp_test",
        "openid": "oABCDEFGHIJKLMNopqrstuvwxyz1234567890",
        "template_id": "tpl_001",
        "subscribed_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _template_row(**overrides) -> dict[str, Any]:
    base = {
        "id": "wmatpl_1",
        "workspace_id": "wsp_test",
        "template_id": "tpl_001",
        "title": "回复提醒",
        "description": "有人回复时提醒",
        "scene": "chat_reply",
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(wma.router)
    return app


def _fake_urlopen(payload: dict[str, Any]):
    """构造一个返回固定 JSON payload 的 urlopen 替身。"""

    class _Resp:
        def __init__(self, data):
            self._data = data

        def read(self):
            return json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return _Resp(payload)


# ============================================================================
# 1. 登录端点
# ============================================================================


@pytest.mark.asyncio
async def test_login_new_user_creates_user_and_session(monkeypatch):
    """新建用户：SELECT 无记录 → INSERT user → INSERT session，返回完整 token 集合。"""
    # 未配置 appid/secret，走 mock code2session
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)

    user = _user_row()
    conn = _SeqConnection(results=[_Result(row=None), _Result(row=user), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await login(LoginRequest(js_code="code-001", workspace_id="wsp_test"))

    assert result["access_token"]
    assert result["refresh_token"]
    assert result["session_token"]
    assert result["token_type"] == "Bearer"
    assert result["expires_in"] == 900
    assert result["openid"].startswith("mock_openid_")
    # 查询带 openid
    select_q = conn.calls[0][0]
    assert "SELECT * FROM wechat_miniapp_user WHERE openid" in select_q
    # INSERT user
    insert_user_q = next(q for q, _ in conn.calls if "INSERT INTO wechat_miniapp_user" in q)
    assert insert_user_q
    # INSERT session
    insert_sess_q = next(q for q, _ in conn.calls if "INSERT INTO wechat_miniapp_session" in q)
    assert insert_sess_q


@pytest.mark.asyncio
async def test_login_existing_user_updates_session_key(monkeypatch):
    """已有用户：UPDATE 而非 INSERT，且更新 session_key。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)

    existing = _user_row(session_key="OLD_KEY")
    updated = _user_row(session_key="mock_session_key")
    conn = _SeqConnection(results=[_Result(row=existing), _Result(row=updated), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await login(LoginRequest(js_code="code-002"))

    # UPDATE 语句存在并包含 session_key 赋值
    update_q = next(q for q, _ in conn.calls if "UPDATE wechat_miniapp_user" in q)
    assert "session_key" in update_q
    assert "RETURNING" in update_q
    assert "session_key" not in result  # 顶层响应不含 session_key


@pytest.mark.asyncio
async def test_login_issues_access_token(monkeypatch):
    """登录签发的 access_token 为非空字符串（JWT）。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)
    conn = _SeqConnection(results=[_Result(row=None), _Result(row=_user_row()), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await login(LoginRequest(js_code="code-003"))
    assert isinstance(result["access_token"], str)
    assert len(result["access_token"]) > 10
    # JWT 通常包含两个点
    assert result["access_token"].count(".") >= 2


@pytest.mark.asyncio
async def test_login_mock_openid_deterministic(monkeypatch):
    """mock 模式下相同 js_code 产生相同 openid（确定性）。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)
    a = _call_code2session("same-code")
    b = _call_code2session("same-code")
    assert a["openid"] == b["openid"]
    assert a["openid"].startswith("mock_openid_")


@pytest.mark.asyncio
async def test_login_mock_mode_when_appid_not_configured(monkeypatch):
    """未配置 appid/secret 时走 mock：openid 以 mock_openid_ 开头。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)
    conn = _SeqConnection(results=[_Result(row=None), _Result(row=_user_row()), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await login(LoginRequest(js_code="code-004"))
    assert result["openid"].startswith("mock_openid_")
    assert _wechat_configured() is False


def test_login_empty_js_code_rejected():
    """js_code 为空被 Pydantic 拒绝（min_length=1）。"""
    with pytest.raises(ValidationError):
        LoginRequest(js_code="")


@pytest.mark.asyncio
async def test_login_code2session_wechat_api_errcode_502(monkeypatch):
    """已配置 appid/secret 时，微信返回 errcode → 502。"""
    monkeypatch.setenv("WECHAT_MINIAPP_APPID", "wxabc")
    monkeypatch.setenv("WECHAT_MINIAPP_SECRET", "secret")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _fake_urlopen({"errcode": 40029, "errmsg": "invalid code"}),
    )
    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(js_code="bad-code"))
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_login_code2session_network_error_502(monkeypatch):
    """已配置 appid/secret 时，网络异常 → 502。"""
    import urllib.error

    monkeypatch.setenv("WECHAT_MINIAPP_APPID", "wxabc")
    monkeypatch.setenv("WECHAT_MINIAPP_SECRET", "secret")

    def _raise(*_a, **_k):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    with pytest.raises(HTTPException) as exc:
        await login(LoginRequest(js_code="any-code"))
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_login_response_does_not_leak_session_key(monkeypatch):
    """登录响应顶层与 user 视图均不含 session_key。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)
    conn = _SeqConnection(results=[_Result(row=None), _Result(row=_user_row()), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await login(LoginRequest(js_code="code-005"))
    assert "session_key" not in result
    assert "session_key" not in result["user"]


@pytest.mark.asyncio
async def test_login_code2session_success_when_configured(monkeypatch):
    """已配置 appid/secret 且微信返回正常时，返回真实 openid/session_key。"""
    monkeypatch.setenv("WECHAT_MINIAPP_APPID", "wxabc")
    monkeypatch.setenv("WECHAT_MINIAPP_SECRET", "secret")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _fake_urlopen(
            {"openid": "real_openid_xyz", "session_key": "real_sk", "unionid": "u_real"}
        ),
    )
    data = _call_code2session("real-code")
    assert data["openid"] == "real_openid_xyz"
    assert data["session_key"] == "real_sk"
    assert data["unionid"] == "u_real"


# ============================================================================
# 2. 刷新 token 端点
# ============================================================================


@pytest.mark.asyncio
async def test_refresh_success_returns_new_tokens(monkeypatch):
    """refresh 成功：返回新的 access_token / refresh_token / session_token。"""
    session = _session_row()
    user = {"user_id": "usr_link_1"}
    conn = _SeqConnection(results=[_Result(row=session), _Result(row=user), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await refresh(wma.RefreshRequest(refresh_token="refresh_token BBBB"))
    assert result["access_token"]
    assert result["refresh_token"] != "refresh_token BBBB"
    assert result["session_token"]
    assert result["expires_in"] == 900
    # UPDATE session 语句存在
    update_q = next(q for q, _ in conn.calls if "UPDATE wechat_miniapp_session" in q)
    assert update_q


@pytest.mark.asyncio
async def test_refresh_invalid_token_401(monkeypatch):
    """refresh_token 无效 → 401。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await refresh(wma.RefreshRequest(refresh_token="not-a-real-token"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_refresh_expired_token_401(monkeypatch):
    """refresh_token 过期 → 401。"""
    session = _session_row(refresh_expires_at=datetime.now(UTC) - timedelta(hours=1))
    conn = _SeqConnection(results=[_Result(row=session)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await refresh(wma.RefreshRequest(refresh_token="refresh_token BBBB"))
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


# ============================================================================
# 3. 会话查询端点
# ============================================================================


@pytest.mark.asyncio
async def test_get_session_success(monkeypatch):
    """会话查询成功：返回脱敏 openid、workspace、session_active。"""
    session = _session_row()
    user = _user_row()
    conn = _SeqConnection(results=[_Result(row=session), _Result(row=user)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await get_session("sess_token AAAA")
    assert result["session_active"] is True
    assert result["workspace_id"] == "wsp_test"
    assert result["user"] is not None


@pytest.mark.asyncio
async def test_get_session_expired_401(monkeypatch):
    """session 过期 → 401。"""
    session = _session_row(expires_at=datetime.now(UTC) - timedelta(hours=1))
    conn = _SeqConnection(results=[_Result(row=session)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await get_session("sess_token AAAA")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_session_not_found_404(monkeypatch):
    """session 不存在 → 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await get_session("unknown-token")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_session_openid_masked(monkeypatch):
    """会话查询返回的 openid 已脱敏，不等于原始 openid 且含 ***。"""
    session = _session_row(openid="oABCDEFGHIJKLMNopqrstuvwxyz1234567890")
    conn = _SeqConnection(results=[_Result(row=session), _Result(row=None)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await get_session("sess_token AAAA")
    assert result["openid"] != "oABCDEFGHIJKLMNopqrstuvwxyz1234567890"
    assert "***" in result["openid"]


# ============================================================================
# 4. 订阅授权端点
# ============================================================================


@pytest.mark.asyncio
async def test_subscribe_records_templates(monkeypatch):
    """订阅授权：记录 template_ids，返回 recorded 列表。"""
    session = _session_row()
    conn = _SeqConnection(results=[_Result(row=session), _Result(), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await subscribe(wma.SubscribeRequest(template_ids=["tpl_001", "tpl_002"]), "sess_token AAAA")
    assert result["count"] == 2
    assert "tpl_001" in result["recorded"]
    assert "tpl_002" in result["recorded"]


@pytest.mark.asyncio
async def test_subscribe_idempotent_on_conflict(monkeypatch):
    """订阅写入使用 ON CONFLICT DO NOTHING，保证幂等。"""
    session = _session_row()
    conn = _SeqConnection(results=[_Result(row=session), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    await subscribe(wma.SubscribeRequest(template_ids=["tpl_001"]), "sess_token AAAA")
    insert_q = next(q for q, _ in conn.calls if "INSERT INTO wechat_miniapp_subscription" in q)
    assert "ON CONFLICT" in insert_q
    assert "DO NOTHING" in insert_q


@pytest.mark.asyncio
async def test_subscribe_multiple_templates(monkeypatch):
    """多模板一次上报，全部记录。"""
    session = _session_row()
    conn = _SeqConnection(
        results=[_Result(row=session), _Result(), _Result(), _Result()]
    )
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await subscribe(
        wma.SubscribeRequest(template_ids=["t1", "t2", "t3"]), "sess_token AAAA"
    )
    assert result["count"] == 3
    inserts = [q for q, _ in conn.calls if "INSERT INTO wechat_miniapp_subscription" in q]
    assert len(inserts) == 3


@pytest.mark.asyncio
async def test_subscribe_session_not_found_404(monkeypatch):
    """订阅时 session 不存在 → 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await subscribe(wma.SubscribeRequest(template_ids=["tpl_001"]), "unknown-token")
    assert exc.value.status_code == 404


# ============================================================================
# 5. 发送订阅消息端点
# ============================================================================


@pytest.mark.asyncio
async def test_notify_success_mock(monkeypatch):
    """发送订阅消息成功（mock 模式）：sent=1。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)
    conn = _SeqConnection(results=[_Result(row=_sub_row())])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await send_notify(
        wma.NotifyRequest(openid="oABC1234567890", template_id="tpl_001", data={"thing1": {"value": "x"}}),
        _actor(),
    )
    assert result["sent"] == 1
    assert result["skipped"] == 0
    assert result["response"]["mock"] is True


@pytest.mark.asyncio
async def test_notify_skips_when_no_subscription(monkeypatch):
    """无订阅记录时跳过发送：sent=0, skipped=1。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await send_notify(
        wma.NotifyRequest(openid="oABC1234567890", template_id="tpl_001", data={}),
        _actor(),
    )
    assert result["sent"] == 0
    assert result["skipped"] == 1
    assert "no subscription" in result["reason"]


@pytest.mark.asyncio
async def test_notify_mock_success_when_not_configured(monkeypatch):
    """未配置 appid/secret 时，发送走 mock 成功路径。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)
    assert _wechat_configured() is False
    conn = _SeqConnection(results=[_Result(row=_sub_row())])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await send_notify(
        wma.NotifyRequest(openid="oABC1234567890", template_id="tpl_001"),
        _actor(),
    )
    assert result["response"]["errcode"] == 0


@pytest.mark.asyncio
async def test_notify_wechat_api_failure_502(monkeypatch):
    """已配置 appid/secret 时，微信 subscribeMessage.send 返回 errcode → 502。"""
    monkeypatch.setenv("WECHAT_MINIAPP_APPID", "wxabc")
    monkeypatch.setenv("WECHAT_MINIAPP_SECRET", "secret")

    def fake_urlopen(url, *args, **kwargs):
        # urlopen 可能接收 str 或 urllib.request.Request，统一转成 url 字符串
        url_str = url if isinstance(url, str) else getattr(url, "full_url", str(url))
        if "cgi-bin/token" in url_str:
            return _fake_urlopen({"access_token": "real_token", "expires_in": 7200})
        if "subscribe/send" in url_str:
            return _fake_urlopen({"errcode": 43101, "errmsg": "user refuse"})
        return _fake_urlopen({"errcode": 0})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    conn = _SeqConnection(results=[_Result(row=_sub_row())])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await send_notify(
            wma.NotifyRequest(openid="oABC1234567890", template_id="tpl_001"),
            _actor(),
        )
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_notify_non_admin_403():
    """非 admin/owner 调用发送 → 403。"""
    with pytest.raises(HTTPException) as exc:
        await send_notify(
            wma.NotifyRequest(openid="oABC1234567890", template_id="tpl_001"),
            _actor(role="member"),
        )
    assert exc.value.status_code == 403


# ============================================================================
# 6. 模板管理端点
# ============================================================================


@pytest.mark.asyncio
async def test_create_template_success(monkeypatch):
    """添加模板成功：返回模板视图。"""
    conn = _SeqConnection(results=[_Result(row=_template_row())])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await create_template(
        wma.TemplateCreate(template_id="tpl_001", title="回复提醒"),
        _actor(),
    )
    assert result["template_id"] == "tpl_001"
    assert result["title"] == "回复提醒"
    insert_q = next(q for q, _ in conn.calls if "INSERT INTO wechat_miniapp_template" in q)
    assert "wsp_test" in conn.calls[0][1]


@pytest.mark.asyncio
async def test_create_template_non_admin_403():
    """member 添加模板 → 403。"""
    with pytest.raises(HTTPException) as exc:
        await create_template(
            wma.TemplateCreate(template_id="tpl_001"),
            _actor(role="member"),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_templates_returns_workspace_items(monkeypatch):
    """模板列表只返回当前 workspace 的模板。"""
    conn = _SeqConnection(
        results=[_Result(rows=[_template_row(id="wmatpl_1"), _template_row(id="wmatpl_2")])]
    )
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await list_templates(_actor())
    assert result["count"] == 2
    assert len(result["items"]) == 2
    select_q = conn.calls[0][0]
    assert "workspace_id" in select_q
    assert "wsp_test" in conn.calls[0][1]


@pytest.mark.asyncio
async def test_list_templates_empty(monkeypatch):
    """无模板时返回空列表。"""
    conn = _SeqConnection(results=[_Result(rows=[])])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await list_templates(_actor())
    assert result["count"] == 0
    assert result["items"] == []


@pytest.mark.asyncio
async def test_delete_template_success(monkeypatch):
    """删除模板成功：返回 deleted=True。"""
    conn = _SeqConnection(results=[_Result(row=_template_row()), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    result = await delete_template("tpl_001", _actor())
    assert result["deleted"] is True
    assert result["template_id"] == "tpl_001"
    delete_q = next(q for q, _ in conn.calls if "DELETE FROM wechat_miniapp_template" in q)
    assert delete_q


@pytest.mark.asyncio
async def test_delete_template_not_found_404(monkeypatch):
    """删除不存在的模板 → 404。"""
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await delete_template("tpl_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_template_cross_workspace_403(monkeypatch):
    """跨 workspace 删除模板 → 403。"""
    conn = _SeqConnection(results=[_Result(row=_template_row(workspace_id="wsp_other"))])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await delete_template("tpl_001", _actor(workspace_id="wsp_test"))
    assert exc.value.status_code == 403
    assert "another workspace" in exc.value.detail


@pytest.mark.asyncio
async def test_delete_template_non_admin_403():
    """member 删除模板 → 403。"""
    with pytest.raises(HTTPException) as exc:
        await delete_template("tpl_001", _actor(role="member"))
    assert exc.value.status_code == 403


# ============================================================================
# 7. session_key 不泄露 & 辅助函数
# ============================================================================


def test_user_view_excludes_session_key():
    """_user_view 剔除 session_key 并对 openid 脱敏。"""
    view = _user_view(_user_row())
    assert "session_key" not in view
    assert "openid" not in view
    assert "openid_masked" in view
    assert "***" in view["openid_masked"]
    assert view["user_id"] == "usr_link_1"
    assert view["nickname"] == "小王"


def test_session_view_excludes_raw_tokens():
    """_session_view 不含原始 token，openid 脱敏。"""
    view = _session_view(_session_row())
    assert "session_token" not in view
    assert "refresh_token" not in view
    assert "session_key" not in view
    assert "openid" not in view
    assert "***" in view["openid_masked"]
    assert view["workspace_id"] == "wsp_test"


def test_template_view_fields():
    """_template_view 返回完整模板字段。"""
    view = _template_view(_template_row())
    assert view["template_id"] == "tpl_001"
    assert view["title"] == "回复提醒"
    assert view["scene"] == "chat_reply"
    assert view["workspace_id"] == "wsp_test"


def test_mask_openid_long():
    """长 openid 保留首尾各 4 位。"""
    masked = _mask_openid("oABCDEFGHIJKLMNopqrstuvwxyz1234567890")
    assert masked == "oABC***7890"


def test_mask_openid_short():
    """短 openid 只保留前 2 位 + ***。"""
    assert _mask_openid("short") == "sh***"


def test_mask_openid_empty():
    """空 openid 返回空串。"""
    assert _mask_openid("") == ""


def test_wechat_configured_false_without_env(monkeypatch):
    """未配置环境变量时 _wechat_configured 返回 False。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)
    assert _wechat_configured() is False


def test_wechat_configured_true_with_env(monkeypatch):
    """配置了两个环境变量时 _wechat_configured 返回 True。"""
    monkeypatch.setenv("WECHAT_MINIAPP_APPID", "wxabc")
    monkeypatch.setenv("WECHAT_MINIAPP_SECRET", "secret")
    assert _wechat_configured() is True


def test_mock_openid_deterministic():
    """_mock_openid 对相同输入产生相同输出。"""
    assert _mock_openid("code-x") == _mock_openid("code-x")
    assert _mock_openid("code-x") != _mock_openid("code-y")


def test_get_wechat_access_token_mock_when_not_configured(monkeypatch):
    """未配置时 _get_wechat_access_token 返回 mock token。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)
    assert _get_wechat_access_token() == "mock_access_token"


def test_get_wechat_access_token_success_when_configured(monkeypatch):
    """已配置时通过 urllib 获取真实 access_token。"""
    monkeypatch.setenv("WECHAT_MINIAPP_APPID", "wxabc")
    monkeypatch.setenv("WECHAT_MINIAPP_SECRET", "secret")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _fake_urlopen({"access_token": "real_token", "expires_in": 7200}),
    )
    assert _get_wechat_access_token() == "real_token"


def test_send_subscribe_message_mock_when_not_configured(monkeypatch):
    """未配置时 _send_subscribe_message 返回 mock 成功。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)
    result = _send_subscribe_message("openid", "tpl", {"x": {"value": "y"}}, None)
    assert result["errcode"] == 0
    assert result["mock"] is True


def test_require_admin_allows_owner():
    """owner 通过 admin 校验。"""
    wma._require_admin(_actor(role="owner"))


def test_require_admin_allows_admin():
    """admin 通过 admin 校验。"""
    wma._require_admin(_actor(role="admin"))


def test_require_admin_rejects_member():
    """member 被拒绝。"""
    with pytest.raises(HTTPException) as exc:
        wma._require_admin(_actor(role="member"))
    assert exc.value.status_code == 403


# ============================================================================
# 8. SCHEMA_STATEMENTS 与路由注册
# ============================================================================


def test_schema_statements_contains_four_tables():
    """SCHEMA_STATEMENTS 包含 4 张表（CREATE TABLE）。"""
    create_tables = [s for s in SCHEMA_STATEMENTS if "CREATE TABLE IF NOT EXISTS" in s]
    assert len(create_tables) == 4
    joined = "\n".join(SCHEMA_STATEMENTS)
    assert "wechat_miniapp_user" in joined
    assert "wechat_miniapp_session" in joined
    assert "wechat_miniapp_subscription" in joined
    assert "wechat_miniapp_template" in joined


def test_schema_has_required_indexes_and_uniques():
    """SCHEMA 含必要的 UNIQUE 与 INDEX 声明。"""
    joined = "\n".join(SCHEMA_STATEMENTS)
    assert "UNIQUE(openid)" in joined  # user 表 openid 唯一
    assert "UNIQUE(openid, template_id)" in joined  # 订阅表联合唯一
    assert "session_token TEXT NOT NULL UNIQUE" in joined  # session_token 唯一
    assert "refresh_token TEXT NOT NULL UNIQUE" in joined  # refresh_token 唯一
    assert "idx_wechat_miniapp_user_workspace_openid" in joined  # INDEX(workspace_id, openid)
    assert "idx_wechat_miniapp_session_openid" in joined  # INDEX(openid)
    assert "idx_wechat_miniapp_template_workspace" in joined  # INDEX(workspace_id)


def test_schema_user_table_has_required_columns():
    """wechat_miniapp_user 表含任务要求的全部列。"""
    user_stmt = next(s for s in SCHEMA_STATEMENTS if "wechat_miniapp_user" in s and "CREATE TABLE" in s)
    for col in ("id", "workspace_id", "openid", "unionid", "user_id", "nickname",
                "avatar_url", "session_key", "created_at", "updated_at"):
        assert col in user_stmt


@pytest.mark.asyncio
async def test_ensure_schema_executes_all_statements():
    """ensure_wechat_miniapp_schema 逐条执行所有 SCHEMA_STATEMENTS。"""
    conn = _SeqConnection()
    await ensure_wechat_miniapp_schema(conn)
    assert len(conn.calls) == len(SCHEMA_STATEMENTS)


def test_router_prefix_correct():
    """router 挂载在 /api/v1/wechat/miniapp 前缀下。"""
    assert wma.router.prefix == "/api/v1/wechat/miniapp"


def test_router_has_eight_endpoints():
    """router 至少暴露 8 个端点。"""
    assert len(wma.router.routes) >= 8


# ============================================================================
# 9. 端点级未认证 401（ASGI）
# ============================================================================


@pytest.mark.asyncio
async def test_get_session_unauthenticated_401():
    """GET /session 未携带 Authorization → 401。"""
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/wechat/miniapp/session")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_notify_unauthenticated_401():
    """POST /notify 未认证 → 401（get_actor 拒绝）。"""
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/wechat/miniapp/notify",
            json={"openid": "oABC", "template_id": "tpl_001"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_template_unauthenticated_401():
    """POST /templates 未认证 → 401。"""
    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/wechat/miniapp/templates",
            json={"template_id": "tpl_001"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_endpoint_asgi_success(monkeypatch):
    """通过 ASGI 客户端调用 /login 端到端成功（mock 模式）。"""
    monkeypatch.delenv("WECHAT_MINIAPP_APPID", raising=False)
    monkeypatch.delenv("WECHAT_MINIAPP_SECRET", raising=False)
    user = _user_row()
    conn = _SeqConnection(results=[_Result(row=None), _Result(row=user), _Result()])
    monkeypatch.setattr(wma, "pool", _Pool(conn))

    app = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/wechat/miniapp/login",
            json={"js_code": "code-asgi"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert "session_key" not in body
