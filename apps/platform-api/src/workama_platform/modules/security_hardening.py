"""Security hardening: input validation, IDOR protection, rate limiting, security policy.

P3 第二次渗透测试安全加固（OWASP Top 10 专项）在本模块追加：
- CSRF 防护（``csrf_protect`` 依赖，校验 Origin/Referer 受信来源）
- 安全响应头 ASGI 中间件 ``SecurityHeadersMiddleware``
- 速率限制 ASGI 中间件 ``RateLimitMiddleware``（滑动窗口，per token/IP，分层阈值）
- 密码强度策略 ``validate_password_strength``
- JWT 安全增强：刷新令牌轮换 / 令牌黑名单 / IP+UA 指纹绑定
- 审计日志链式 hash（防篡改）+ 链完整性校验
- 4 个新端点（含 workspace 隔离 + JWT 鉴权）
- ``SCHEMA_STATEMENTS`` + ``ensure_security_hardening_schema`` 建表登记

不引入新的第三方依赖；所有 DB 访问通过 ``pool``，可在测试中用 fake connection 替换。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlparse

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    _DEV_HS256_MODE,
    _JWT_PRIVATE_KEY,
    create_access_token,
    decode_token,
    get_actor,
    hash_secret,
    json_dumps,
    new_id,
    pool,
    settings,
)
# ---------------------------------------------------------------------------
# Dangerous pattern regexes
# ---------------------------------------------------------------------------

_PATH_TRAVERSAL = re.compile(r"\.\./|\.\.\\")
_NULL_BYTE = re.compile(r"\x00")
_SQL_INJECTION_HINTS = re.compile(
    r"(union\s+select|insert\s+into|delete\s+from|drop\s+table|--|;)",
    re.IGNORECASE,
)
_XSS_HINTS = re.compile(
    r"<script|javascript:|onerror=|onload=",
    re.IGNORECASE,
)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_PATH_SEPARATORS = re.compile(r"[/\\]")

_WORKAMA_ID = re.compile(r"^[a-z]{3,5}_[0-9A-Za-z]{20,}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_RESOURCE_TABLES: dict[str, tuple[str, str]] = {
    "ag_session": ("ag_session", "user_id"),
    "ag_attachment": ("ag_attachment", "user_id"),
    "ag_artifact": ("ag_artifact", "user_id"),
    "id_notification": ("id_notification", "user_id"),
}


# ---------------------------------------------------------------------------
# 1. Input validation
# ---------------------------------------------------------------------------


def validate_path_component(value: str, max_length: int = 256) -> str:
    """Validate path component (no traversal/null/control chars). Raises 400."""
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=400, detail="path component is required")

    if len(value) > max_length:
        raise HTTPException(status_code=400, detail="path component too long")

    if _PATH_TRAVERSAL.search(value):
        raise HTTPException(status_code=400, detail="path traversal detected in component")

    if _NULL_BYTE.search(value):
        raise HTTPException(status_code=400, detail="null byte in path component")

    if _PATH_SEPARATORS.search(value):
        raise HTTPException(status_code=400, detail="path separators not allowed in component")

    if _CONTROL_CHARS.search(value):
        raise HTTPException(status_code=400, detail="control characters in path component")

    return value


def sanitize_search_query(value: Any, max_length: int = 500) -> str:
    """Sanitize search query: remove SQL injection hints + XSS tags, truncate."""
    if value is None:
        return ""

    text = str(value)
    text = text.replace("\x00", "")
    text = _SQL_INJECTION_HINTS.sub("", text)
    text = _XSS_HINTS.sub("", text)

    if len(text) > max_length:
        text = text[:max_length]

    return text.strip()


def validate_uuid_like(value: Any) -> bool:
    """Check if value matches Workama ID or UUID format."""
    if not isinstance(value, str) or not value:
        return False

    if _WORKAMA_ID.match(value):
        return True

    if _UUID.match(value):
        return True

    return False


def check_mass_assignment(body: Any, allowed_fields: set[str] | None) -> dict:
    """Filter out non-allowed fields (mass assignment protection)."""
    if not isinstance(body, dict):
        return {}

    if not allowed_fields:
        return {}

    return {k: v for k, v in body.items() if k in allowed_fields}


# ---------------------------------------------------------------------------
# 2. IDOR protection
# ---------------------------------------------------------------------------


async def require_resource_owner(
    resource_type: str,
    resource_id: str,
    actor: Actor,
) -> dict[str, Any]:
    """Verify resource belongs to current user + workspace."""
    table_info = _RESOURCE_TABLES.get(resource_type)
    if table_info is None:
        raise HTTPException(status_code=404, detail="resource not found")

    if not validate_uuid_like(resource_id):
        raise HTTPException(status_code=404, detail="resource not found")

    table_name, owner_col = table_info

    query = (
        f"SELECT * FROM {table_name} "
        f"WHERE id = %s AND {owner_col} = %s AND workspace_id = %s"
    )

    async with pool.connection() as conn:
        result = await conn.execute(query, (resource_id, actor.user_id, actor.workspace_id))
        row = await result.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="resource not found")

    return {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "owner_verified": True,
    }


# ---------------------------------------------------------------------------
# 3. Rate limiting (per-user dependency, kept for backwards compat)
# ---------------------------------------------------------------------------

_rate_limit_buckets: dict[str, list[float]] = defaultdict(list)


def _reset_rate_limits() -> None:
    """Clear rate limit state (for testing)."""
    _rate_limit_buckets.clear()


def rate_limit_dependency(max_requests: int = 60, window_seconds: int = 60):
    """Return FastAPI dependency for per-user rate limiting. Exceeds -> 429."""

    async def _check(actor: Actor) -> None:
        now = time.monotonic()
        key = actor.user_id

        bucket = _rate_limit_buckets[key]
        cutoff = now - window_seconds
        _rate_limit_buckets[key] = [ts for ts in bucket if ts > cutoff]

        if len(_rate_limit_buckets[key]) >= max_requests:
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": str(window_seconds)},
            )

        _rate_limit_buckets[key].append(now)

    return _check


# ---------------------------------------------------------------------------
# 4. Security policy
# ---------------------------------------------------------------------------

_SECURITY_HEADERS_CATALOG = [
    {"name": "X-Content-Type-Options", "value": "nosniff", "description": "Prevent MIME sniffing"},
    {"name": "X-Frame-Options", "value": "DENY", "description": "Prevent clickjacking"},
    {"name": "Strict-Transport-Security", "value": "max-age=31536000; includeSubDomains", "description": "Force HTTPS"},
    {"name": "X-XSS-Protection", "value": "1; mode=block", "description": "XSS filter"},
    {"name": "Content-Security-Policy", "value": "default-src 'self'", "description": "Content security policy"},
    {"name": "Referrer-Policy", "value": "strict-origin-when-cross-origin", "description": "Referrer control"},
    {"name": "Permissions-Policy", "value": "geolocation=(), microphone=(), camera=()", "description": "Permissions policy"},
]

_MFA_REQUIRED_ROLES = {"owner", "admin"}
_PASSWORD_POLICY = {
    "min_length": 12,
    "require_uppercase": True,
    "require_lowercase": True,
    "require_digit": True,
    "require_special": True,
}


async def security_headers_info() -> dict[str, Any]:
    """Return platform security headers catalog."""
    return {"headers": _SECURITY_HEADERS_CATALOG}


async def security_policy(actor: Actor) -> dict[str, Any]:
    """Return current workspace security policy."""
    mfa_required = getattr(actor, "role", "member") in _MFA_REQUIRED_ROLES

    return {
        "workspace_id": actor.workspace_id,
        "password_policy": _PASSWORD_POLICY,
        "session_timeout_seconds": 3600,
        "mfa_required": mfa_required,
        "mfa_required_roles": sorted(_MFA_REQUIRED_ROLES),
        "auth_strength_current": getattr(actor, "auth_strength", 1),
    }

# ===========================================================================
# P3 第二次渗透测试安全加固
# ===========================================================================

# ---------------------------------------------------------------------------
# 5. CSRF 防护
# ---------------------------------------------------------------------------

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_CSRF_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/auth/refresh-rotate",
    }
)


def _origin_from_referer(referer: str) -> str:
    """从 Referer 中提取 scheme://host 部分，便于与受信 origin 比对。"""
    if not referer:
        return ""
    parsed = urlparse(referer)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _extract_request_origin(request: Request) -> str:
    """提取请求来源：优先 Origin header，缺失时回退 Referer 的 origin 部分。"""
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        return origin
    referer = (request.headers.get("referer") or "").strip()
    return _origin_from_referer(referer)


