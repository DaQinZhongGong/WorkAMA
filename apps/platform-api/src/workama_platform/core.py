from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis
from workama_observability import org_id_var, workspace_id_var


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://workama:workama_dev@localhost:5432/workama"
    redis_url: str = "redis://localhost:6379/0"
    nats_url: str = "nats://localhost:4222"
    jwt_secret: str = "change-this-jwt-secret"
    jwt_private_key: str = ""
    jwt_public_key: str = ""
    internal_token: str = "change-this-internal-token"
    key_pepper: str = "change-this-key-pepper"
    encryption_key: str = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI="
    cors_origins: str = "http://localhost:20204"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_from: str = "notifications@workama.local"
    # 真实 SMTP 投递附加配置；smtp_mock=True 时仍走 mock 路径以保证向后兼容
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_mock: bool = True
    # 通知 Webhook 投递开关；True 时走 mock:// 确定性签名，False 时通过 httpx 真实投递
    notification_webhook_mock: bool = True
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "workama"
    minio_secret_key: str = "workama_minio"
    minio_secure: bool = False
    auth_debug_tokens: bool = False
    auth_oauth_enabled: bool = False
    oauth_redirect_base_url: str = "http://localhost:8000"
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    github_oauth_authorization_url: str = "https://github.com/login/oauth/authorize"
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_authorization_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    setup_token: str = ""
    agent_server_url: str = "http://agent-server:8001"
    sandbox_fleet_url: str = "http://sandbox-fleet:8002"
    gateway_url: str = "http://gateway:8080"
    billing_mock_webhook_secret: str = "workama-mock-provider-secret"
    workama_env: str = "development"
    # v7.177: 海外区数据驻留默认区域（CN/EU/US/SG），向后兼容默认 CN
    default_region: str = "CN"
    # 性能优化：DB 连接池规模与获取超时，可通过环境变量按负载调整
    db_pool_min_size: int = 5
    db_pool_max_size: int = 20
    # v7.176: 多可用区 Redis 哨兵配置（逗号分隔 host:port 列表；为空走单节点）
    redis_sentinels: str = ""
    redis_master_name: str = ""
    # P3 第二次渗透测试安全加固：CSRF 受信来源 / 密码强度 / 速率限制阈值
    trusted_origins: list[str] = ["http://localhost:20204", "http://localhost:20205"]
    password_min_length: int = 12
    rate_limit_login_per_min: int = 5
    rate_limit_sensitive_per_min: int = 10
    rate_limit_default_per_min: int = 60


settings = Settings()
pool = AsyncConnectionPool(
    settings.database_url,
    min_size=settings.db_pool_min_size,
    max_size=settings.db_pool_max_size,
    timeout=30,
    open=False,
    kwargs={"row_factory": dict_row},
)
redis = Redis.from_url(settings.redis_url, decode_responses=True)
password_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)

# ------------------------------------------------------------------------------
# Redis cache helpers (best-effort: silently skip when redis is unavailable)
# ------------------------------------------------------------------------------
CACHE_TTL_DEFAULT = 60


def _cache_key(workspace_id: str, resource: str, key_hash: str) -> str:
    return f"workama:cache:{workspace_id}:{resource}:{key_hash}"


async def cache_get(key: str) -> str | None:
    try:
        return await redis.get(key)
    except Exception:
        return None


async def cache_set(key: str, value: str, ttl: int = CACHE_TTL_DEFAULT) -> None:
    try:
        await redis.setex(key, ttl, value)
    except Exception:
        pass


async def cache_delete(key: str) -> None:
    try:
        await redis.delete(key)
    except Exception:
        pass


async def cache_delete_pattern(pattern: str) -> None:
    try:
        keys = await redis.keys(pattern)
        if keys:
            await redis.delete(*keys)
    except Exception:
        pass
bearer = HTTPBearer(auto_error=False)
fernet = Fernet(settings.encryption_key.encode())


def _load_jwt_keys():
    if settings.jwt_private_key and settings.jwt_public_key:
        return (
            serialization.load_pem_private_key(settings.jwt_private_key.encode(), password=None),
            serialization.load_pem_public_key(settings.jwt_public_key.encode()),
        )
    if settings.workama_env.lower() == "production":
        raise RuntimeError("JWT_PRIVATE_KEY and JWT_PUBLIC_KEY are required in production")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


_JWT_PRIVATE_KEY, _JWT_PUBLIC_KEY = _load_jwt_keys()

# dev 模式未配置 RSA 密钥时使用 HS256（共享 jwt_secret），保证多 worker 下
# 签发/验签密钥一致，避免跨 worker 验签 401。生产模式（配置了 RSA 密钥）仍走 RS256。
_DEV_HS256_MODE = not (settings.jwt_private_key and settings.jwt_public_key)

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str) -> str:
    timestamp = int(datetime.now(UTC).timestamp() * 1000)
    value = (timestamp << 80) | secrets.randbits(80)
    chars: list[str] = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"


def hash_secret(value: str) -> str:
    return hmac.new(
        settings.key_pepper.encode(), value.encode(), hashlib.sha256
    ).hexdigest()


def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    return fernet.encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    return fernet.decrypt(value.encode()).decode()


# ------------------------------------------------------------------------------
# 生产密钥硬化：启动期拒绝占位符 / 弱密钥（与网关 INTERNAL_TOKEN 处理逻辑一致）
# ------------------------------------------------------------------------------
# 文档化的占位符（任何环境都不应作为真实密钥）。
_PLACEHOLDER_JWT_SECRETS: frozenset[str] = frozenset(
    {
        "change-this-jwt-secret",
        "workama-local-jwt-secret-change-before-production",
    }
)
_PLACEHOLDER_KEY_PEPPERS: frozenset[str] = frozenset(
    {
        "change-this-key-pepper",
        "change-this-pepper",
        "workama-local-key-pepper-change-before-production",
    }
)
_PLACEHOLDER_INTERNAL_TOKENS: frozenset[str] = frozenset(
    {
        "change-this-internal-token",
    }
)
# 已知弱默认值（base64 编码的 32 字节全 0x42）。生产环境必须替换为唯一密钥。
_WEAK_ENCRYPTION_KEY = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI="
# 密钥最小长度阈值（字节 / 字符）。低于此值视为弱密钥。
_MIN_SECRET_LEN = 16


def _secret_is_placeholder(value: str, placeholders: frozenset[str]) -> bool:
    v = (value or "").strip()
    if not v:
        return True
    return v in placeholders


def validate_production_secrets(s: Settings | None = None) -> None:
    """生产环境拒绝占位符 / 弱密钥；开发 / 测试环境不强制。

    在 app lifespan 启动期调用（pool.open 之前）。返回 None 表示通过；
    否则抛 ``RuntimeError`` 阻断启动，避免以弱密钥对外提供服务。

    传入 ``s`` 便于单测；默认使用模块级单例 ``settings``。
    """
    s = s or settings
    if s.workama_env.lower() != "production":
        return
    problems: list[str] = []
    if _secret_is_placeholder(s.jwt_secret, _PLACEHOLDER_JWT_SECRETS) or len(s.jwt_secret) < _MIN_SECRET_LEN:
        problems.append("JWT_SECRET is a placeholder or too short (<16 chars)")
    if _secret_is_placeholder(s.key_pepper, _PLACEHOLDER_KEY_PEPPERS) or len(s.key_pepper) < _MIN_SECRET_LEN:
        problems.append("KEY_PEPPER is a placeholder or too short (<16 chars)")
    if _secret_is_placeholder(s.internal_token, _PLACEHOLDER_INTERNAL_TOKENS) or len(s.internal_token) < _MIN_SECRET_LEN:
        problems.append("INTERNAL_TOKEN is a placeholder or too short (<16 chars)")
    enc = (s.encryption_key or "").strip()
    if not enc or enc == _WEAK_ENCRYPTION_KEY:
        problems.append("ENCRYPTION_KEY is empty or the known weak default")
    else:
        try:
            Fernet(enc.encode())
        except Exception:
            problems.append("ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes)")
    if problems:
        raise RuntimeError(
            "Refusing to start in production with weak secrets: " + "; ".join(problems)
        )


def hash_password(value: str) -> str:
    return password_hasher.hash(value)


