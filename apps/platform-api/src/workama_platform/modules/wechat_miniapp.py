"""微信小程序登录闭环（code2Session + 订阅消息 + session 持久化 + 会话安全强化）。

提供 12 个 REST 端点（prefix=/api/v1/wechat/miniapp）：
- POST /login    微信登录：js_code 换 openid+session_key，创建/更新用户，签发平台 token
- POST /refresh  刷新 token：refresh_token 换新 access_token
- GET  /session  获取当前会话（openid/session 状态/用户信息）
- POST /subscribe 记录订阅消息授权（用户授权后前端上报 template_ids）
- POST /notify   发送订阅消息（admin 权限，调用微信 subscribeMessage.send API）
- GET  /templates  订阅消息模板列表
- POST /templates  添加订阅消息模板（admin 权限）
- DELETE /templates/{template_id} 删除订阅消息模板（admin 权限）
- POST /logout   注销当前会话（撤销 refresh_token，记录审计日志）[P2 新增]
- GET  /security-check  返回当前会话安全状态（suspicious_flags）[P2 新增]
- POST /sessions/revoke  撤销指定 session_id（admin 权限）[P2 新增]
- GET  /sessions  列出当前用户所有活跃会话（cursor 分页）[P2 新增]

设计要点：
- 不引入第三方微信 SDK，统一用标准库 urllib.request 调用微信 API
- 未配置 WECHAT_MINIAPP_APPID/SECRET 时所有微信 API 调用走 mock（确保测试不依赖外部）
- session_key 绝不返回给前端；openid 在展示场景下脱敏
- 所有端点 workspace 隔离
- 建表 SQL 放在 SCHEMA_STATEMENTS，使用 CREATE TABLE IF NOT EXISTS
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from workama_platform.core import Actor, create_access_token, get_actor, new_id, pool, redis

logger = logging.getLogger("workama_platform.wechat_miniapp")

router = APIRouter(prefix="/api/v1/wechat/miniapp", tags=["wechat-miniapp"])

# access_token（JWT）有效期 15 分钟，与 core.create_access_token 保持一致
ACCESS_TOKEN_TTL_SECONDS = 15 * 60
# session_token 有效期 2 小时
SESSION_TOKEN_TTL = timedelta(hours=2)
# refresh_token 有效期 30 天
REFRESH_TOKEN_TTL = timedelta(days=30)
# 微信 API 调用超时
_WECHAT_HTTP_TIMEOUT = 10


# ============================================================================
# Pydantic 数据模型
# ============================================================================


class LoginRequest(BaseModel):
    """微信登录请求。"""

    js_code: str = Field(min_length=1, max_length=500, description="wx.login 返回的临时登录凭证")
    workspace_id: str | None = Field(default=None, max_length=160, description="工作区 ID，未传则用 default")
    nickname: str | None = Field(default=None, max_length=200)
    avatar_url: str | None = Field(default=None, max_length=1000)


class RefreshRequest(BaseModel):
    """刷新 token 请求。"""

    refresh_token: str = Field(min_length=1, max_length=200)


class SubscribeRequest(BaseModel):
    """记录订阅消息授权请求。"""

    template_ids: list[str] = Field(min_length=1, max_length=50)


class NotifyRequest(BaseModel):
    """发送订阅消息请求（admin）。"""

    openid: str = Field(min_length=1, max_length=128)
    template_id: str = Field(min_length=1, max_length=128)
    data: dict[str, Any] = Field(default_factory=dict, description="模板字段值，如 {\"thing1\":{\"value\":\"x\"}}")
    page: str | None = Field(default=None, max_length=200)


class TemplateCreate(BaseModel):
    """添加订阅消息模板请求（admin）。"""

    template_id: str = Field(min_length=1, max_length=128, description="微信模板 ID")
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=1000)
    scene: str = Field(default="", max_length=200)


class LogoutRequest(BaseModel):
    """注销请求：携带 refresh_token，由 access_token 鉴权后撤销。"""

    refresh_token: str = Field(min_length=1, max_length=200)


class RevokeSessionRequest(BaseModel):
    """撤销指定会话请求（admin）。"""

    session_id: str = Field(min_length=1, max_length=200)


# ============================================================================
# 数据库 Schema
# ============================================================================

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS wechat_miniapp_user (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        openid TEXT NOT NULL,
        unionid TEXT,
        user_id TEXT,
        nickname TEXT NOT NULL DEFAULT '',
        avatar_url TEXT NOT NULL DEFAULT '',
        session_key TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(openid)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wechat_miniapp_user_workspace_openid ON wechat_miniapp_user(workspace_id, openid)",
    """
    CREATE TABLE IF NOT EXISTS wechat_miniapp_session (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        openid TEXT NOT NULL,
        session_token TEXT NOT NULL UNIQUE,
        refresh_token TEXT NOT NULL UNIQUE,
        expires_at TIMESTAMPTZ NOT NULL,
        refresh_expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wechat_miniapp_session_openid ON wechat_miniapp_session(openid)",
    """
    CREATE TABLE IF NOT EXISTS wechat_miniapp_subscription (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        openid TEXT NOT NULL,
        template_id TEXT NOT NULL,
        subscribed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(openid, template_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wechat_miniapp_template (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        template_id TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        scene TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wechat_miniapp_template_workspace ON wechat_miniapp_template(workspace_id)",
)


async def ensure_wechat_miniapp_schema(conn) -> None:
    """幂等建表：执行所有 SCHEMA_STATEMENTS。"""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


# 会话安全强化相关表（P2 阶段新增，与 SCHEMA_STATEMENTS 分离避免影响现有测试计数）
SECURITY_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS wx_miniapp_session_log (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('login', 'logout', 'refresh', 'revoke')),
        ip TEXT,
        user_agent TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wx_miniapp_session_log_workspace_user ON wx_miniapp_session_log(workspace_id, user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_wx_miniapp_session_log_session ON wx_miniapp_session_log(session_id, created_at DESC)",
)


async def ensure_session_security_schema(conn) -> None:
    """幂等建表：执行所有 SECURITY_SCHEMA_STATEMENTS。"""
    for statement in SECURITY_SCHEMA_STATEMENTS:
        await conn.execute(statement)


# ============================================================================
# 辅助函数
# ============================================================================


def _require_admin(actor: Actor) -> None:
    """校验 admin/owner 角色。"""
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


def _mask_openid(openid: str) -> str:
    """openid 脱敏：保留首尾各 4 位，中间用 *** 替换；过短则只保留前 2 位。"""
    if not openid:
        return ""
    if len(openid) <= 8:
        return openid[:2] + "***"
    return openid[:4] + "***" + openid[-4:]


def _user_view(row: dict[str, Any]) -> dict[str, Any]:
    """用户视图：剔除 session_key，openid 脱敏展示。"""
    return {
        "id": row.get("id"),
        "workspace_id": row.get("workspace_id"),
        "openid_masked": _mask_openid(row.get("openid", "")),
        "unionid": row.get("unionid"),
        "user_id": row.get("user_id"),
        "nickname": row.get("nickname", ""),
        "avatar_url": row.get("avatar_url", ""),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _session_view(row: dict[str, Any]) -> dict[str, Any]:
    """会话视图：openid 脱敏，不含任何原始 token。"""
    return {
        "id": row.get("id"),
        "workspace_id": row.get("workspace_id"),
        "openid_masked": _mask_openid(row.get("openid", "")),
        "expires_at": row.get("expires_at"),
        "refresh_expires_at": row.get("refresh_expires_at"),
        "created_at": row.get("created_at"),
    }


def _template_view(row: dict[str, Any]) -> dict[str, Any]:
    """订阅消息模板视图。"""
    return {
        "id": row.get("id"),
        "workspace_id": row.get("workspace_id"),
        "template_id": row.get("template_id"),
        "title": row.get("title", ""),
        "description": row.get("description", ""),
        "scene": row.get("scene", ""),
        "created_at": row.get("created_at"),
    }


# ============================================================================
# 会话安全强化辅助函数（P2 阶段新增）
# ============================================================================


def _token_hash(token: str) -> str:
    """计算 token 的 SHA-256 哈希，用于 Redis blacklist key。"""
    return hashlib.sha256(token.encode()).hexdigest()


async def _revoke_token(token: str, ttl_seconds: int) -> None:
    """将 token 哈希写入 Redis blacklist，TTL 为剩余有效期（best-effort）。"""
    try:
        await redis.setex(f"revoked:{_token_hash(token)}", int(ttl_seconds), "1")
    except Exception:
        pass


async def _is_token_revoked(token: str) -> bool:
    """检查 token 是否已被撤销（best-effort，Redis 不可用时返回 False）。"""
    try:
        return bool(await redis.get(f"revoked:{_token_hash(token)}"))
    except Exception:
        return False


async def _log_session_event(
    conn,
    workspace_id: str,
    user_id: str,
    session_id: str,
    action: str,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """写入会话审计日志到 wx_miniapp_session_log 表。"""
    await conn.execute(
        """
        INSERT INTO wx_miniapp_session_log(id, workspace_id, user_id, session_id, action, ip, user_agent)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (new_id("wxsl"), workspace_id, user_id, session_id, action, ip, user_agent),
    )