def _origin_is_trusted(origin: str) -> bool:
    """来源是否在 settings.trusted_origins 受信列表内。"""
    if not origin:
        return False
    return origin in settings.trusted_origins


async def csrf_protect(request: Request) -> None:
    """FastAPI 依赖：对状态变更请求校验 Origin/Referer 受信来源。"""
    if request.method in _SAFE_METHODS:
        return
    path = request.url.path
    if path in _CSRF_EXEMPT_PATHS:
        return
    if request.headers.get("x-internal-token"):
        return
    origin = _extract_request_origin(request)
    if not _origin_is_trusted(origin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF: untrusted or missing origin",
        )


# ---------------------------------------------------------------------------
# 6. 安全响应头 ASGI 中间件
# ---------------------------------------------------------------------------

SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"strict-transport-security", b"max-age=31536000"),
    (b"content-security-policy", b"default-src 'self'"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
]


class SecurityHeadersMiddleware:
    """ASGI 中间件：为所有 HTTP 响应注入 6 个安全响应头（已存在则不覆盖）。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        existing: set[bytes] = set()

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                for key, _value in headers:
                    existing.add(key.lower())
                for name, value in SECURITY_HEADERS:
                    if name not in existing:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ---------------------------------------------------------------------------
# 7. 速率限制 ASGI 中间件（滑动窗口，per token/IP）
# ---------------------------------------------------------------------------

_RATE_LIMIT_EXEMPT_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz"})

_SENSITIVE_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/auth/refresh-rotate",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
        "/api/v1/security/password/strength-check",
        "/api/v1/security/csrf-token",
    }
)

_rl_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_WINDOW_SECONDS = 60


def _reset_rate_limit_middleware() -> None:
    """清空中间件速率限制桶（测试用）。"""
    _rl_buckets.clear()


def _route_tier(path: str) -> str:
    if path == "/api/v1/auth/login":
        return "login"
    if path in _SENSITIVE_PATHS:
        return "sensitive"
    return "default"


def _tier_limit(tier: str) -> int:
    if tier == "login":
        return settings.rate_limit_login_per_min
    if tier == "sensitive":
        return settings.rate_limit_sensitive_per_min
    return settings.rate_limit_default_per_min


def _scope_header(headers: list[tuple[bytes, bytes]], name: str) -> str:
    """Case-insensitive header lookup from ASGI scope headers."""
    target = name.lower().encode("latin-1")
    for key, value in headers:
        if key.lower() == target:
            try:
                return value.decode("latin-1").strip()
            except Exception:
                return ""
    return ""


def _rl_identifier(headers: list[tuple[bytes, bytes]], scope: dict[str, Any]) -> str:
    """速率限制标识：有 Bearer token 时按 token hash，否则按客户端 IP。"""
    auth = _scope_header(headers, "authorization")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        if token:
            return "tok:" + hash_secret(token)[:24]
    client = scope.get("client")
    if isinstance(client, (list, tuple)) and client:
        return "ip:" + str(client[0])
    return "ip:anonymous"


def _rl_allow(key: str, limit: int, *, window: int = _RATE_LIMIT_WINDOW_SECONDS) -> bool:
    """滑动窗口判定：返回 True 表示放行并记录本次时间戳。"""
    now = time.monotonic()
    bucket = _rl_buckets[key]
    cutoff = now - window
    _rl_buckets[key] = [ts for ts in bucket if ts > cutoff]
    if len(_rl_buckets[key]) >= limit:
        return False
    _rl_buckets[key].append(now)
    return True


def _rl_retry_after(key: str, *, window: int = _RATE_LIMIT_WINDOW_SECONDS) -> int:
    """返回距离窗口内最旧请求过期所需的秒数（至少 1）。"""
    bucket = _rl_buckets.get(key)
    if not bucket:
        return window
    now = time.monotonic()
    oldest = bucket[0]
    remaining = int(oldest + window - now) + 1
    return max(1, min(window, remaining))


class RateLimitMiddleware:
    """ASGI 中间件：分层滑动窗口速率限制，超限返回 429 + Retry-After。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        if path in _RATE_LIMIT_EXEMPT_PATHS or not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        if _scope_header(headers, "x-internal-token"):
            await self.app(scope, receive, send)
            return

        tier = _route_tier(path)
        limit = _tier_limit(tier)
        identifier = _rl_identifier(headers, scope)
        key = f"{tier}:{identifier}"

        if not _rl_allow(key, limit):
            retry = _rl_retry_after(key)
            body = json_dumps({"detail": "rate limit exceeded"}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", str(retry).encode("latin-1")),
                        (b"content-length", str(len(body)).encode("latin-1")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)

# ---------------------------------------------------------------------------
# 8. 密码强度策略
# ---------------------------------------------------------------------------

_WEAK_PASSWORDS: frozenset[str] = frozenset(
    {
        "123456", "password", "12345678", "qwerty", "123456789", "12345", "1234", "111111",
        "1234567", "dragon", "123123", "baseball", "abc123", "football", "monkey", "letmein",
        "696969", "shadow", "master", "666666", "qwertyuiop", "123321", "mustang", "1234567890",
        "michael", "654321", "pussy", "superman", "1qaz2wsx", "7777777", "fuckyou", "121212",
        "000000", "qazwsx", "123qwe", "killer", "trustno1", "jordan", "jennifer", "zxcvbnm",
        "asdfgh", "hunter", "buster", "soccer", "harley", "batman", "andrew", "tigger",
        "sunshine", "iloveyou", "fuckme", "charlie", "robert", "thomas", "hockey", "ranger",
        "daniel", "starwars", "klaster", "112233", "george", "asshole", "computer", "michelle",
        "jessica", "pepper", "1111", "zxcvbn", "555555", "11111111", "131313", "freedom",
        "777777", "pass", "fuck", "maggie", "159753", "aaaaaa", "ginger", "princess", "joshua",
        "cheese", "amanda", "summer", "love", "ashley", "6969", "nicole", "chelsea", "biteme",
        "matthew", "access", "yankees", "987654321", "dallas", "austin", "thunder", "taylor",
        "matrix", "william", "corvette", "hello", "martin", "heather",
    }
)

_SPECIAL_CHARS = re.compile(r"[^A-Za-z0-9]")
_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_DIGIT = re.compile(r"[0-9]")


def validate_password_strength(
    password: str,
    *,
    username: str = "",
    email: str = "",
) -> dict[str, Any]:
    """校验密码强度，返回 ``{valid, score, suggestions}``。"""
    if not isinstance(password, str) or not password:
        return {
            "valid": False,
            "score": 0,
            "suggestions": ["password is required"],
        }

    suggestions: list[str] = []
    score = 0
    min_len = settings.password_min_length

    if len(password) >= min_len:
        score += 1
    else:
        suggestions.append(f"use at least {min_len} characters")

    has_upper = bool(_UPPER.search(password))
    has_lower = bool(_LOWER.search(password))
    has_digit = bool(_DIGIT.search(password))
    has_special = bool(_SPECIAL_CHARS.search(password))

    if has_upper:
        score += 1
    else:
        suggestions.append("add uppercase letters")
    if has_lower:
        score += 1
    else:
        suggestions.append("add lowercase letters")
    if has_digit:
        score += 1
    else:
        suggestions.append("add digits")
    if has_special:
        score += 1
    else:
        suggestions.append("add special characters")

    lower_pw = password.lower()
    if username and username.lower() in lower_pw:
        suggestions.append("do not include your username")
        score = max(0, score - 1)
    email_local = email.split("@", 1)[0] if email else ""
    if email_local and len(email_local) >= 3 and email_local.lower() in lower_pw:
        suggestions.append("do not include your email address")
        score = max(0, score - 1)

    if password.lower() in _WEAK_PASSWORDS:
        suggestions.append("avoid common weak passwords")
        score = 0

    valid = (
        len(password) >= min_len
        and has_upper
        and has_lower
        and has_digit
        and has_special
        and password.lower() not in _WEAK_PASSWORDS
        and (not username or username.lower() not in lower_pw)
        and (not email_local or len(email_local) < 3 or email_local.lower() not in lower_pw)
    )

    if valid and not suggestions:
        suggestions.append("password meets all strength requirements")

    return {"valid": valid, "score": score, "suggestions": suggestions}


# ---------------------------------------------------------------------------
# 9. JWT 安全增强：刷新令牌轮换 / 黑名单 / IP+UA 指纹绑定
# ---------------------------------------------------------------------------


def _refresh_token_raw() -> str:
    return secrets.token_urlsafe(48)


def bind_token_fingerprint(ip: str, ua: str) -> str:
    """生成 IP + UA 指纹 hash（绑定 access token，变更时失效）。"""
    raw = f"{ip or ''}|{ua or ''}"
    return hmac.new(
        settings.key_pepper.encode(), raw.encode(), hashlib.sha256
    ).hexdigest()


def create_bound_access_token(
    user_id: str,
    workspace_id: str,
    role: str,
    *,
    ip: str = "",
    ua: str = "",
    auth_strength: int = 1,
) -> str:
    """签发带 ``jti`` + ``fp``（IP+UA 指纹）绑定的 access token。"""
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "ws": workspace_id,
        "role": role,
        "auth_strength": auth_strength,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "jti": new_id("jti"),
        "fp": bind_token_fingerprint(ip, ua) if (ip or ua) else "",
    }
    if _DEV_HS256_MODE:
        return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return jwt.encode(payload, _JWT_PRIVATE_KEY, algorithm="RS256")