def verify_password(hashed: str, value: str) -> bool:
    try:
        return password_hasher.verify(hashed, value)
    except VerifyMismatchError:
        return False


def create_access_token(user_id: str, workspace_id: str, role: str, auth_strength: int = 1) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "ws": workspace_id,
        "role": role,
        "auth_strength": auth_strength,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    if _DEV_HS256_MODE:
        return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return jwt.encode(payload, _JWT_PRIVATE_KEY, algorithm="RS256")


def decode_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    if _DEV_HS256_MODE:
        # dev 模式直接 HS256，避免多 worker 随机 RS256 密钥导致跨 worker 验签失败
        try:
            payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
    else:
        try:
            payload = jwt.decode(token, _JWT_PUBLIC_KEY, algorithms=["RS256"])
        except jwt.PyJWTError as rs256_error:
            # Accept pre-RS256 access/MFA tokens during a rolling deployment. New
            # access tokens are always RS256. The HS256 compatibility path is local-only.
            if settings.workama_env.lower() == "production":
                raise HTTPException(status_code=401, detail="Invalid or expired token") from rs256_error
            try:
                payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
            except jwt.PyJWTError as exc:
                raise HTTPException(status_code=401, detail="Invalid or expired token") from rs256_error
    if payload.get("type") != expected_type:
        raise HTTPException(status_code=401, detail="Unexpected token type")
    return payload


# 性能优化：JWT 验签结果缓存（Redis），相同 token 60s 内不重复执行 RS256 验签。
# Redis 不可用时降级为每次验签（复用 cache_get/cache_set 的 best-effort 语义）。
JWT_VERIFY_CACHE_TTL = 60


def _jwt_cache_key(token: str) -> str:
    # 仅取 sha256 前 32 字符作为缓存 key，避免在 Redis 中存储完整 token
    token_hash = hashlib.sha256(token.encode()).hexdigest()[:32]
    return f"jwt:verified:{token_hash}"


async def decode_token_cached(token: str, expected_type: str = "access") -> dict[str, Any]:
    """带 Redis 缓存的 JWT 验签：缓存命中时不执行 RS256 验签，但仍校验 exp 防止过期 token 被放行。"""
    cache_key = _jwt_cache_key(token)
    cached = await cache_get(cache_key)
    if cached:
        try:
            payload = json.loads(cached)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if payload:
            # 缓存命中仍需校验 exp，避免 token 过期后缓存继续放行
            exp = payload.get("exp")
            if isinstance(exp, (int, float)) and datetime.now(UTC).timestamp() < exp:
                return payload
            # 已过期或异常，清除脏缓存后走正常验签
            await cache_delete(cache_key)
    # 缓存 miss 或过期：正常验签（验签失败会抛 401，不写入缓存）
    payload = decode_token(token, expected_type)
    await cache_set(cache_key, json.dumps(payload, separators=(",", ":")), JWT_VERIFY_CACHE_TTL)
    return payload


async def invalidate_jwt_cache(token: str) -> None:
    """登出/撤销时清除该 token 的验签缓存（best-effort，Redis 不可用静默跳过）。"""
    await cache_delete(_jwt_cache_key(token))


@dataclass(frozen=True)
class Actor:
    user_id: str
    workspace_id: str
    org_id: str
    role: str
    email: str
    display_name: str
    onboarding_completed: bool
    actor_type: str = "user"
    credential_id: str | None = None
    capabilities: tuple[str, ...] = ()
    auth_strength: int = 1


ROLE_CAPABILITIES = {
    "owner": ("*",),
    "admin": ("workspace:*", "member:*", "group:*", "role:*", "role_binding:*", "service_account_policy:*", "auth_policy:*", "audit:*", "a2a:*", "gateway_channel:*", "prompt:*", "api_key:*", "session:*", "operation:*", "security:*", "dataset:*", "rag_eval:*", "rag_feedback:create", "memory:*", "assistant:*", "workflow:*", "mcp_server:*", "moderation:*", "service_account:*", "code:*", "work:*", "automation:*", "skill:*", "connector:*", "identity_federation:*", "oauth_client:*", "webhook:*", "design:*", "external_app:*", "marketplace:*", "org:read", "im:*"),
    "member": ("session:*", "artifact:*", "group:read", "role:read", "role_binding:read", "service_account_policy:read", "auth_policy:read", "audit:read", "a2a:read", "gateway_channel:read", "prompt:read", "operation:read", "dataset:*", "rag_eval:*", "rag_feedback:create", "memory:*", "assistant:*", "workflow:*", "mcp_server:read", "moderation:read", "service_account:read", "code:*", "work:*", "automation:*", "skill:*", "connector:*", "identity_federation:read", "oauth_client:read", "webhook:*", "design:*", "external_app:read", "marketplace:read", "org:read", "im:*"),
    "viewer": ("session:read", "artifact:read", "group:read", "role:read", "role_binding:read", "service_account_policy:read", "auth_policy:read", "audit:read", "a2a:read", "gateway_channel:read", "prompt:read", "operation:read", "dataset:read", "rag_eval:read", "rag_feedback:create", "memory:read", "assistant:read", "workflow:read", "mcp_server:read", "moderation:read", "code:read", "work:read", "automation:read", "skill:read", "connector:read", "identity_federation:read", "oauth_client:read", "webhook:read", "design:read", "external_app:read", "marketplace:read", "org:read", "im:read"),
}


def capability_allows(grants: tuple[str, ...] | list[str], required: str) -> bool:
    domain = required.split(":", 1)[0]
    return "*" in grants or required in grants or f"{domain}:*" in grants


def platform_key_scope_allows(scopes: tuple[str, ...] | list[str], required: str) -> bool:
    action = required.split(":", 1)[-1]
    return capability_allows(scopes, required) or "platform:*" in scopes or (
        action == "read" and "platform:read" in scopes
    ) or (action != "read" and "platform:write" in scopes)


def _request_capability(request: Request) -> str:
    path = request.url.path
    domain = "platform"
    for prefix, candidate in (
        ("/api/v1/billing", "billing"), ("/api/v1/gateway/prompts", "prompt"), ("/api/v1/gateway", "gateway_channel"),
        ("/api/v1/sessions", "session"), ("/api/v1/api-keys", "api_key"),
        ("/api/v1/operations", "operation"), ("/api/v1/admin", "operation"),
        ("/api/v1/security", "security"), ("/api/v1/privacy", "privacy"),
        ("/api/v1/datasets", "dataset"), ("/api/v1/rag/feedback", "rag_feedback"),
        ("/api/v1/rag", "rag_eval"),
        ("/api/v1/memories", "memory"),
        ("/api/v1/assistants", "assistant"), ("/api/v1/workflows", "workflow"),
        ("/api/v1/workflow-runs", "workflow"),
        ("/api/v1/mcp-servers", "mcp_server"),
        ("/api/v1/service-accounts", "service_account"),
        ("/api/v1/code", "code"),
        ("/api/v1/work", "work"),
        ("/api/v1/automations", "automation"),
        ("/api/v1/skills", "skill"),
        ("/api/v1/connectors", "connector"),
        ("/api/v1/identity-federation", "identity_federation"),
        ("/api/v1/oauth/clients", "oauth_client"),
        ("/api/v1/webhooks", "webhook"),
        ("/api/v1/design", "design"),
        ("/api/v1/external-apps", "external_app"),
        ("/api/v1/marketplace", "marketplace"),
        ("/api/v1/enterprise/groups", "group"),
        ("/api/v1/enterprise/role-bindings", "role_binding"),
        ("/api/v1/enterprise/roles", "role"),
        ("/api/v1/enterprise/service-account-policies", "service_account_policy"),
        ("/api/v1/enterprise/auth-strength-matrix", "auth_policy"),
        ("/api/v1/enterprise/audit", "audit"),
        ("/api/v1/enterprise/siem", "audit"),
        ("/api/v1/a2a", "a2a"),
        ("/api/v1/orgs", "org"),
    ):
        if path.startswith(prefix): domain = candidate; break
    return f"{domain}:{'read' if request.method in {'GET', 'HEAD', 'OPTIONS'} else 'write'}"