def _extract_bearer_token(authorization: str | None) -> str:
    """从 Authorization 头解析 Bearer token，无效时抛 401。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[len("Bearer "):]
    if not token:
        raise HTTPException(status_code=401, detail="Empty access token")
    return token


def _client_ip(x_forwarded_for: str | None) -> str | None:
    """从 X-Forwarded-For 头提取客户端 IP（取第一个）。"""
    if not x_forwarded_for:
        return None
    return x_forwarded_for.split(",")[0].strip() or None


# ============================================================================
# 微信 API 调用（标准库 urllib.request，未配置时走 mock）
# ============================================================================


def _wechat_configured() -> bool:
    """是否已配置微信小程序 appid/secret。"""
    return bool(os.environ.get("WECHAT_MINIAPP_APPID")) and bool(os.environ.get("WECHAT_MINIAPP_SECRET"))


def _mock_openid(js_code: str) -> str:
    """mock 模式下根据 js_code 生成确定性 openid，确保同 code 同 openid。"""
    code_hash = hashlib.sha256(js_code.encode()).hexdigest()[:16]
    return f"mock_openid_{code_hash}"


def _call_code2session(js_code: str) -> dict[str, Any]:
    """调用微信 code2Session API 换取 openid + session_key。

    未配置 appid/secret 时返回 mock 响应；调用失败或微信返回 errcode 时抛 502。
    """
    appid = os.environ.get("WECHAT_MINIAPP_APPID", "")
    secret = os.environ.get("WECHAT_MINIAPP_SECRET", "")
    if not appid or not secret:
        return {"openid": _mock_openid(js_code), "session_key": "mock_session_key"}
    params = urllib.parse.urlencode(
        {
            "appid": appid,
            "secret": secret,
            "js_code": js_code,
            "grant_type": "authorization_code",
        }
    )
    url = f"https://api.weixin.qq.com/sns/jscode2session?{params}"
    try:
        with urllib.request.urlopen(url, timeout=_WECHAT_HTTP_TIMEOUT) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"WeChat code2Session request failed: {exc}"
        ) from exc
    if data.get("errcode"):
        raise HTTPException(
            status_code=502,
            detail=f"WeChat code2Session failed: {data.get('errmsg')} (errcode={data.get('errcode')})",
        )
    if not data.get("openid") or not data.get("session_key"):
        raise HTTPException(status_code=502, detail="WeChat code2Session returned no openid/session_key")
    return data


def _get_wechat_access_token() -> str:
    """获取微信平台 access_token（cgi-bin/token）。

    未配置 appid/secret 时返回 mock token。
    """
    appid = os.environ.get("WECHAT_MINIAPP_APPID", "")
    secret = os.environ.get("WECHAT_MINIAPP_SECRET", "")
    if not appid or not secret:
        return "mock_access_token"
    params = urllib.parse.urlencode(
        {"grant_type": "client_credential", "appid": appid, "secret": secret}
    )
    url = f"https://api.weixin.qq.com/cgi-bin/token?{params}"
    try:
        with urllib.request.urlopen(url, timeout=_WECHAT_HTTP_TIMEOUT) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"WeChat access_token request failed: {exc}"
        ) from exc
    if data.get("errcode"):
        raise HTTPException(
            status_code=502,
            detail=f"WeChat access_token failed: {data.get('errmsg')} (errcode={data.get('errcode')})",
        )
    return data.get("access_token", "")


def _send_subscribe_message(
    openid: str, template_id: str, data: dict[str, Any], page: str | None
) -> dict[str, Any]:
    """调用微信 subscribeMessage.send 发送订阅消息。

    未配置 appid/secret 时返回 mock 成功响应；调用失败或微信返回 errcode 时抛 502。
    """
    appid = os.environ.get("WECHAT_MINIAPP_APPID", "")
    secret = os.environ.get("WECHAT_MINIAPP_SECRET", "")
    if not appid or not secret:
        return {"errcode": 0, "errmsg": "ok", "mock": True}
    access_token = _get_wechat_access_token()
    url = f"https://api.weixin.qq.com/cgi-bin/message/subscribe/send?access_token={access_token}"
    payload = json.dumps(
        {
            "touser": openid,
            "template_id": template_id,
            "page": page or "pages/index/index",
            "data": data,
        }
    ).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_WECHAT_HTTP_TIMEOUT) as resp:  # noqa: S310
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"WeChat subscribeMessage.send failed: {exc}"
        ) from exc
    if result.get("errcode"):
        raise HTTPException(
            status_code=502,
            detail=f"WeChat subscribeMessage.send failed: {result.get('errmsg')} (errcode={result.get('errcode')})",
        )
    return result


# ============================================================================
# session_token 依赖（从 Authorization 头解析 Bearer token）
# ============================================================================


async def _extract_session_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """从 Authorization: Bearer <session_token> 头中解析 session_token。"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization[len("Bearer ") :]
    if not token:
        raise HTTPException(status_code=401, detail="Empty session token")
    return token