def verify_token_binding(payload: dict[str, Any], ip: str, ua: str) -> bool:
    """校验 token 的 IP+UA 指纹；无 ``fp`` claim 视为未绑定（兼容旧 token）。"""
    fp = payload.get("fp")
    if not fp:
        return True
    return secrets.compare_digest(fp, bind_token_fingerprint(ip, ua))


async def is_token_blacklisted(jti: str) -> bool:
    """查询 ``jwt_token_blacklist`` 判断 jti 是否已撤销。"""
    if not jti:
        return False
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT 1 FROM jwt_token_blacklist WHERE jti = %s",
            (jti,),
        )
        row = await result.fetchone()
    return row is not None


async def revoke_token(jti: str, reason: str, expires_at: datetime | None = None) -> None:
    """将 jti 加入黑名单（幂等）。"""
    if not jti:
        return
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO jwt_token_blacklist(jti, reason, expires_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (jti) DO NOTHING
            """,
            (jti, reason, expires_at),
        )
        await conn.commit()


async def rotate_refresh_token(
    refresh_token: str,
    *,
    ip: str = "",
    ua: str = "",
) -> dict[str, Any]:
    """刷新令牌轮换：校验旧 refresh -> 撤销 + 加黑名单 -> 签发新 access + refresh。"""
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token required")

    token_hash = hash_secret(refresh_token)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT r.id, r.user_id, r.family_id, r.revoked_at, r.expires_at,
                   r.workspace_id, m.role
            FROM id_refresh_token r
            JOIN id_member m ON m.user_id = r.user_id AND m.workspace_id = r.workspace_id
            WHERE r.token_hash = %s
            FOR UPDATE
            """,
            (token_hash,),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Refresh token invalid")
        if row["revoked_at"] is not None:
            await conn.execute(
                "UPDATE id_refresh_token SET revoked_at = COALESCE(revoked_at, now()) WHERE family_id = %s",
                (row["family_id"],),
            )
            await conn.commit()
            raise HTTPException(status_code=401, detail="Refresh token reuse detected")
        if row["expires_at"] <= datetime.now(UTC):
            raise HTTPException(status_code=401, detail="Refresh token expired")

        await conn.execute(
            "UPDATE id_refresh_token SET revoked_at = now(), last_used_at = now() WHERE id = %s",
            (row["id"],),
        )
        await conn.execute(
            """
            INSERT INTO jwt_token_blacklist(jti, reason, expires_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (jti) DO NOTHING
            """,
            (row["id"], "refresh_rotated", row["expires_at"]),
        )

        new_refresh = _refresh_token_raw()
        new_refresh_id = new_id("rft")
        new_expires = datetime.now(UTC) + timedelta(days=30)
        await conn.execute(
            """
            INSERT INTO id_refresh_token(id, user_id, workspace_id, token_hash, family_id, parent_id, expires_at, last_used_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                new_refresh_id,
                row["user_id"],
                row["workspace_id"],
                hash_secret(new_refresh),
                row["family_id"],
                row["id"],
                new_expires,
            ),
        )
        await conn.execute(
            "UPDATE id_refresh_token SET rotated_to_id = %s WHERE id = %s",
            (new_refresh_id, row["id"]),
        )
        await conn.commit()

    access_token = create_bound_access_token(
        row["user_id"], row["workspace_id"], row["role"], ip=ip, ua=ua
    )
    return {
        "access_token": access_token,
        "refresh_token": new_refresh,
        "token_type": "bearer",
        "expires_in": 15 * 60,
    }

# ---------------------------------------------------------------------------
# 10. 审计日志链式 hash（防篡改）
# ---------------------------------------------------------------------------


def _chain_payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def compute_payload_hash(payload: dict[str, Any]) -> str:
    """计算单条审计负载的 SHA256。"""
    return hashlib.sha256(_chain_payload_json(payload).encode("utf-8")).hexdigest()


def compute_chain_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """计算链式 hash：SHA256(prev_hash + curr_payload_hash)。"""
    return compute_chain_hash_from_payload_hash(prev_hash, compute_payload_hash(payload))


def compute_chain_hash_from_payload_hash(prev_hash: str, payload_hash: str) -> str:
    """链式 hash 的可重算形式：SHA256(prev_hash + payload_hash)。"""
    return hashlib.sha256((prev_hash + payload_hash).encode("utf-8")).hexdigest()


async def append_audit_chain_entry(
    conn: Any,
    audit_id: str,
    workspace_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """写入一条审计链记录。"""
    result = await conn.execute(
        """
        SELECT c.chain_hash FROM audit_log_chain c
        JOIN audit_log a ON a.id = c.audit_id
        WHERE a.workspace_id = %s
        ORDER BY c.created_at DESC, c.audit_id DESC LIMIT 1
        """,
        (workspace_id,),
    )
    last = await result.fetchone()
    prev_hash = last["chain_hash"] if last else ""
    payload_hash = compute_payload_hash(payload)
    chain_hash = compute_chain_hash_from_payload_hash(prev_hash, payload_hash)
    await conn.execute(
        """
        INSERT INTO audit_log_chain(audit_id, prev_hash, payload_hash, chain_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (audit_id) DO NOTHING
        """,
        (audit_id, prev_hash, payload_hash, chain_hash),
    )
    return {
        "audit_id": audit_id,
        "prev_hash": prev_hash,
        "payload_hash": payload_hash,
        "chain_hash": chain_hash,
    }


async def verify_audit_chain(workspace_id: str) -> dict[str, Any]:
    """验证 workspace 审计日志链完整性。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT c.audit_id, c.prev_hash, c.payload_hash, c.chain_hash
            FROM audit_log_chain c
            JOIN audit_log a ON a.id = c.audit_id
            WHERE a.workspace_id = %s
            ORDER BY a.created_at ASC, c.audit_id ASC
            """,
            (workspace_id,),
        )
        rows = await result.fetchall()

    if not rows:
        return {"valid": True, "count": 0, "broken_at": None}

    prev_hash = ""
    for row in rows:
        expected_prev = prev_hash
        if row["prev_hash"] != expected_prev:
            return {"valid": False, "count": len(rows), "broken_at": row["audit_id"]}
        expected_chain = compute_chain_hash_from_payload_hash(prev_hash, row["payload_hash"])
        if row["chain_hash"] != expected_chain:
            return {"valid": False, "count": len(rows), "broken_at": row["audit_id"]}
        prev_hash = row["chain_hash"]

    return {"valid": True, "count": len(rows), "broken_at": None}


# ---------------------------------------------------------------------------
# 11. SCHEMA_STATEMENTS + 建表函数
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS rate_limit_bucket (
        key TEXT NOT NULL,
        window_start TIMESTAMPTZ NOT NULL,
        count BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (key, window_start)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket_key_window ON rate_limit_bucket(key, window_start DESC)",
    """
    CREATE TABLE IF NOT EXISTS jwt_token_blacklist (
        jti TEXT PRIMARY KEY,
        reason TEXT NOT NULL,
        revoked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jwt_token_blacklist_expires ON jwt_token_blacklist(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS audit_log_chain (
        audit_id TEXT PRIMARY KEY,
        prev_hash TEXT NOT NULL DEFAULT '',
        payload_hash TEXT NOT NULL,
        chain_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_log_chain_created ON audit_log_chain(created_at DESC)",
)


async def ensure_security_hardening_schema(conn: Any) -> None:
    """幂等建表：执行所有 SCHEMA_STATEMENTS。"""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


# ---------------------------------------------------------------------------
# 12. 新增端点
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/security", tags=["security-hardening"])
auth_extension_router = APIRouter(prefix="/api/v1/auth", tags=["security-hardening"])


class PasswordStrengthRequest(BaseModel):
    password: str = Field(min_length=1, max_length=10_000)
    username: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)


class RefreshRotateRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


@router.post("/csrf-token")
async def issue_csrf_token(
    request: Request,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """获取 CSRF 令牌（绑定当前 session / user）。"""
    now_bucket = int(time.time() // 600)
    sig = hmac.new(
        settings.jwt_secret.encode(),
        f"{actor.user_id}:{now_bucket}".encode(),
        hashlib.sha256,
    ).hexdigest()
    csrf_token = f"{actor.user_id}.{sig}"
    return {"csrf_token": csrf_token, "expires_in": 600, "user_id": actor.user_id}


@router.get("/audit-chain/verify")
async def verify_audit_chain_endpoint(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """验证审计日志链完整性（admin only，workspace 隔离）。"""
    _require_admin(actor)
    return await verify_audit_chain(actor.workspace_id)


@router.post("/password/strength-check")
async def password_strength_check(
    body: PasswordStrengthRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """密码强度检查（不存储密码，仅返回 score 与建议）。"""
    result = validate_password_strength(
        body.password, username=body.username, email=body.email
    )
    return {
        "valid": result["valid"],
        "score": result["score"],
        "suggestions": result["suggestions"],
        "min_length": settings.password_min_length,
    }


@auth_extension_router.post("/refresh-rotate")
async def refresh_rotate(
    request: Request,
    response: Response,
    body: RefreshRotateRequest | None = None,
    workama_refresh: Annotated[str | None, Cookie()] = None,
):
    """刷新令牌轮换：旧 refresh_token 失效并加黑名单，返回新 access + refresh。"""
    refresh = body.refresh_token if body and body.refresh_token else workama_refresh
    if not refresh:
        raise HTTPException(status_code=401, detail="Refresh token required")
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    result = await rotate_refresh_token(refresh, ip=ip, ua=ua)
    response.set_cookie(
        "workama_refresh",
        result["refresh_token"],
        httponly=True,
        secure=settings.workama_env.lower() == "production",
        samesite="strict",
        max_age=30 * 24 * 3600,
        path="/api/v1/auth",
    )
    return result


__all__ = [
    "validate_path_component",
    "sanitize_search_query",
    "validate_uuid_like",
    "check_mass_assignment",
    "require_resource_owner",
    "rate_limit_dependency",
    "security_headers_info",
    "security_policy",
    "_reset_rate_limits",
    "csrf_protect",
    "SecurityHeadersMiddleware",
    "RateLimitMiddleware",
    "SECURITY_HEADERS",
    "validate_password_strength",
    "bind_token_fingerprint",
    "create_bound_access_token",
    "verify_token_binding",
    "is_token_blacklisted",
    "revoke_token",
    "rotate_refresh_token",
    "compute_payload_hash",
    "compute_chain_hash",
    "compute_chain_hash_from_payload_hash",
    "append_audit_chain_entry",
    "verify_audit_chain",
    "SCHEMA_STATEMENTS",
    "ensure_security_hardening_schema",
    "router",
    "auth_extension_router",
    "_reset_rate_limit_middleware",
    "_origin_is_trusted",
    "_extract_request_origin",
    "_route_tier",
    "_rl_allow",
    "_rl_retry_after",
]