# ---------------------------------------------------------------------------
# 鉴权 actor 读穿透缓存（尾延迟收口：get_actor 原每次请求命中 DB，是 P99 主因）
# ---------------------------------------------------------------------------
# best-effort：缓存命中/写入异常一律降级为直连 DB，不改变正确性。
# workama_env == "test" 时关闭，避免单测因共享 token 键互相污染。
_ACTOR_CACHE_TTL = 60.0
_ACTOR_CACHE_ENABLED = settings.workama_env.lower() != "test"

# 进程内 L1（per-worker）：热读跳过 Redis 跨容器 RTT；token 绑定、TTL 兜底，无需失效。
from workama_platform.modules.cache import LocalTTLCache

_ACTOR_LOCAL = LocalTTLCache(ttl=_ACTOR_CACHE_TTL, maxsize=2048)


def _actor_cache_key(raw: str) -> str:
    return f"workama:actor:{hash_secret(raw)[:24]}"


async def get_actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    workama_access: Annotated[str | None, Cookie()] = None,
) -> Actor:
    if credentials is None and not workama_access:
        raise HTTPException(status_code=401, detail="Authentication required")
    raw = credentials.credentials if credentials is not None else workama_access
    if raw.startswith("sa-wama-"):
        # Service-account credentials are resolved by the enterprise module so
        # the shared actor path never needs to see their plaintext token.
        from workama_platform.modules.enterprise import authenticate_service_account_token

        service_account = await authenticate_service_account_token(raw)
        if not service_account:
            raise HTTPException(status_code=401, detail="Service-account token is invalid or expired")
        async with pool.connection() as conn:
            result = await conn.execute(
                """
                SELECT u.id AS user_id, u.email, u.display_name, u.onboarding_completed,
                       m.workspace_id, m.org_id
                FROM id_user u JOIN id_member m ON m.user_id=u.id AND m.workspace_id=%s
                WHERE u.id=%s AND m.org_id=%s AND u.status='active'
                LIMIT 1
                """,
                (service_account["workspace_id"], service_account["owner_user_id"], service_account["org_id"]),
            )
            actor = await result.fetchone()
        if not actor:
            raise HTTPException(status_code=401, detail="Service-account owner or workspace unavailable")
        org_id_var.set(actor["org_id"])
        workspace_id_var.set(actor["workspace_id"])
        return Actor(
            **actor,
            role="service_account",
            actor_type="service_account",
            credential_id=service_account["service_account_id"],
            capabilities=tuple(service_account["scopes"]),
            auth_strength=1,
        )
    if raw.startswith("sk-wama-"):
        async with pool.connection() as conn:
            result = await conn.execute(
                """SELECT u.id user_id,u.email,u.display_name,u.onboarding_completed,m.workspace_id,m.org_id,m.role,
                          k.id credential_id,k.scopes,k.resource_allowlist
                   FROM id_api_key k JOIN id_user u ON u.id=k.actor_user_id
                   JOIN id_member m ON m.user_id=u.id AND m.workspace_id=k.workspace_id
                   WHERE k.key_hash=%s AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at>now())
                     AND u.status='active'""",
                (hash_secret(raw),),
            )
            actor = await result.fetchone()
            if not actor or not platform_key_scope_allows(actor["scopes"], _request_capability(request)):
                raise HTTPException(status_code=403 if actor else 401, detail="API key is invalid or lacks required scope")
            if actor["resource_allowlist"] and not any(resource in request.url.path for resource in actor["resource_allowlist"]):
                raise HTTPException(status_code=403, detail="API key resource allowlist denied this request")
            await conn.execute("UPDATE id_api_key SET last_used_at=now() WHERE id=%s", (actor["credential_id"],))
            await conn.commit()
        scopes = tuple(actor.pop("scopes")); actor.pop("resource_allowlist")
        org_id_var.set(actor["org_id"]); workspace_id_var.set(actor["workspace_id"])
        return Actor(**actor, actor_type="api_key", capabilities=scopes, auth_strength=1)
    payload = await decode_token_cached(raw)
    # L1 进程内缓存（最快路径）：命中即返回，跳过 Redis 跨容器 RTT 与 DB。
    if _ACTOR_CACHE_ENABLED:
        try:
            _l1 = _ACTOR_LOCAL.get(_actor_cache_key(raw))
            if _l1 is not None:
                return _l1
        except Exception:
            pass
    # L2 redis 缓存：命中跳过 per-request DB 查询（P99 主因），并回填 L1。
    if _ACTOR_CACHE_ENABLED:
        try:
            _cached = await cache_get(_actor_cache_key(raw))
            if _cached:
                _d = json.loads(_cached)
                _actor = Actor(
                    user_id=_d["user_id"],
                    workspace_id=_d["workspace_id"],
                    org_id=_d["org_id"],
                    role=_d["role"],
                    email=_d["email"],
                    display_name=_d["display_name"],
                    onboarding_completed=_d["onboarding_completed"],
                    capabilities=tuple(_d["capabilities"]),
                    auth_strength=_d["auth_strength"],
                )
                try:
                    _ACTOR_LOCAL.set(_actor_cache_key(raw), _actor)
                except Exception:
                    pass
                return _actor
        except Exception:
            pass
    async with pool.connection() as conn:
        row = await conn.execute(
            """
            SELECT u.id AS user_id, u.email, u.display_name, u.onboarding_completed,
                   m.workspace_id, m.org_id, m.role
            FROM id_user u
            JOIN id_member m ON m.user_id = u.id
            WHERE u.id = %s AND m.workspace_id = %s AND u.status = 'active'
            """,
            (payload["sub"], payload["ws"]),
        )
        actor = await row.fetchone()
    if not actor:
        raise HTTPException(status_code=401, detail="Account or workspace unavailable")
    org_id_var.set(actor["org_id"])
    workspace_id_var.set(actor["workspace_id"])
    built = Actor(
        **actor,
        capabilities=ROLE_CAPABILITIES.get(actor["role"], ()),
        auth_strength=max(1, min(int(payload.get("auth_strength", 1)), 2)),
    )
    if _ACTOR_CACHE_ENABLED:
        try:
            _ACTOR_LOCAL.set(_actor_cache_key(raw), built)
        except Exception:
            pass
        try:
            await cache_set(
                _actor_cache_key(raw),
                json_dumps(
                    {
                        "user_id": built.user_id,
                        "workspace_id": built.workspace_id,
                        "org_id": built.org_id,
                        "role": built.role,
                        "email": built.email,
                        "display_name": built.display_name,
                        "onboarding_completed": built.onboarding_completed,
                        "capabilities": list(built.capabilities),
                        "auth_strength": built.auth_strength,
                    }
                ),
                _ACTOR_CACHE_TTL,
            )
        except Exception:
            pass
    return built


def require_roles(*roles: str):
    async def dependency(actor: Annotated[Actor, Depends(get_actor)]) -> Actor:
        if actor.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of: {', '.join(roles)}",
            )
        return actor

    return dependency


def require_capability(capability: str):
    async def dependency(actor: Annotated[Actor, Depends(get_actor)]) -> Actor:
        if not capability_allows(actor.capabilities, capability):
            raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")
        return actor
    return dependency


async def require_internal(
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    if not x_internal_token or not secrets.compare_digest(
        x_internal_token, settings.internal_token
    ):
        raise HTTPException(status_code=401, detail="Invalid internal service token")


def actor_payload(actor: Actor) -> dict[str, Any]:
    return {
        "id": actor.user_id,
        "email": actor.email,
        "display_name": actor.display_name,
        "workspace_id": actor.workspace_id,
        "org_id": actor.org_id,
        "role": actor.role,
        "onboarding_completed": actor.onboarding_completed,
        "actor_type": actor.actor_type,
        "capabilities": list(actor.capabilities),
        "auth_strength": actor.auth_strength,
    }


def json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=lambda x: x.isoformat() if isinstance(x, datetime) else (float(x) if isinstance(x, Decimal) else None),
    )