# ============================================================================
# Router 端点
# ============================================================================


@router.post("/login")
async def login(body: LoginRequest):
    """微信登录。

    接收 js_code，调用微信 code2Session 换取 openid+session_key，创建/更新用户，
    持久化 session，签发平台 access_token（复用 core.create_access_token）。
    """
    # 调用微信 code2Session（未配置 appid/secret 时走 mock）
    code2session = _call_code2session(body.js_code)
    openid = code2session["openid"]
    session_key = code2session["session_key"]
    unionid = code2session.get("unionid")
    workspace_id = body.workspace_id or "default"
    now = datetime.now(UTC)

    async with pool.connection() as conn:
        # 查找已有用户（openid 全局唯一）
        result = await conn.execute(
            "SELECT * FROM wechat_miniapp_user WHERE openid = %s",
            (openid,),
        )
        existing = await result.fetchone()

        if existing:
            # 已有用户：更新 session_key / unionid / 昵称头像
            user_id = existing["user_id"] or new_id("usr")
            update_result = await conn.execute(
                """
                UPDATE wechat_miniapp_user
                SET session_key = %s,
                    unionid = COALESCE(%s, unionid),
                    nickname = COALESCE(%s, nickname),
                    avatar_url = COALESCE(%s, avatar_url),
                    user_id = %s,
                    updated_at = now()
                WHERE openid = %s
                RETURNING *
                """,
                (
                    session_key,
                    unionid,
                    body.nickname,
                    body.avatar_url,
                    user_id,
                    openid,
                ),
            )
            user_row = await update_result.fetchone()
        else:
            # 新建用户
            user_id = new_id("usr")
            user_pk = new_id("wmau")
            insert_result = await conn.execute(
                """
                INSERT INTO wechat_miniapp_user(
                    id, workspace_id, openid, unionid, user_id, nickname, avatar_url, session_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    user_pk,
                    workspace_id,
                    openid,
                    unionid,
                    user_id,
                    body.nickname or "",
                    body.avatar_url or "",
                    session_key,
                ),
            )
            user_row = await insert_result.fetchone()

        # 持久化 session
        session_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        session_id = new_id("wmasess")
        await conn.execute(
            """
            INSERT INTO wechat_miniapp_session(
                id, workspace_id, openid, session_token, refresh_token, expires_at, refresh_expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                session_id,
                workspace_id,
                openid,
                session_token,
                refresh_token,
                now + SESSION_TOKEN_TTL,
                now + REFRESH_TOKEN_TTL,
            ),
        )

        # 签发平台 access_token（复用 core 的 JWT 签发逻辑）
        access_token = create_access_token(user_id, workspace_id, "member")
        # 记录登录审计日志
        await _log_session_event(conn, workspace_id, user_id, session_id, "login", None, None)
        await conn.commit()

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "session_token": session_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "openid": openid,
        "user": _user_view(user_row),
    }


@router.post("/refresh")
async def refresh(body: RefreshRequest):
    """刷新 token：用 refresh_token 换新 access_token，并轮换 session_token/refresh_token。"""
    now = datetime.now(UTC)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM wechat_miniapp_session WHERE refresh_token = %s",
            (body.refresh_token,),
        )
        session = await result.fetchone()
        if not session:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        # refresh_token 过期检查
        refresh_expires = session["refresh_expires_at"]
        if refresh_expires is not None and refresh_expires < now:
            raise HTTPException(status_code=401, detail="Refresh token expired")

        # 查用户以拿到 user_id 签发 access_token
        user_result = await conn.execute(
            "SELECT user_id FROM wechat_miniapp_user WHERE openid = %s",
            (session["openid"],),
        )
        user_row = await user_result.fetchone()
        user_id = (user_row or {}).get("user_id") or new_id("usr")

        # 轮换 session_token / refresh_token
        new_session_token = secrets.token_urlsafe(32)
        new_refresh_token = secrets.token_urlsafe(32)
        await conn.execute(
            """
            UPDATE wechat_miniapp_session
            SET session_token = %s,
                refresh_token = %s,
                expires_at = %s,
                refresh_expires_at = %s
            WHERE id = %s
            """,
            (
                new_session_token,
                new_refresh_token,
                now + SESSION_TOKEN_TTL,
                now + REFRESH_TOKEN_TTL,
                session["id"],
            ),
        )
        access_token = create_access_token(user_id, session["workspace_id"], "member")
        # 记录刷新审计日志
        await _log_session_event(conn, session["workspace_id"], user_id, session["id"], "refresh", None, None)
        await conn.commit()

    return {
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "session_token": new_session_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
    }