async def ensure_runtime_schema() -> None:
    """Apply additive P0 schema changes to existing local development volumes."""
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("ALTER TABLE id_api_key ADD COLUMN IF NOT EXISTS resource_allowlist TEXT[] NOT NULL DEFAULT ARRAY[]::text[]")
            await conn.execute("ALTER TABLE id_api_key ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'file'")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS s3_key TEXT")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS size_bytes BIGINT NOT NULL DEFAULT 0")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS content_sha256 TEXT")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ready'")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS preview JSONB NOT NULL DEFAULT '{}'::jsonb")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS provenance_status TEXT NOT NULL DEFAULT 'not_applicable'")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS provenance_hash TEXT")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS parent_artifact_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[]")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS purge_after TIMESTAMPTZ")
            await conn.execute("ALTER TABLE ag_artifact ADD COLUMN IF NOT EXISTS delete_reason TEXT")
            await conn.execute("ALTER TABLE ag_attachment ADD COLUMN IF NOT EXISTS s3_key TEXT")
            await conn.execute("ALTER TABLE ag_attachment ADD COLUMN IF NOT EXISTS content_sha256 TEXT")
            await conn.execute("ALTER TABLE ag_attachment ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours')")
            await conn.execute("ALTER TABLE ag_attachment ADD COLUMN IF NOT EXISTS parse_error TEXT")
            await conn.execute("ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS agent_kind TEXT NOT NULL DEFAULT 'ama_chat'")
            await conn.execute("ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS model_config JSONB NOT NULL DEFAULT '{}'::jsonb")
            await conn.execute("ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS toolset TEXT[] NOT NULL DEFAULT ARRAY['web_search','file.read','file.write','file.search','code_interpreter','terminal']::text[]")
            await conn.execute("ALTER TABLE ag_session ALTER COLUMN toolset SET DEFAULT ARRAY['web_search','file.read','file.write','file.search','code_interpreter','terminal']::text[]")
            await conn.execute("ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS canvas_enabled BOOLEAN NOT NULL DEFAULT TRUE")
            await conn.execute("ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS prompt_version_id TEXT REFERENCES sec_prompt_version(id) ON DELETE SET NULL")
            await conn.execute("ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS max_steps INTEGER NOT NULL DEFAULT 50")
            await conn.execute("ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS max_credits NUMERIC(18,6) NOT NULL DEFAULT 500")
            await conn.execute("ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS max_duration_seconds INTEGER NOT NULL DEFAULT 3600")
            await conn.execute("ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS used_steps INTEGER NOT NULL DEFAULT 0")
            await conn.execute("ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS used_credits NUMERIC(18,6) NOT NULL DEFAULT 0")
            await conn.execute("ALTER TABLE ag_session ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ")
            await conn.execute("""CREATE TABLE IF NOT EXISTS ag_attachment_upload (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES ag_session(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES id_workspace(id), filename TEXT NOT NULL,
                content_type TEXT NOT NULL, expected_size BIGINT NOT NULL CHECK (expected_size >= 0 AND expected_size <= 5242880),
                expected_sha256 TEXT, s3_key TEXT NOT NULL UNIQUE, token_hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'prepared' CHECK (status IN ('prepared','uploaded','completed','expired')),
                expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '15 minutes'), created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            await conn.execute("""CREATE TABLE IF NOT EXISTS ag_artifact_share (
                id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES ag_artifact(id) ON DELETE CASCADE,
                workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE, expires_at TIMESTAMPTZ NOT NULL,
                max_downloads INTEGER, download_count INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                revoked_at TIMESTAMPTZ, revoke_reason TEXT)""")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_ag_artifact_share_artifact ON ag_artifact_share(artifact_id,created_at DESC)")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ops_feature_flag (
                  id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES id_org(id),
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id), flag_key TEXT NOT NULL,
                  version INTEGER NOT NULL, flag_type TEXT NOT NULL, default_value JSONB NOT NULL DEFAULT 'false'::jsonb,
                  safe_value JSONB NOT NULL DEFAULT 'false'::jsonb, targeting JSONB NOT NULL DEFAULT '{}'::jsonb,
                  salt TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'disabled', owner TEXT NOT NULL,
                  runbook TEXT, metrics JSONB NOT NULL DEFAULT '{}'::jsonb, starts_at TIMESTAMPTZ,
                  ends_at TIMESTAMPTZ, expires_at TIMESTAMPTZ, previous_version INTEGER,
                  content_hash TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, flag_key, version)
                );
                CREATE TABLE IF NOT EXISTS ops_dynamic_config (
                  id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES id_org(id),
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id), config_key TEXT NOT NULL,
                  version INTEGER NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1,
                  value_schema JSONB NOT NULL, config_value JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
                  risk_level TEXT NOT NULL DEFAULT 'normal', effective_at TIMESTAMPTZ, expires_at TIMESTAMPTZ,
                  approved_by TEXT REFERENCES id_user(id), previous_version INTEGER, content_hash TEXT NOT NULL,
                  created_by TEXT NOT NULL REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  UNIQUE(workspace_id, config_key, version)
                );
                CREATE TABLE IF NOT EXISTS ops_event_catalog (
                  event_name TEXT PRIMARY KEY, domain TEXT NOT NULL, event_version INTEGER NOT NULL DEFAULT 1,
                  allowed_properties JSONB NOT NULL DEFAULT '[]'::jsonb, required_properties JSONB NOT NULL DEFAULT '[]'::jsonb,
                  retention_days INTEGER NOT NULL DEFAULT 395, owner TEXT NOT NULL DEFAULT 'product-ops',
                  status TEXT NOT NULL DEFAULT 'active', content_hash TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS ops_product_event (
                  id TEXT PRIMARY KEY, event_name TEXT NOT NULL REFERENCES ops_event_catalog(event_name),
                  event_version INTEGER NOT NULL DEFAULT 1, user_id TEXT REFERENCES id_user(id), org_id TEXT REFERENCES id_org(id),
                  workspace_id TEXT REFERENCES id_workspace(id), client TEXT NOT NULL DEFAULT 'web', client_version TEXT,
                  locale TEXT, region TEXT, session_ref TEXT, experiment_assignments JSONB NOT NULL DEFAULT '{}'::jsonb,
                  properties JSONB NOT NULL DEFAULT '{}'::jsonb, occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_ops_product_event_workspace_time ON ops_product_event(workspace_id, occurred_at DESC);
                CREATE TABLE IF NOT EXISTS ops_release_evidence (
                  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id), release_version TEXT NOT NULL,
                  environment TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', commit_ref TEXT,
                  image_refs JSONB NOT NULL DEFAULT '{}'::jsonb, test_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                  migration_summary JSONB NOT NULL DEFAULT '{}'::jsonb, security_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                  rollback_summary JSONB NOT NULL DEFAULT '{}'::jsonb, approvals JSONB NOT NULL DEFAULT '[]'::jsonb,
                  content_hash TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  UNIQUE(workspace_id, release_version, environment)
                );
                CREATE TABLE IF NOT EXISTS ops_outbox (
                  id TEXT PRIMARY KEY, event_type TEXT NOT NULL, workspace_id TEXT, trace_id TEXT,
                  payload JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                  available_at TIMESTAMPTZ NOT NULL DEFAULT now(), published_at TIMESTAMPTZ,
                  last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_ops_outbox_pending ON ops_outbox(status, available_at, created_at);
                CREATE TABLE IF NOT EXISTS ops_async_operation (
                  id TEXT PRIMARY KEY, operation_type TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1,
                  org_id TEXT NOT NULL REFERENCES id_org(id), workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                  actor_id TEXT NOT NULL REFERENCES id_user(id), actor_role TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL, input_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                  progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100), stage TEXT,
                  cancellable BOOLEAN NOT NULL DEFAULT TRUE, attempt_count INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL DEFAULT 3, result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                  error_code TEXT, error_message TEXT, cancellation_reason TEXT, trace_id TEXT,
                  policy_version TEXT, cancel_requested_at TIMESTAMPTZ, started_at TIMESTAMPTZ,
                  completed_at TIMESTAMPTZ, expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, operation_type, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS ops_job (
                  id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES ops_async_operation(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id), job_type TEXT NOT NULL,
                  schema_version INTEGER NOT NULL DEFAULT 1, queue TEXT NOT NULL DEFAULT 'platform',
                  priority INTEGER NOT NULL DEFAULT 100, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                  payload_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempt_count INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL DEFAULT 3, timeout_seconds INTEGER NOT NULL DEFAULT 300,
                  heartbeat_seconds INTEGER NOT NULL DEFAULT 15, cancellable BOOLEAN NOT NULL DEFAULT TRUE,
                  progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100), stage TEXT,
                  lease_owner TEXT, lease_token TEXT, lease_expires_at TIMESTAMPTZ, heartbeat_at TIMESTAMPTZ,
                  scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(), cancel_requested_at TIMESTAMPTZ,
                  cancellation_reason TEXT, last_error_code TEXT, last_error TEXT, started_at TIMESTAMPTZ,
                  completed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_ops_job_claim ON ops_job(status, scheduled_at, priority DESC);
                CREATE INDEX IF NOT EXISTS idx_ops_job_queue_claim ON ops_job(queue, status, scheduled_at, priority DESC);
                CREATE TABLE IF NOT EXISTS ops_job_run (
                  id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES ops_job(id) ON DELETE CASCADE,
                  attempt INTEGER NOT NULL, worker_id TEXT NOT NULL, status TEXT NOT NULL,
                  started_at TIMESTAMPTZ NOT NULL, heartbeat_at TIMESTAMPTZ, ended_at TIMESTAMPTZ,
                  result_summary JSONB NOT NULL DEFAULT '{}'::jsonb, error_code TEXT, error_summary TEXT,
                  metrics JSONB NOT NULL DEFAULT '{}'::jsonb, UNIQUE(job_id, attempt)
                );
                CREATE TABLE IF NOT EXISTS ops_job_dlq (
                  id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES ops_job(id),
                  operation_id TEXT NOT NULL REFERENCES ops_async_operation(id), workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                  job_type TEXT NOT NULL, payload_hash TEXT NOT NULL, attempts INTEGER NOT NULL,
                  error_code TEXT, error_summary TEXT, failed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  replayed_at TIMESTAMPTZ, replayed_by TEXT REFERENCES id_user(id), replay_reason TEXT, replay_job_id TEXT
                );
                CREATE TABLE IF NOT EXISTS ops_search_document (
                  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                  resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, owner_id TEXT REFERENCES id_user(id),
                  visibility TEXT NOT NULL DEFAULT 'workspace', acl_user_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
                  acl_roles TEXT[] NOT NULL DEFAULT ARRAY[]::text[], acl_version BIGINT NOT NULL DEFAULT 1,
                  title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', tags TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
                  status TEXT NOT NULL DEFAULT 'active', source_version BIGINT NOT NULL,
                  tombstone BOOLEAN NOT NULL DEFAULT FALSE, updated_at TIMESTAMPTZ NOT NULL,
                  indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  search_vector TSVECTOR NOT NULL DEFAULT ''::tsvector,
                  UNIQUE(resource_type, resource_id)
                );
                CREATE INDEX IF NOT EXISTS idx_ops_search_workspace_acl ON ops_search_document(workspace_id, tombstone, visibility, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ops_search_vector ON ops_search_document USING GIN(search_vector);
                CREATE TABLE IF NOT EXISTS ops_workspace_export (
                  id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES ops_async_operation(id),
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id), status TEXT NOT NULL DEFAULT 'queued',
                  manifest JSONB NOT NULL DEFAULT '{}'::jsonb, object_ref TEXT, checksum TEXT,
                  size_bytes BIGINT, expires_at TIMESTAMPTZ, created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS ops_workspace_import (
                  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                  upload_id TEXT NOT NULL UNIQUE, object_ref TEXT, upload_checksum TEXT, status TEXT NOT NULL DEFAULT 'uploading',
                  manifest JSONB NOT NULL DEFAULT '{}'::jsonb, dry_run_report JSONB NOT NULL DEFAULT '{}'::jsonb,
                  id_mapping JSONB NOT NULL DEFAULT '{}'::jsonb, result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                  created_by TEXT NOT NULL REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  uploaded_at TIMESTAMPTZ, completed_at TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS ops_notification_template (
                  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                  template_id TEXT NOT NULL, version INTEGER NOT NULL, locale TEXT NOT NULL DEFAULT 'zh-CN',
                  channel TEXT NOT NULL, subject_template TEXT NOT NULL, body_template TEXT NOT NULL,
                  variables_schema JSONB NOT NULL DEFAULT '{}'::jsonb, sensitive_level TEXT NOT NULL DEFAULT 'C2',
                  status TEXT NOT NULL DEFAULT 'draft', content_hash TEXT NOT NULL,
                  created_by TEXT NOT NULL REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  published_at TIMESTAMPTZ, UNIQUE(workspace_id, template_id, version, locale, channel)
                );
                CREATE TABLE IF NOT EXISTS ops_lifecycle_policy (
                  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                  resource_type TEXT NOT NULL, retention_days INTEGER NOT NULL, batch_size INTEGER NOT NULL DEFAULT 100,
                  status TEXT NOT NULL DEFAULT 'enabled', runbook TEXT NOT NULL, updated_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  UNIQUE(workspace_id, resource_type)
                );
                CREATE TABLE IF NOT EXISTS ops_lifecycle_run (
                  id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES ops_async_operation(id),
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id), resource_type TEXT NOT NULL,
                  dry_run BOOLEAN NOT NULL, status TEXT NOT NULL DEFAULT 'queued', eligible_count INTEGER NOT NULL DEFAULT 0,
                  processed_count INTEGER NOT NULL DEFAULT 0, skipped_hold_count INTEGER NOT NULL DEFAULT 0,
                  verification JSONB NOT NULL DEFAULT '{}'::jsonb, created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ
                );
                CREATE TABLE IF NOT EXISTS sec_legal_hold (
                  id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                  resource_type TEXT NOT NULL, resource_id TEXT, basis TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                  approved_by TEXT NOT NULL REFERENCES id_user(id), starts_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  expires_at TIMESTAMPTZ, released_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS idx_sec_legal_hold_active ON sec_legal_hold(workspace_id, resource_type, status);
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gw_model_mapping (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                    model TEXT NOT NULL,
                    channel_id TEXT NOT NULL REFERENCES gw_channel(id) ON DELETE CASCADE,
                    upstream_model TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(workspace_id, model, channel_id)
                )
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gw_model_mapping_workspace_model
                ON gw_model_mapping(workspace_id, model)
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gw_token_group (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                    name TEXT NOT NULL,
                    rpm_limit INTEGER NOT NULL DEFAULT 600,
                    tpm_limit INTEGER NOT NULL DEFAULT 1000000,
                    model_whitelist TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
                    pinned_channel_id TEXT,
                    fallback_chain JSONB NOT NULL DEFAULT '{}'::jsonb,
                    model_mapping_override JSONB NOT NULL DEFAULT '{}'::jsonb,
                    status TEXT NOT NULL DEFAULT 'enabled',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(workspace_id, name)
                )
                """
            )
            await conn.execute(
                "ALTER TABLE gw_token ADD COLUMN IF NOT EXISTS pinned_channel_id TEXT"
            )
            await conn.execute(
                "ALTER TABLE gw_token ADD COLUMN IF NOT EXISTS group_id TEXT"
            )
            await conn.execute(
                "ALTER TABLE gw_token_group ADD COLUMN IF NOT EXISTS fallback_chain JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
            await conn.execute(
                "ALTER TABLE gw_token_group ADD COLUMN IF NOT EXISTS model_mapping_override JSONB NOT NULL DEFAULT '{}'::jsonb"
            )
            await conn.execute(
                "ALTER TABLE bill_account ADD COLUMN IF NOT EXISTS frozen_balance NUMERIC(18,6) NOT NULL DEFAULT 0"
            )
            await conn.execute(
                "ALTER TABLE bill_account ALTER COLUMN granted_balance TYPE NUMERIC(18,6), ALTER COLUMN purchased_balance TYPE NUMERIC(18,6)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bill_reservation (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                    request_id TEXT NOT NULL UNIQUE,
                    model TEXT NOT NULL,
                    estimated_cost NUMERIC(18,6) NOT NULL,
                    actual_cost NUMERIC(18,6),
                    status TEXT NOT NULL CHECK (status IN ('frozen', 'settled', 'released')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    settled_at TIMESTAMPTZ
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bill_reservation_workspace_status ON bill_reservation(workspace_id, status, created_at DESC)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bill_credit_grant (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                    subscription_id TEXT,
                    source TEXT NOT NULL CHECK (source IN ('initial','subscription','manual','migration')),
                    period_start TIMESTAMPTZ NOT NULL,
                    expires_at TIMESTAMPTZ,
                    initial_amount NUMERIC(18,6) NOT NULL CHECK (initial_amount > 0),
                    remaining_amount NUMERIC(18,6) NOT NULL CHECK (remaining_amount >= 0 AND remaining_amount <= initial_amount),
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','exhausted','expired')),
                    idempotency_key TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    expired_at TIMESTAMPTZ,
                    UNIQUE(workspace_id, idempotency_key)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bill_credit_grant_expiry ON bill_credit_grant(workspace_id,status,expires_at,period_start)"
            )
            await conn.execute(
                """
                INSERT INTO bill_credit_grant(
                    id,workspace_id,source,period_start,initial_amount,remaining_amount,status,idempotency_key
                )
                SELECT 'grant_migration_' || a.id,a.workspace_id,'migration',date_trunc('month',now()),
                       a.granted_balance,a.granted_balance,'active','migration:' || a.workspace_id
                FROM bill_account a
                WHERE a.granted_balance > 0
                ON CONFLICT(workspace_id,idempotency_key) DO NOTHING
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bill_usage_hourly (
                    workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                    resource TEXT NOT NULL DEFAULT 'llm', model TEXT NOT NULL,
                    hour TIMESTAMPTZ NOT NULL, requests BIGINT NOT NULL DEFAULT 0,
                    prompt_tokens BIGINT NOT NULL DEFAULT 0,
                    completion_tokens BIGINT NOT NULL DEFAULT 0,
                    cost_credits NUMERIC(18,6) NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY(workspace_id, resource, model, hour)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bill_reconciliation_run (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                    business_date DATE NOT NULL, usage_credits NUMERIC(18,6) NOT NULL,
                    ledger_credits NUMERIC(18,6) NOT NULL, difference NUMERIC(18,6) NOT NULL,
                    difference_ratio NUMERIC(18,6) NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('passed', 'mismatch')),
                    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(workspace_id, business_date)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bill_usage_export (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
                    workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','completed','failed','expired')),
                    format TEXT NOT NULL CHECK (format IN ('jsonl','csv')),
                    filter_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    record_count INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT,
                    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_by TEXT NOT NULL REFERENCES id_user(id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours')
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bill_usage_export_workspace_time ON bill_usage_export(workspace_id, created_at DESC)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ops_inbox (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    consumer_name TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    last_error TEXT,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    processed_at TIMESTAMPTZ,
                    request_id TEXT,
                    UNIQUE(event_id, consumer_name)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ops_inbox_consumer_status ON ops_inbox(consumer_name, status, received_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ops_inbox_request_id ON ops_inbox(request_id)"
            )
            await conn.execute(
                "ALTER TABLE id_user ALTER COLUMN email_verified SET DEFAULT FALSE"
            )
            await conn.execute(
                "ALTER TABLE id_user ADD COLUMN IF NOT EXISTS failed_login_count INTEGER NOT NULL DEFAULT 0"
            )
            await conn.execute(
                "ALTER TABLE id_user ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ"
            )
            await conn.execute(
                "ALTER TABLE id_refresh_token ADD COLUMN IF NOT EXISTS family_id TEXT"
            )
            await conn.execute(
                "ALTER TABLE id_refresh_token ADD COLUMN IF NOT EXISTS parent_id TEXT REFERENCES id_refresh_token(id)"
            )
            await conn.execute(
                "ALTER TABLE id_refresh_token ADD COLUMN IF NOT EXISTS rotated_to_id TEXT REFERENCES id_refresh_token(id)"
            )
            await conn.execute(
                "ALTER TABLE id_refresh_token ADD COLUMN IF NOT EXISTS workspace_id TEXT"
            )
            await conn.execute(
                "ALTER TABLE id_refresh_token ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            )
            await conn.execute(
                "UPDATE id_refresh_token r SET workspace_id = m.workspace_id"
                " FROM (SELECT DISTINCT ON (user_id) user_id, workspace_id FROM id_member ORDER BY user_id, created_at) m"
                " WHERE r.workspace_id IS NULL AND m.workspace_id IS NOT NULL AND r.user_id = m.user_id"
            )
            await conn.execute(
                "UPDATE id_refresh_token SET family_id = id WHERE family_id IS NULL"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_id_refresh_token_family ON id_refresh_token(family_id)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS id_auth_token (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
                    token_type TEXT NOT NULL CHECK (token_type IN ('email_verify', 'password_reset')),
                    token_hash TEXT NOT NULL UNIQUE, expires_at TIMESTAMPTZ NOT NULL,
                    consumed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_id_auth_token_user_type ON id_auth_token(user_id, token_type, created_at DESC)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS id_mfa_factor (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
                    factor_type TEXT NOT NULL DEFAULT 'totp' CHECK (factor_type IN ('totp')),
                    secret_enc TEXT NOT NULL, confirmed_at TIMESTAMPTZ, disabled_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(user_id, factor_type)
                )
                """
            )
            await conn.execute(
                "ALTER TABLE id_consent ADD COLUMN IF NOT EXISTS locale TEXT NOT NULL DEFAULT 'zh-CN'"
            )
            await conn.execute("ALTER TABLE id_consent ADD COLUMN IF NOT EXISTS display_text_hash TEXT NOT NULL DEFAULT ''")
            await conn.execute("ALTER TABLE id_consent ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'web'")
            await conn.execute("ALTER TABLE id_consent ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '{}'::jsonb")
            await conn.execute("ALTER TABLE id_consent ADD COLUMN IF NOT EXISTS withdrawn_at TIMESTAMPTZ")
            await conn.execute("ALTER TABLE id_data_request ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'content'")
            await conn.execute("ALTER TABLE id_data_request ADD COLUMN IF NOT EXISTS identity_verified_at TIMESTAMPTZ")
            await conn.execute("ALTER TABLE id_data_request ADD COLUMN IF NOT EXISTS result_manifest JSONB")
            await conn.execute("ALTER TABLE id_data_request ADD COLUMN IF NOT EXISTS result_checksum TEXT")
            await conn.execute("ALTER TABLE id_data_request ADD COLUMN IF NOT EXISTS exceptions JSONB NOT NULL DEFAULT '[]'::jsonb")
            await conn.execute("ALTER TABLE id_data_request ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS id_data_request_step (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES id_data_request(id) ON DELETE CASCADE,
                    step_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','running','completed','failed','skipped')),
                    resource_count INTEGER NOT NULL DEFAULT 0, action TEXT, checksum TEXT, error TEXT,
                    started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, UNIQUE(request_id, step_name)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS id_deletion_tombstone (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES id_data_request(id),
                    user_id TEXT NOT NULL REFERENCES id_user(id), workspace_id TEXT NOT NULL REFERENCES id_workspace(id),
                    scope TEXT NOT NULL, replay_version INTEGER NOT NULL DEFAULT 1,
                    resource_counts JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(request_id, scope)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS id_processing_activity (
                    table_name TEXT PRIMARY KEY, classification TEXT NOT NULL CHECK (classification IN ('C0','C1','C2','C3','C4')),
                    purpose TEXT NOT NULL, owner TEXT NOT NULL, region TEXT NOT NULL,
                    retention_days INTEGER NOT NULL, deletion_behavior TEXT NOT NULL,
                    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sec_policy (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL UNIQUE REFERENCES id_workspace(id) ON DELETE CASCADE,
                    input_action TEXT NOT NULL DEFAULT 'log' CHECK (input_action IN ('block','mask','log')),
                    output_action TEXT NOT NULL DEFAULT 'block' CHECK (output_action IN ('block','mask','log')),
                    blocked_terms TEXT[] NOT NULL DEFAULT ARRAY['api_key','system prompt','身份证号']::text[],
                    autonomy_level TEXT NOT NULL DEFAULT 'A2' CHECK (autonomy_level IN ('A1','A2','A3','A4')),
                    domain_allowlist TEXT[] NOT NULL DEFAULT ARRAY[]::text[], domain_denylist TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
                    updated_by TEXT REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sec_moderation_log (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                    direction TEXT NOT NULL CHECK (direction IN ('input','output')),
                    action TEXT NOT NULL CHECK (action IN ('block','mask','log')), matched_terms TEXT[] NOT NULL,
                    content_hash TEXT NOT NULL, request_id TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sec_moderation_log_workspace_time ON sec_moderation_log(workspace_id, created_at DESC)")
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS ag_approval (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL REFERENCES ag_session(id) ON DELETE CASCADE, call_id TEXT NOT NULL,
                    requester_id TEXT NOT NULL REFERENCES id_user(id), tool_name TEXT NOT NULL, action_hash TEXT NOT NULL,
                    risk TEXT NOT NULL CHECK (risk IN ('A3','A4')), preview JSONB NOT NULL DEFAULT '{}'::jsonb,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','expired','consumed')),
                    reason TEXT, decided_by TEXT REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    expires_at TIMESTAMPTZ NOT NULL, decided_at TIMESTAMPTZ, consumed_at TIMESTAMPTZ,
                    UNIQUE(workspace_id, call_id))"""
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_ag_approval_workspace_status ON ag_approval(workspace_id,status,created_at DESC)")
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS ag_tool_grant (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                    tool_name TEXT NOT NULL, scope TEXT NOT NULL CHECK (scope IN ('workspace','session')),
                    session_id TEXT REFERENCES ag_session(id) ON DELETE CASCADE, max_risk TEXT NOT NULL CHECK (max_risk IN ('A1','A2')),
                    created_by TEXT NOT NULL REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    expires_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ, revoke_reason TEXT)"""
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_ag_tool_grant_workspace_active ON ag_tool_grant(workspace_id,tool_name,expires_at) WHERE revoked_at IS NULL")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sec_prompt_version (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                    name TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL, checksum TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','archived')),
                    created_by TEXT NOT NULL REFERENCES id_user(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    published_at TIMESTAMPTZ, UNIQUE(workspace_id, name, version)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sec_eval_run (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                    prompt_version_id TEXT NOT NULL REFERENCES sec_prompt_version(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK (status IN ('passed','failed')), total_cases INTEGER NOT NULL,
                    passed_cases INTEGER NOT NULL, failures JSONB NOT NULL DEFAULT '[]'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS id_notification (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES id_user(id),
                    workspace_id TEXT NOT NULL REFERENCES id_workspace(id), event_type TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'normal', title TEXT NOT NULL, summary TEXT NOT NULL,
                    action_url TEXT, payload_min JSONB NOT NULL DEFAULT '{}'::jsonb,
                    resource_ref TEXT, dedupe_key TEXT NOT NULL, read_at TIMESTAMPTZ,
                    archived_at TIMESTAMPTZ, expires_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(user_id, dedupe_key)
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_id_notification_user_time ON id_notification(user_id, created_at DESC)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS id_notification_delivery (
                    id TEXT PRIMARY KEY, notification_id TEXT NOT NULL REFERENCES id_notification(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL, provider TEXT, attempt INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
                    provider_id TEXT, error_class TEXT, next_attempt_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(notification_id, channel)
                )
                """
            )
            # pgvector is provided by the production-compatible PostgreSQL image.
            # Keeping the column dimensionless allows each embedding profile to
            # own its dimension while profile-specific HNSW indexes stay valid.
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pf_dataset (
                  id TEXT PRIMARY KEY,
                  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  name TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  embedding_model TEXT NOT NULL DEFAULT 'workama-embed',
                  embedding_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
                  retrieval_config JSONB NOT NULL DEFAULT '{"top_k":5,"candidate_k":20,"rrf_k":60,"score_threshold":0}'::jsonb,
                  stats JSONB NOT NULL DEFAULT '{"document_count":0,"chunk_count":0}'::jsonb,
                  active_generation_id TEXT,
                  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleting','deleted')),
                  version INTEGER NOT NULL DEFAULT 1,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  deleted_at TIMESTAMPTZ,
                  UNIQUE(workspace_id, name)
                );
                CREATE TABLE IF NOT EXISTS pf_index_generation (
                  id TEXT PRIMARY KEY,
                  dataset_id TEXT NOT NULL REFERENCES pf_dataset(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  generation INTEGER NOT NULL,
                  embedding_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
                  status TEXT NOT NULL DEFAULT 'building' CHECK (status IN ('building','ready','active','retired','failed')),
                  document_count INTEGER NOT NULL DEFAULT 0,
                  chunk_count INTEGER NOT NULL DEFAULT 0,
                  error TEXT,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  activated_at TIMESTAMPTZ,
                  completed_at TIMESTAMPTZ,
                  UNIQUE(dataset_id, generation)
                );
                CREATE TABLE IF NOT EXISTS pf_document (
                  id TEXT PRIMARY KEY,
                  dataset_id TEXT NOT NULL REFERENCES pf_dataset(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  name TEXT NOT NULL,
                  source TEXT NOT NULL CHECK (source IN ('upload','url','connector')),
                  source_url TEXT,
                  s3_key TEXT NOT NULL,
                  mime TEXT NOT NULL,
                  size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0 AND size_bytes <= 104857600),
                  content_sha256 TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','parsing','chunking','embedding','indexed','failed','cancelled','deleting','deleted')),
                  error TEXT,
                  chunk_count INTEGER NOT NULL DEFAULT 0,
                  connector_id TEXT,
                  source_object_id TEXT,
                  source_version TEXT,
                  acl_hash TEXT,
                  sync_id TEXT,
                  version INTEGER NOT NULL DEFAULT 1,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  indexed_at TIMESTAMPTZ,
                  deleted_at TIMESTAMPTZ,
                  UNIQUE(dataset_id, content_sha256, deleted_at)
                );
                CREATE TABLE IF NOT EXISTS pf_chunk (
                  id TEXT PRIMARY KEY,
                  document_id TEXT NOT NULL REFERENCES pf_document(id) ON DELETE CASCADE,
                  dataset_id TEXT NOT NULL REFERENCES pf_dataset(id) ON DELETE CASCADE,
                  generation_id TEXT NOT NULL REFERENCES pf_index_generation(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  content TEXT NOT NULL,
                  content_sha256 TEXT NOT NULL,
                  token_count INTEGER NOT NULL CHECK (token_count >= 0),
                  position INTEGER NOT NULL CHECK (position >= 0),
                  parent_id TEXT,
                  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                  tsv TSVECTOR NOT NULL,
                  embedding VECTOR,
                  embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0 AND embedding_dimension <= 4096),
                  version INTEGER NOT NULL DEFAULT 1,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  UNIQUE(document_id, generation_id, position)
                );
                CREATE INDEX IF NOT EXISTS idx_pf_dataset_workspace_status ON pf_dataset(workspace_id, status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_pf_document_dataset_status ON pf_document(dataset_id, status, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_pf_document_active_content ON pf_document(dataset_id, content_sha256) WHERE deleted_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_pf_chunk_dataset_generation_position ON pf_chunk(dataset_id, generation_id, position);
                CREATE INDEX IF NOT EXISTS idx_pf_chunk_tsv ON pf_chunk USING GIN(tsv);
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pf_eval_set (
                  id TEXT PRIMARY KEY,
                  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  name TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  domain TEXT NOT NULL DEFAULT 'rag',
                  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                  dataset_id TEXT REFERENCES pf_dataset(id) ON DELETE SET NULL,
                  sampling_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
                  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','archived')),
                  idempotency_key TEXT,
                  input_hash TEXT NOT NULL,
                  resource_version INTEGER NOT NULL DEFAULT 1,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  deleted_at TIMESTAMPTZ,
                  delete_reason TEXT,
                  UNIQUE(workspace_id, name, version),
                  UNIQUE(workspace_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS pf_eval_case (
                  id TEXT PRIMARY KEY,
                  eval_set_id TEXT NOT NULL REFERENCES pf_eval_set(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  query TEXT NOT NULL,
                  expected_chunk_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
                  expected_answer TEXT,
                  forbidden TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
                  labels JSONB NOT NULL DEFAULT '{}'::jsonb,
                  provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
                  case_hash TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted')),
                  version INTEGER NOT NULL DEFAULT 1,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  deleted_at TIMESTAMPTZ,
                  UNIQUE(eval_set_id, case_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_pf_eval_case_set_status ON pf_eval_case(eval_set_id, status, created_at);
                CREATE TABLE IF NOT EXISTS pf_eval_run (
                  id TEXT PRIMARY KEY,
                  eval_set_id TEXT NOT NULL REFERENCES pf_eval_set(id) ON DELETE CASCADE,
                  dataset_id TEXT NOT NULL REFERENCES pf_dataset(id) ON DELETE RESTRICT,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  generation_id TEXT NOT NULL REFERENCES pf_index_generation(id) ON DELETE RESTRICT,
                  operation_id TEXT NOT NULL UNIQUE REFERENCES ops_async_operation(id) ON DELETE RESTRICT,
                  config JSONB NOT NULL DEFAULT '{}'::jsonb,
                  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
                  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
                  evidence_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
                  error TEXT,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  started_at TIMESTAMPTZ,
                  completed_at TIMESTAMPTZ,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_pf_eval_run_workspace_time ON pf_eval_run(workspace_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS pf_rag_feedback (
                  id TEXT PRIMARY KEY,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  dataset_id TEXT NOT NULL REFERENCES pf_dataset(id) ON DELETE CASCADE,
                  query TEXT NOT NULL,
                  chunk_ids TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
                  rating SMALLINT NOT NULL CHECK (rating BETWEEN -1 AND 1),
                  comment TEXT,
                  eval_run_id TEXT REFERENCES pf_eval_run(id) ON DELETE SET NULL,
                  eval_case_id TEXT REFERENCES pf_eval_case(id) ON DELETE SET NULL,
                  idempotency_key TEXT NOT NULL,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  UNIQUE(workspace_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_pf_rag_feedback_dataset_time ON pf_rag_feedback(dataset_id, created_at DESC);
                """
            )
            await conn.execute(
                """
                -- T-M3-003: 知识库评测集/标注回流相关表
                CREATE TABLE IF NOT EXISTS kb_eval_set (
                  id TEXT PRIMARY KEY,
                  dataset_id TEXT NOT NULL REFERENCES pf_dataset(id) ON DELETE CASCADE,
                  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  name TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  metrics JSONB NOT NULL DEFAULT '["retrieval_recall","retrieval_precision","answer_relevance"]'::jsonb,
                  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
                  resource_version INTEGER NOT NULL DEFAULT 1,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  UNIQUE(workspace_id, dataset_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_kb_eval_set_dataset ON kb_eval_set(dataset_id, status, created_at DESC);
                CREATE TABLE IF NOT EXISTS kb_eval_case (
                  id TEXT PRIMARY KEY,
                  eval_set_id TEXT NOT NULL REFERENCES kb_eval_set(id) ON DELETE CASCADE,
                  dataset_id TEXT NOT NULL REFERENCES pf_dataset(id) ON DELETE CASCADE,
                  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  question TEXT NOT NULL,
                  expected_answer TEXT NOT NULL DEFAULT '',
                  expected_chunks JSONB NOT NULL DEFAULT '[]'::jsonb,
                  tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                  case_hash TEXT,
                  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted')),
                  version INTEGER NOT NULL DEFAULT 1,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  deleted_at TIMESTAMPTZ,
                  UNIQUE(eval_set_id, case_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_kb_eval_case_set_status ON kb_eval_case(eval_set_id, status, created_at);
                CREATE TABLE IF NOT EXISTS kb_eval_run (
                  id TEXT PRIMARY KEY,
                  eval_set_id TEXT NOT NULL REFERENCES kb_eval_set(id) ON DELETE CASCADE,
                  dataset_id TEXT NOT NULL REFERENCES pf_dataset(id) ON DELETE RESTRICT,
                  generation_id TEXT NOT NULL REFERENCES pf_index_generation(id) ON DELETE RESTRICT,
                  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  operation_id TEXT NOT NULL UNIQUE REFERENCES ops_async_operation(id) ON DELETE RESTRICT,
                  config JSONB NOT NULL DEFAULT '{}'::jsonb,
                  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','cancelled')),
                  metrics_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                  error TEXT,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  started_at TIMESTAMPTZ,
                  completed_at TIMESTAMPTZ,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_kb_eval_run_workspace_time ON kb_eval_run(workspace_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS kb_eval_result (
                  id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL REFERENCES kb_eval_run(id) ON DELETE CASCADE,
                  case_id TEXT NOT NULL REFERENCES kb_eval_case(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  question TEXT NOT NULL,
                  retrieved_chunks JSONB NOT NULL DEFAULT '[]'::jsonb,
                  generated_answer TEXT NOT NULL DEFAULT '',
                  metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
                  latency_ms INTEGER NOT NULL DEFAULT 0,
                  error TEXT,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_kb_eval_result_run ON kb_eval_result(run_id, created_at);
                CREATE TABLE IF NOT EXISTS kb_eval_annotation (
                  id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL REFERENCES kb_eval_run(id) ON DELETE CASCADE,
                  case_id TEXT NOT NULL REFERENCES kb_eval_case(id) ON DELETE CASCADE,
                  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  rating INTEGER CHECK (rating BETWEEN 1 AND 5),
                  feedback TEXT NOT NULL DEFAULT '',
                  corrected_answer TEXT NOT NULL DEFAULT '',
                  corrected_chunks JSONB NOT NULL DEFAULT '[]'::jsonb,
                  labels JSONB NOT NULL DEFAULT '[]'::jsonb,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  UNIQUE(run_id, case_id, created_by)
                );
                CREATE INDEX IF NOT EXISTS idx_kb_eval_annotation_run ON kb_eval_annotation(run_id, created_at DESC);
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ag_memory (
                  id TEXT PRIMARY KEY,
                  org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
                  workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
                  user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
                  kind TEXT NOT NULL CHECK (kind IN ('profile','episodic','semantic')),
                  memory_key TEXT NOT NULL,
                  content TEXT NOT NULL,
                  source_session_id TEXT REFERENCES ag_session(id) ON DELETE SET NULL,
                  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted','expired')),
                  expires_at TIMESTAMPTZ,
                  created_by TEXT NOT NULL REFERENCES id_user(id),
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  deleted_at TIMESTAMPTZ,
                  UNIQUE(workspace_id, user_id, kind, memory_key)
                );
                CREATE INDEX IF NOT EXISTS idx_ag_memory_owner_status ON ag_memory(workspace_id, user_id, status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ag_memory_search ON ag_memory USING GIN (to_tsvector('simple', memory_key || ' ' || content));
                """
            )
            await conn.execute("ALTER TABLE ag_memory DROP CONSTRAINT IF EXISTS ag_memory_kind_check")
            await conn.execute("ALTER TABLE ag_memory ADD CONSTRAINT ag_memory_kind_check CHECK (kind IN ('profile','episodic','semantic'))")
            await conn.execute("ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS importance NUMERIC(4,3) NOT NULL DEFAULT 0.5")
            await conn.execute("ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS confidence NUMERIC(4,3) NOT NULL DEFAULT 0.5")
            await conn.execute("ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS retention_policy TEXT NOT NULL DEFAULT 'standard'")
            await conn.execute("ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS semantic_embedding JSONB NOT NULL DEFAULT '[]'::jsonb")
            await conn.execute("ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS semantic_version TEXT NOT NULL DEFAULT 'local-hash-v1'")
            await conn.execute("ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ")
            await conn.execute("ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS forgotten_at TIMESTAMPTZ")
            await conn.execute("ALTER TABLE ag_memory ADD COLUMN IF NOT EXISTS forget_reason TEXT")
            await conn.execute("ALTER TABLE ag_memory DROP CONSTRAINT IF EXISTS ag_memory_retention_policy_check")
            await conn.execute("ALTER TABLE ag_memory ADD CONSTRAINT ag_memory_retention_policy_check CHECK (retention_policy IN ('standard','session','indefinite'))")
            await conn.execute("ALTER TABLE ag_memory DROP CONSTRAINT IF EXISTS ag_memory_importance_check")
            await conn.execute("ALTER TABLE ag_memory ADD CONSTRAINT ag_memory_importance_check CHECK (importance >= 0 AND importance <= 1)")
            await conn.execute("ALTER TABLE ag_memory DROP CONSTRAINT IF EXISTS ag_memory_confidence_check")
            await conn.execute("ALTER TABLE ag_memory ADD CONSTRAINT ag_memory_confidence_check CHECK (confidence >= 0 AND confidence <= 1)")
            # 记忆真实向量索引表（pgvector，1536 维）
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_vector (
                    memory_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding vector(1536),
                    metadata JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    access_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_memory_vector_workspace ON memory_vector(workspace_id);
                CREATE INDEX IF NOT EXISTS idx_memory_vector_embedding ON memory_vector USING ivfflat (embedding vector_cosine_ops);
                """
            )