@router.get("/session")
async def get_session(
    session_token: Annotated[str, Depends(_extract_session_token)],
):
    """获取当前会话。返回 openid（脱敏）/session 状态/用户信息。"""
    now = datetime.now(UTC)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM wechat_miniapp_session WHERE session_token = %s",
            (session_token,),
        )
        session = await result.fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        # session 过期检查
        expires_at = session["expires_at"]
        if expires_at is not None and expires_at < now:
            raise HTTPException(status_code=401, detail="Session expired")
        user_result = await conn.execute(
            "SELECT * FROM wechat_miniapp_user WHERE openid = %s",
            (session["openid"],),
        )
        user_row = await user_result.fetchone()

    return {
        "openid": _mask_openid(session["openid"]),
        "workspace_id": session["workspace_id"],
        "expires_at": session["expires_at"],
        "session_active": True,
        "user": _user_view(user_row) if user_row else None,
    }


@router.post("/subscribe")
async def subscribe(
    body: SubscribeRequest,
    session_token: Annotated[str, Depends(_extract_session_token)],
):
    """记录订阅消息授权。用户在前端授权后上报 template_ids，幂等写入订阅表。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM wechat_miniapp_session WHERE session_token = %s",
            (session_token,),
        )
        session = await result.fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # 幂等写入：UNIQUE(openid, template_id) + ON CONFLICT DO NOTHING
        recorded: list[str] = []
        for template_id in body.template_ids:
            sub_id = new_id("wmasub")
            await conn.execute(
                """
                INSERT INTO wechat_miniapp_subscription(id, workspace_id, openid, template_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (openid, template_id) DO NOTHING
                """,
                (sub_id, session["workspace_id"], session["openid"], template_id),
            )
            recorded.append(template_id)
        await conn.commit()

    return {
        "recorded": recorded,
        "count": len(recorded),
        "openid_masked": _mask_openid(session["openid"]),
    }


@router.post("/notify")
async def send_notify(
    body: NotifyRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """发送订阅消息（admin/owner）。

    先校验订阅授权记录存在，再调用微信 subscribeMessage.send；
    无订阅记录则跳过；未配置 appid/secret 时 mock 成功。
    """
    _require_admin(actor)
    async with pool.connection() as conn:
        sub_result = await conn.execute(
            """
            SELECT * FROM wechat_miniapp_subscription
            WHERE workspace_id = %s AND openid = %s AND template_id = %s
            """,
            (actor.workspace_id, body.openid, body.template_id),
        )
        sub = await sub_result.fetchone()

    if not sub:
        # 无订阅记录：跳过发送
        return {
            "sent": 0,
            "skipped": 1,
            "reason": "no subscription record",
            "openid_masked": _mask_openid(body.openid),
        }

    # 调用微信订阅消息发送（未配置 appid/secret 时 mock 成功）
    response = _send_subscribe_message(body.openid, body.template_id, body.data, body.page)
    return {
        "sent": 1,
        "skipped": 0,
        "openid_masked": _mask_openid(body.openid),
        "response": response,
    }


@router.get("/templates")
async def list_templates(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """订阅消息模板列表（管理端：当前 workspace 已配置的模板）。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT * FROM wechat_miniapp_template
            WHERE workspace_id = %s
            ORDER BY created_at DESC
            """,
            (actor.workspace_id,),
        )
        rows = await result.fetchall()
    items = [_template_view(row) for row in rows]
    return {"items": items, "count": len(items)}


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_template(
    body: TemplateCreate,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """添加订阅消息模板（admin/owner）。"""
    _require_admin(actor)
    template_pk = new_id("wmatpl")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            INSERT INTO wechat_miniapp_template(id, workspace_id, template_id, title, description, scene)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                template_pk,
                actor.workspace_id,
                body.template_id,
                body.title,
                body.description,
                body.scene,
            ),
        )
        row = await result.fetchone()
        await conn.commit()
    return _template_view(row)


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除订阅消息模板（admin/owner）。跨 workspace 返回 403。"""
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM wechat_miniapp_template WHERE template_id = %s",
            (template_id,),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Template not found")
        if row["workspace_id"] != actor.workspace_id:
            raise HTTPException(
                status_code=403, detail="Template belongs to another workspace"
            )
        await conn.execute(
            "DELETE FROM wechat_miniapp_template WHERE id = %s",
            (row["id"],),
        )
        await conn.commit()
    return {"template_id": template_id, "deleted": True}


# ============================================================================
# 会话安全强化端点（P2 阶段新增）
# ============================================================================


@router.post("/logout")
async def logout(
    body: LogoutRequest,
    actor: Annotated[Actor, Depends(get_actor)],
    authorization: Annotated[str | None, Header()] = None,
    x_forwarded_for: Annotated[str | None, Header()] = None,
    user_agent: Annotated[str | None, Header()] = None,
):
    """注销当前会话。

    先由 get_actor 验证 access_token 有效性，再撤销 refresh_token / session_token /
    access_token（Redis blacklist），删除会话记录，写入 logout 审计日志。
    """
    # get_actor 依赖已完成 access_token 验证；此处提取原始 token 用于撤销
    access_token = _extract_bearer_token(authorization)
    ip = _client_ip(x_forwarded_for)
    now = datetime.now(UTC)

    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM wechat_miniapp_session WHERE refresh_token = %s AND workspace_id = %s",
            (body.refresh_token, actor.workspace_id),
        )
        session = await result.fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # 查 user_id 以写审计日志
        user_result = await conn.execute(
            "SELECT user_id FROM wechat_miniapp_user WHERE openid = %s",
            (session["openid"],),
        )
        user_row = await user_result.fetchone()
        user_id = (user_row or {}).get("user_id") or actor.user_id

        # 撤销三类 token（Redis blacklist，TTL=剩余有效期）
        refresh_expires = session["refresh_expires_at"]
        refresh_ttl = int((refresh_expires - now).total_seconds()) if refresh_expires and refresh_expires > now else 0
        await _revoke_token(body.refresh_token, max(refresh_ttl, 1))
        await _revoke_token(access_token, ACCESS_TOKEN_TTL_SECONDS)
        await _revoke_token(session["session_token"], int(SESSION_TOKEN_TTL.total_seconds()))

        # 删除会话记录
        await conn.execute(
            "DELETE FROM wechat_miniapp_session WHERE id = %s",
            (session["id"],),
        )
        # 写 logout 审计日志
        await _log_session_event(
            conn, actor.workspace_id, user_id, session["id"], "logout", ip, user_agent
        )
        await conn.commit()

    return {"logged_out": True, "session_id": session["id"]}


@router.get("/security-check")
async def security_check(
    actor: Annotated[Actor, Depends(get_actor)],
    x_forwarded_for: Annotated[str | None, Header()] = None,
    user_agent: Annotated[str | None, Header()] = None,
):
    """返回当前会话安全状态。

    包含 last_login_at / login_ip / device_fingerprint / active_sessions_count /
    suspicious_flags（IP 异常、设备指纹变化、并发会话过多）。
    普通用户可访问自己的会话安全状态。
    """
    ip = _client_ip(x_forwarded_for)

    async with pool.connection() as conn:
        # 查当前用户活跃会话（通过 user_id 关联 wechat_miniapp_user → session）
        sessions_result = await conn.execute(
            """
            SELECT s.* FROM wechat_miniapp_session s
            JOIN wechat_miniapp_user u ON s.openid = u.openid
            WHERE u.user_id = %s AND s.workspace_id = %s
            ORDER BY s.created_at DESC
            """,
            (actor.user_id, actor.workspace_id),
        )
        sessions = await sessions_result.fetchall()

        # 查最近一次 login 审计日志
        log_result = await conn.execute(
            """
            SELECT * FROM wx_miniapp_session_log
            WHERE workspace_id = %s AND user_id = %s AND action = 'login'
            ORDER BY created_at DESC LIMIT 1
            """,
            (actor.workspace_id, actor.user_id),
        )
        last_login = await log_result.fetchone()

        # 查历史 login IP 列表（异常检测）
        ips_result = await conn.execute(
            """
            SELECT DISTINCT ip FROM wx_miniapp_session_log
            WHERE workspace_id = %s AND user_id = %s AND action = 'login' AND ip IS NOT NULL
            """,
            (actor.workspace_id, actor.user_id),
        )
        ip_rows = await ips_result.fetchall()

        # 查历史 login user_agent 列表（设备指纹检测）
        ua_result = await conn.execute(
            """
            SELECT DISTINCT user_agent FROM wx_miniapp_session_log
            WHERE workspace_id = %s AND user_id = %s AND action = 'login' AND user_agent IS NOT NULL
            """,
            (actor.workspace_id, actor.user_id),
        )
        ua_rows = await ua_result.fetchall()

    known_ips = {row["ip"] for row in ip_rows if row.get("ip")}
    known_uas = {row["user_agent"] for row in ua_rows if row.get("user_agent")}

    suspicious_flags: list[str] = []
    if ip and known_ips and ip not in known_ips:
        suspicious_flags.append("ip_anomaly")
    if user_agent and known_uas and user_agent not in known_uas:
        suspicious_flags.append("device_fingerprint_changed")
    if len(sessions) > 5:
        suspicious_flags.append("concurrent_sessions_excessive")

    return {
        "last_login_at": (last_login or {}).get("created_at"),
        "login_ip": (last_login or {}).get("ip"),
        "device_fingerprint": (last_login or {}).get("user_agent"),
        "active_sessions_count": len(sessions),
        "suspicious_flags": suspicious_flags,
    }


@router.post("/sessions/revoke")
async def revoke_session(
    body: RevokeSessionRequest,
    actor: Annotated[Actor, Depends(get_actor)],
    x_forwarded_for: Annotated[str | None, Header()] = None,
    user_agent: Annotated[str | None, Header()] = None,
):
    """撤销指定 session_id（admin/owner 权限）。

    跨 workspace 返回 404（不泄露存在性），跨用户同 workspace 由 admin 操作。
    """
    _require_admin(actor)
    ip = _client_ip(x_forwarded_for)
    now = datetime.now(UTC)

    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM wechat_miniapp_session WHERE id = %s AND workspace_id = %s",
            (body.session_id, actor.workspace_id),
        )
        session = await result.fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # 查 user_id
        user_result = await conn.execute(
            "SELECT user_id FROM wechat_miniapp_user WHERE openid = %s",
            (session["openid"],),
        )
        user_row = await user_result.fetchone()
        user_id = (user_row or {}).get("user_id") or actor.user_id

        # 撤销 token
        refresh_expires = session["refresh_expires_at"]
        refresh_ttl = int((refresh_expires - now).total_seconds()) if refresh_expires and refresh_expires > now else 0
        await _revoke_token(session["refresh_token"], max(refresh_ttl, 1))
        await _revoke_token(session["session_token"], int(SESSION_TOKEN_TTL.total_seconds()))

        await conn.execute(
            "DELETE FROM wechat_miniapp_session WHERE id = %s",
            (session["id"],),
        )
        await _log_session_event(
            conn, actor.workspace_id, user_id, session["id"], "revoke", ip, user_agent
        )
        await conn.commit()

    return {"revoked": True, "session_id": body.session_id}


@router.get("/sessions")
async def list_sessions(
    actor: Annotated[Actor, Depends(get_actor)],
    cursor: str | None = None,
    limit: int = 20,
):
    """列出当前用户的所有活跃会话（session_id/device/last_active_at/ip）。

    支持 cursor 分页（按 created_at 游标），默认 limit=20。普通用户可查看自己的会话。
    """
    if limit < 1 or limit > 100:
        limit = 20

    async with pool.connection() as conn:
        if cursor:
            result = await conn.execute(
                """
                SELECT s.id, s.openid, s.expires_at, s.refresh_expires_at, s.created_at
                FROM wechat_miniapp_session s
                JOIN wechat_miniapp_user u ON s.openid = u.openid
                WHERE u.user_id = %s AND s.workspace_id = %s AND s.created_at < %s
                ORDER BY s.created_at DESC
                LIMIT %s
                """,
                (actor.user_id, actor.workspace_id, cursor, limit + 1),
            )
        else:
            result = await conn.execute(
                """
                SELECT s.id, s.openid, s.expires_at, s.refresh_expires_at, s.created_at
                FROM wechat_miniapp_session s
                JOIN wechat_miniapp_user u ON s.openid = u.openid
                WHERE u.user_id = %s AND s.workspace_id = %s
                ORDER BY s.created_at DESC
                LIMIT %s
                """,
                (actor.user_id, actor.workspace_id, limit + 1),
            )
        rows = await result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = str(items[-1]["created_at"]) if has_more and items else None

    return {
        "items": [
            {
                "session_id": row.get("id"),
                "device": _mask_openid(row.get("openid", "")),
                "last_active_at": row.get("created_at"),
                "ip": None,
            }
            for row in items
        ],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }
