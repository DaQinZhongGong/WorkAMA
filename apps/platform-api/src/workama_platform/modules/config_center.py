"""配置中心（Config Center）—— 以可视化界面取代 .env 的运行时配置系统。

设计目标（生产级，非 demo/POC）：
- **单一可信源**：所有可运维配置存于 ``config_settings`` 表，由可视化控制台编辑，
  优先级 **DB(UI) > ENV(代码启动时读取的 .env) > 代码默认**。
- **实时热生效**：写入后立刻覆盖进程内 ``core.settings`` 单例（对请求期读取的配置
  如限流/SMTP/OAuth/特性开关等立即生效），并递增 Redis 版本号；其它 worker 通过
  版本号轮询（≤1s 惰性）收敛到同一最新值。
- **安全**：密钥类字段落库时 Fernet 加密；API 读取只返回掩码，更新时支持"保持不变"哨兵。
- **可审计 / 可回滚**：每次发布生成全局 revision + 全量快照（``config_revision``），
  逐键差异进入 ``config_history``，支持一键回滚到任意历史 revision。
- **诚实边界**：``restart_required`` 标记的配置（如 database_url / encryption_key /
  jwt 密钥 / 内部 token）写入即成为权威值并在下次重启生效；运行期已初始化的资源
  （连接池 / Fernet 实例）不会自动重建，UI 会显式提示需重启。

本模块不依赖任何外部组件；DB/Redis 不可用时退化为 ENV/默认（与现有 ``Settings`` 行为一致）。
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import time
from dataclasses import dataclass, field as dc_field
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    encrypt_secret,
    decrypt_secret,
    get_actor,
    json_dumps,
    new_id,
    pool,
    redis,
    require_internal,
    settings,
)

router = APIRouter(prefix="/api/v1/config", tags=["config-center"])
internal_router = APIRouter(prefix="/internal/config", tags=["config-center-internal"])

# 更新密钥时前端可传此哨兵表示"保持当前值不变"（避免把掩码写回库）。
KEEP_SENTINEL = "********"
# 写入后覆盖 settings 的字段集合（仅这些会在运行期真正热生效；其余标记为需重启）。
REDIS_VERSION_KEY = "workama:config:version"

# ---------------------------------------------------------------------------
# Schema 目录：声明每一项可配置字段的元数据
# ---------------------------------------------------------------------------


@dataclass
class ConfigField:
    key: str
    group: str
    label: str
    type: str = "str"  # str|int|bool|list|url|email|enum
    default: Any = ""
    required: bool = False
    secret: bool = False
    restart_required: bool = False
    choices: tuple[str, ...] = ()
    min: Optional[int] = None
    max: Optional[int] = None
    pattern: str = ""
    help: str = ""


GROUP_LABELS = {
    "infra": "基础设施",
    "secrets": "安全密钥",
    "auth": "认证与授权",
    "oauth": "OAuth 登录",
    "smtp": "邮件 (SMTP)",
    "notify": "通知 Webhook",
    "storage": "对象存储 (MinIO)",
    "services": "内部服务地址",
    "billing": "计费",
    "pool": "数据库连接池",
    "redis_ha": "Redis 高可用",
    "setup": "初始化",
    "llm_staging": "LLM 覆盖渠道 (staging)",
}

SCHEMA: list[ConfigField] = [
    # 基础设施（均为启动期生效，需重启）
    ConfigField("database_url", "infra", "数据库连接串", type="url", required=True,
                secret=True, restart_required=True, help="PostgreSQL DSN，含用户名/密码，落库加密。"),
    ConfigField("redis_url", "infra", "Redis 连接串", type="url", required=True,
                restart_required=True, help="redis://host:port/db，运行期连接池需重启生效。"),
    ConfigField("nats_url", "infra", "NATS 连接串", type="url", restart_required=True,
                help="nats://host:port。"),
    ConfigField("workama_env", "infra", "运行环境", type="enum",
                choices=("development", "test", "staging", "production"),
                restart_required=True, help="development/test/staging/production。"),
    ConfigField("default_region", "infra", "默认数据驻留区域", type="enum",
                choices=("CN", "EU", "US", "SG"), help="海外区数据驻留默认区域。"),
    # 安全密钥（密钥材料，运行期已初始化，需重启）
    ConfigField("jwt_secret", "secrets", "JWT 共享密钥", secret=True, restart_required=True,
                help="HS256 共享密钥（未配置 RSA 密钥时在 dev 使用）。"),
    ConfigField("key_pepper", "secrets", "密钥胡椒", secret=True, restart_required=True,
                help="用于口令/密钥哈希的胡椒值。"),
    ConfigField("encryption_key", "secrets", "字段加密密钥", secret=True, restart_required=True,
                help="Fernet 密钥（32 url-safe base64 字节）。"),
    ConfigField("internal_token", "secrets", "内部服务令牌", secret=True, restart_required=True,
                help="服务间内部调用 X-Internal-Token。"),
    # 认证与授权（请求期读取，热生效）
    ConfigField("auth_debug_tokens", "auth", "调试令牌", type="bool", default=False,
                help="允许使用调试令牌（仅非生产）。"),
    ConfigField("password_min_length", "auth", "口令最小长度", type="int", default=12,
                min=8, max=64, help="注册/改密口令最小长度。"),
    ConfigField("rate_limit_login_per_min", "auth", "登录限流(次/分)", type="int", default=5,
                min=1, max=1000, help="单 IP/账户登录尝试频率上限。"),
    ConfigField("rate_limit_sensitive_per_min", "auth", "敏感操作限流(次/分)", type="int",
                default=10, min=1, max=1000, help="敏感操作频率上限。"),
    ConfigField("rate_limit_default_per_min", "auth", "默认限流(次/分)", type="int", default=60,
                min=1, max=10000, help="通用 API 默认频率上限（运行期热生效）。"),
    ConfigField("cors_origins", "auth", "CORS 来源", type="str",
                default="http://localhost:20204", help="逗号分隔的受信前端来源（需重启 CORS 中间件）。"),
    ConfigField("trusted_origins", "auth", "可信来源", type="list",
                default=["http://localhost:20204", "http://localhost:20205"],
                help="逗号分隔的可信来源列表（CSRF 等）。"),
    # OAuth
    ConfigField("auth_oauth_enabled", "oauth", "启用 OAuth 登录", type="bool", default=False,
                help="开启后允许 OAuth 第三方登录。"),
    ConfigField("oauth_redirect_base_url", "oauth", "OAuth 回调基址", type="url",
                default="http://localhost:8000", help="OAuth 回调基址。"),
    ConfigField("github_oauth_client_id", "oauth", "GitHub Client ID", default=""),
    ConfigField("github_oauth_client_secret", "oauth", "GitHub Client Secret", secret=True),
    ConfigField("github_oauth_authorization_url", "oauth", "GitHub 授权地址", type="url",
                default="https://github.com/login/oauth/authorize"),
    ConfigField("google_oauth_client_id", "oauth", "Google Client ID", default=""),
    ConfigField("google_oauth_client_secret", "oauth", "Google Client Secret", secret=True),
    ConfigField("google_oauth_authorization_url", "oauth", "Google 授权地址", type="url",
                default="https://accounts.google.com/o/oauth2/v2/auth"),
    # SMTP（请求期读取，热生效）
    ConfigField("smtp_mock", "smtp", "SMTP 模拟投递", type="bool", default=True,
                help="true 时走确定性 mock 路径（不真实发信）。"),
    ConfigField("smtp_host", "smtp", "SMTP 主机", default=""),
    ConfigField("smtp_port", "smtp", "SMTP 端口", type="int", default=587, min=1, max=65535),
    ConfigField("smtp_from", "smtp", "发件人", type="email", default="notifications@workama.local"),
    ConfigField("smtp_username", "smtp", "SMTP 用户名", default=""),
    ConfigField("smtp_password", "smtp", "SMTP 密码", secret=True),
    ConfigField("smtp_use_tls", "smtp", "SMTP 使用 TLS", type="bool", default=True),
    # 通知
    ConfigField("notification_webhook_mock", "notify", "Webhook 模拟投递", type="bool", default=True,
                help="true 时走 mock:// 确定性签名。"),
    # 对象存储
    ConfigField("minio_endpoint", "storage", "MinIO 端点", default="localhost:9000",
                help="host:port。"),
    ConfigField("minio_access_key", "storage", "MinIO Access Key", default="workama"),
    ConfigField("minio_secret_key", "storage", "MinIO Secret Key", secret=True),
    ConfigField("minio_secure", "storage", "MinIO 使用 HTTPS", type="bool", default=False),
    # 内部服务地址
    ConfigField("gateway_url", "services", "网关地址", type="url", default="http://gateway:8080"),
    ConfigField("agent_server_url", "services", "Agent 服务地址", type="url",
                default="http://agent-server:8001"),
    ConfigField("sandbox_fleet_url", "services", "沙箱集群地址", type="url",
                default="http://sandbox-fleet:8002"),
    # 计费
    ConfigField("billing_mock_webhook_secret", "billing", "计费 Webhook 密钥", secret=True,
                default="workama-mock-provider-secret"),
    # 连接池
    ConfigField("db_pool_min_size", "pool", "连接池最小连接", type="int", default=5,
                min=1, max=200, restart_required=True),
    ConfigField("db_pool_max_size", "pool", "连接池最大连接", type="int", default=20,
                min=1, max=1000, restart_required=True),
    # Redis 高可用
    ConfigField("redis_sentinels", "redis_ha", "Redis 哨兵列表", type="list", default=[],
                help="逗号分隔 host:port（为空走单节点）。", restart_required=True),
    ConfigField("redis_master_name", "redis_ha", "Redis 主节点名", default="",
                restart_required=True),
    # 初始化
    ConfigField("setup_token", "setup", "初始化令牌", secret=True, default=""),
    # LLM 覆盖渠道（staging）：经 /internal/config/export 加密下发，Go 网关轮询热应用。
    # 优先于 DB 渠道，失败自动回退 DB 渠道；关闭/清空即恢复内置渠道路由。
    ConfigField("llm_staging_enabled", "llm_staging", "启用覆盖渠道", type="bool", default=False,
                help="开启后网关优先把 chat/completions 转发到该真实上游；失败回退 DB 渠道。"),
    ConfigField("llm_staging_provider", "llm_staging", "供应商协议", type="enum",
                choices=("openai", "openai-compatible", "azure"), default="openai-compatible",
                help="解析对应适配器；与 gw_channel.provider 语义一致。"),
    ConfigField("llm_staging_base_url", "llm_staging", "上游 Base URL", type="url",
                default="https://api.openai.com/v1",
                help="OpenAI 兼容基址（含 /v1）；网关向其 POST {base}/chat/completions。"),
    ConfigField("llm_staging_api_key", "llm_staging", "上游 API Key", secret=True, default="",
                help="Bearer 凭据；落库加密，仅以密文形式下发网关解密使用。"),
    ConfigField("llm_staging_model", "llm_staging", "上游模型名", default="",
                help="留空时网关沿用请求中的 model 名直传。"),
]

_SCHEMA_BY_KEY: dict[str, ConfigField] = {f.key: f for f in SCHEMA}


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


# ---------------------------------------------------------------------------
# 类型校验 / 编解码
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def validate_value(field: ConfigField, value: Any) -> None:
    """校验单个值是否符合字段约束，不通过抛 ``ValueError``。"""
    if isinstance(value, str) and value == KEEP_SENTINEL:
        # 保持当前值（即便必填项也允许，因为库中已有值）
        return
    if value is None:
        if field.required:
            raise ValueError(f"{field.key} 为必填项")
        return
    t = field.type
    if t == "int":
        try:
            iv = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field.key} 必须为整数")
        if field.min is not None and iv < field.min:
            raise ValueError(f"{field.key} 不能小于 {field.min}")
        if field.max is not None and iv > field.max:
            raise ValueError(f"{field.key} 不能大于 {field.max}")
    elif t == "bool":
        if not isinstance(value, bool) and str(value).lower() not in {
            "0", "1", "true", "false", "yes", "no", "on", "off",
        }:
            raise ValueError(f"{field.key} 必须为布尔值")
    elif t == "enum":
        if value not in field.choices:
            raise ValueError(f"{field.key} 必须为 {field.choices} 之一")
    elif t == "email":
        if not _EMAIL_RE.match(str(value)):
            raise ValueError(f"{field.key} 不是合法邮箱")
    elif t == "url":
        if not _URL_RE.match(str(value)):
            raise ValueError(f"{field.key} 必须是合法 URL（含 scheme，如 http://）")
    elif t == "list":
        items = value if isinstance(value, list) else [v for v in str(value).split(",") if v.strip()]
        if field.pattern:
            for it in items:
                if not re.match(field.pattern, it):
                    raise ValueError(f"{field.key} 含非法项：{it}")


def _coerce_in(field: ConfigField, value: Any) -> str:
    """把 API 传入的 Python 值序列化为库内存储字符串。"""
    t = field.type
    if t == "int":
        return str(int(value))
    if t == "bool":
        b = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}
        return "true" if b else "false"
    if t == "list":
        items = value if isinstance(value, list) else [v for v in str(value).split(",") if v.strip()]
        return ",".join(str(i).strip() for i in items)
    return str(value)


def _coerce_out(field: ConfigField, raw: Optional[str]) -> Any:
    """把库内存储字符串还原为运行期 Python 值。"""
    if raw is None:
        return None
    t = field.type
    if t == "int":
        return int(raw)
    if t == "bool":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if t == "list":
        return [x for x in raw.split(",") if x.strip()] if raw else []
    return raw


def _mask(field: ConfigField) -> str:
    return KEEP_SENTINEL


# ---------------------------------------------------------------------------
# DB Schema
# ---------------------------------------------------------------------------


async def ensure_config_schema() -> None:
    """幂等建表（与 ensure_runtime_schema 风格一致）。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS config_settings (
                  key TEXT PRIMARY KEY,
                  group_key TEXT NOT NULL,
                  value TEXT,
                  value_type TEXT NOT NULL DEFAULT 'str',
                  is_secret BOOLEAN NOT NULL DEFAULT FALSE,
                  is_encrypted BOOLEAN NOT NULL DEFAULT FALSE,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_by TEXT
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS config_history (
                  id TEXT PRIMARY KEY,
                  revision INTEGER NOT NULL,
                  key TEXT NOT NULL,
                  group_key TEXT NOT NULL,
                  old_value TEXT,
                  new_value TEXT,
                  old_is_encrypted BOOLEAN NOT NULL DEFAULT FALSE,
                  new_is_encrypted BOOLEAN NOT NULL DEFAULT FALSE,
                  changed_by TEXT,
                  changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_config_history_key ON config_history(key, changed_at DESC)"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS config_revision (
                  id TEXT PRIMARY KEY,
                  revision INTEGER NOT NULL UNIQUE,
                  snapshot_json JSONB NOT NULL,
                  changed_by TEXT,
                  changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  note TEXT
                )
                """
            )


# ---------------------------------------------------------------------------
# 解析 / 热生效
# ---------------------------------------------------------------------------


async def _redis_get_version() -> int:
    try:
        raw = await redis.get(REDIS_VERSION_KEY)
        return int(raw) if raw else 0
    except Exception:
        return 0


async def _redis_bump_version() -> int:
    try:
        return int(await redis.incr(REDIS_VERSION_KEY))
    except Exception:
        return 0


_LOCAL: dict[str, Any] = {"version": -1, "ts": 0.0, "snapshot": {}}


async def _read_db_rows() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    async with pool.connection() as conn:
        res = await conn.execute(
            "SELECT key, group_key, value, value_type, is_secret, is_encrypted FROM config_settings"
        )
        async for row in res:
            out[row["key"]] = dict(row)
    return out


async def _build_effective() -> dict[str, dict[str, Any]]:
    """构建当前生效配置视图（含来源判别与密钥掩码）。"""
    rows = await _read_db_rows()
    eff: dict[str, dict[str, Any]] = {}
    for f in SCHEMA:
        entry = rows.get(f.key)
        if entry and entry.get("value") is not None:
            raw = entry["value"]
            if entry.get("is_encrypted"):
                try:
                    raw = decrypt_secret(raw)
                except Exception:
                    raw = None
            value = _coerce_out(f, raw)
            source = "db"
        else:
            env_val = getattr(settings, f.key, None)
            if env_val is not None and env_val != f.default and env_val != "":
                value = env_val
                source = "env"
            else:
                value = f.default
                source = "default"
        eff[f.key] = {
            "key": f.key,
            "value": _mask(f) if f.secret else value,
            "secret": f.secret,
            "secret_set": bool(f.secret and source == "db"),
            "source": source,
            "restart_required": f.restart_required,
        }
    return eff


async def get_effective_config(force: bool = False) -> dict[str, dict[str, Any]]:
    """带版本号轮询的生效配置视图（≤1s 惰性刷新，跨 worker 收敛）。"""
    now = time.monotonic()
    ver = await _redis_get_version()
    if force or _LOCAL["version"] != ver or now - _LOCAL["ts"] > 1.0:
        _LOCAL["snapshot"] = await _build_effective()
        _LOCAL["version"] = ver
        _LOCAL["ts"] = now
    return _LOCAL["snapshot"]


# 进程内基线：首次应用前对 settings 的原始快照（ENV 合并默认后的值）。
# 删除 UI 覆盖时按基线回落，避免旧覆盖值残留在进程内。
_BASELINE: dict[str, Any] = {"obj": None, "values": {}}


def _baseline_values() -> dict[str, Any]:
    s = settings
    if _BASELINE["obj"] is not s:
        _BASELINE["values"] = {f.key: getattr(s, f.key, f.default) for f in SCHEMA}
        _BASELINE["obj"] = s
    return _BASELINE["values"]


async def load_and_apply_config_overrides() -> None:
    """启动时把 DB 中的 UI 配置覆盖到 ``core.settings`` 单例（UI 优先级最高，持久生效）。

    仅覆盖 schema 中声明的键；密钥字段解密后写入。运行期已初始化的资源
    （连接池 / Fernet / JWT 密钥）不会自动重建，相关键标记 ``restart_required``。
    应用前先把全部键恢复到基线（ENV/默认），保证删除覆盖后正确回落，
    不会残留上一轮的覆盖值。
    """
    rows = await _read_db_rows()
    base = _baseline_values()
    for f in SCHEMA:
        try:
            setattr(settings, f.key, base[f.key])
        except Exception:
            continue
    for f in SCHEMA:
        entry = rows.get(f.key)
        if not entry or entry.get("value") is None:
            continue
        raw = entry["value"]
        if entry.get("is_encrypted"):
            try:
                raw = decrypt_secret(raw)
            except Exception:
                continue
        if raw is None:
            continue
        try:
            setattr(settings, f.key, _coerce_out(f, raw))
        except Exception:
            # 类型不兼容时跳过该键，避免破坏启动
            continue
    # 初始化版本号，保证后续轮询能检测到变更
    if await _redis_get_version() == 0:
        try:
            await redis.set(REDIS_VERSION_KEY, 0)
        except Exception:
            pass


async def apply_overrides_to_settings() -> None:
    """运行期变更后（PUT/回滚）重新把 DB 覆盖到 settings 单例并刷新本地快照。"""
    await load_and_apply_config_overrides()
    _LOCAL["version"] = await _redis_get_version()
    _LOCAL["snapshot"] = await _build_effective()
    _LOCAL["ts"] = time.monotonic()


async def config_watcher_loop(interval: float = 1.0) -> None:
    """跨进程热收敛循环：检测 Redis 版本号变化并把 UI 配置重应用到本进程。

    Granian 多 worker / platform-worker / rag-worker 各进程独立持有 settings
    单例；一次 PUT 只会即时覆盖处理请求的那个进程。该循环让其余进程在
    ``≤interval`` 秒内收敛到同一最新值。循环内任何异常都被吞掉（配置刷新
    永不拖垮业务主循环）；取消时正常退出。
    """
    while True:
        try:
            await asyncio.sleep(interval)
            ver = await _redis_get_version()
            if ver != _LOCAL["version"]:
                await apply_overrides_to_settings()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 配置刷新失败不影响主流程，下轮重试
            continue


async def _current_revision() -> int:
    async with pool.connection() as conn:
        res = await conn.execute("SELECT COALESCE(MAX(revision), 0) AS m FROM config_revision")
        row = await res.fetchone()
        return int(row["m"]) if row else 0


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class ConfigItem(BaseModel):
    key: str
    value: Any = None


class ConfigUpdate(BaseModel):
    items: list[ConfigItem] = Field(min_length=1, max_length=200)
    note: str = ""


class ConfigRollback(BaseModel):
    revision: int
    note: str = ""


class ConfigTest(BaseModel):
    key: str
    value: Any = None  # 可选覆盖值（用于"先用这个值探测再保存"）


# ---------------------------------------------------------------------------
# 端点（管理面）
# ---------------------------------------------------------------------------


@router.get("/schema")
async def get_schema(actor: Annotated[Actor, Depends(get_actor)]):
    """返回配置目录（分组 + 元数据 + 当前生效值 + 来源 + 是否需重启）。"""
    _require_admin(actor)
    eff = await get_effective_config()
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in SCHEMA:
        e = eff.get(f.key, {})
        groups.setdefault(f.group, []).append({
            "key": f.key,
            "label": f.label,
            "type": f.type,
            "value": e.get("value"),
            "secret": f.secret,
            "secret_set": e.get("secret_set", False),
            "source": e.get("source", "default"),
            "restart_required": f.restart_required,
            "required": f.required,
            "choices": list(f.choices),
            "min": f.min,
            "max": f.max,
            "help": f.help,
        })
    return {
        "groups": [
            {"key": g, "label": GROUP_LABELS.get(g, g), "fields": fields}
            for g, fields in groups.items()
        ],
        "version": await _redis_get_version(),
    }


@router.get("/values")
async def get_values(actor: Annotated[Actor, Depends(get_actor)]):
    """返回当前生效值（密钥掩码）。"""
    _require_admin(actor)
    eff = await get_effective_config()
    return {"version": await _redis_get_version(), "values": eff}


@router.put("/values")
async def put_values(
    body: ConfigUpdate, actor: Annotated[Actor, Depends(get_actor)]
):
    """批量更新配置：校验 → 写库（密钥加密）→ 审计 → 发布版本 → 热生效。"""
    _require_admin(actor)
    # 1) 预校验，避免部分写入
    prepared: list[tuple[ConfigField, Optional[str]]] = []
    for item in body.items:
        f = _SCHEMA_BY_KEY.get(item.key)
        if not f:
            raise HTTPException(status_code=422, detail=f"未知配置项：{item.key}")
        if isinstance(item.value, str) and item.value == KEEP_SENTINEL:
            continue  # 保持当前值
        try:
            validate_value(f, item.value)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        store = _coerce_in(f, item.value) if item.value is not None else None
        prepared.append((f, store))

    async with pool.connection() as conn:
        async with conn.transaction():
            # 当前行（用于差异审计）
            cur = await conn.execute(
                "SELECT key, value, is_encrypted FROM config_settings WHERE key = ANY(%s)",
                ([f.key for f, _ in prepared],),
            )
            existing: dict[str, dict[str, Any]] = {r["key"]: dict(r) for r in await cur.fetchall()}

            revision = await _current_revision() + 1
            history_rows: list[tuple] = []
            for f, store in prepared:
                old = existing.get(f.key)
                old_value = old["value"] if old else None
                old_enc = bool(old["is_encrypted"]) if old else False
                new_enc = bool(f.secret)
                new_value = encrypt_secret(store) if (f.secret and store is not None) else store
                await conn.execute(
                    """
                    INSERT INTO config_settings
                      (key, group_key, value, value_type, is_secret, is_encrypted, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET
                      value = EXCLUDED.value, value_type = EXCLUDED.value_type,
                      is_secret = EXCLUDED.is_secret, is_encrypted = EXCLUDED.is_encrypted,
                      updated_at = now(), updated_by = EXCLUDED.updated_by
                    """,
                    (f.key, f.group, new_value, f.type, f.secret, new_enc, actor.user_id),
                )
                history_rows.append((
                    new_id("cfg"), revision, f.key, f.group,
                    old_value, new_value, old_enc, new_enc, actor.user_id,
                ))
            if history_rows:
                _HIST_SQL = """
                    INSERT INTO config_history
                      (id, revision, key, group_key, old_value, new_value,
                       old_is_encrypted, new_is_encrypted, changed_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                for hr in history_rows:
                    await conn.execute(_HIST_SQL, hr)
            # 全量快照（用于回滚）
            snapshot = await _snapshot_now(conn)
            await conn.execute(
                """
                INSERT INTO config_revision (id, revision, snapshot_json, changed_by, note)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (new_id("rev"), revision, json_dumps(snapshot), actor.user_id, body.note or f"rev {revision}"),
            )
    # 2) 发布：递增版本号 + 运行期热生效
    await _redis_bump_version()
    await apply_overrides_to_settings()
    eff = await get_effective_config(force=True)
    return {
        "version": await _redis_get_version(),
        "revision": revision,
        "values": eff,
        "restart_required": sorted({
            eff[k]["key"] for k in eff if eff[k]["restart_required"]
            and eff[k]["source"] == "db"
        }),
    }


async def _snapshot_now(conn) -> dict[str, Any]:
    """读取当前全量配置（解密后的明文值）作为回滚快照。"""
    res = await conn.execute(
        "SELECT key, value, is_encrypted FROM config_settings"
    )
    snap: dict[str, Any] = {}
    async for row in res:
        raw = row["value"]
        if row["is_encrypted"]:
            try:
                raw = decrypt_secret(raw)
            except Exception:
                raw = None
        snap[row["key"]] = raw
    return snap


@router.get("/history")
async def get_history(
    key: str | None = None,
    limit: int = 50,
    actor: Annotated[Actor, Depends(get_actor)] = None,
):
    _require_admin(actor)
    limit = max(1, min(limit, 500))
    async with pool.connection() as conn:
        if key:
            res = await conn.execute(
                """
                SELECT id, revision, key, old_value, new_value, old_is_encrypted,
                       new_is_encrypted, changed_by, changed_at
                FROM config_history WHERE key = %s
                ORDER BY changed_at DESC LIMIT %s
                """,
                (key, limit),
            )
        else:
            res = await conn.execute(
                """
                SELECT id, revision, key, old_value, new_value, old_is_encrypted,
                       new_is_encrypted, changed_by, changed_at
                FROM config_history ORDER BY changed_at DESC LIMIT %s
                """,
                (limit,),
            )
        rows = await res.fetchall()
    return {
        "items": [
            {
                "id": r["id"],
                "revision": r["revision"],
                "key": r["key"],
                "old_value": "********" if r["old_is_encrypted"] else r["old_value"],
                "new_value": "********" if r["new_is_encrypted"] else r["new_value"],
                "changed_by": r["changed_by"],
                "changed_at": r["changed_at"].isoformat() if r["changed_at"] else None,
            }
            for r in rows
        ]
    }


@router.get("/revisions")
async def get_revisions(
    limit: int = 50, actor: Annotated[Actor, Depends(get_actor)] = None
):
    _require_admin(actor)
    limit = max(1, min(limit, 500))
    async with pool.connection() as conn:
        res = await conn.execute(
            """
            SELECT id, revision, changed_by, changed_at, note
            FROM config_revision ORDER BY revision DESC LIMIT %s
            """,
            (limit,),
        )
        rows = await res.fetchall()
    return {
        "items": [
            {
                "id": r["id"],
                "revision": r["revision"],
                "changed_by": r["changed_by"],
                "changed_at": r["changed_at"].isoformat() if r["changed_at"] else None,
                "note": r["note"],
            }
            for r in rows
        ]
    }


@router.post("/rollback")
async def rollback(
    body: ConfigRollback, actor: Annotated[Actor, Depends(get_actor)]
):
    """回滚到指定 revision 的全量快照。"""
    _require_admin(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            res = await conn.execute(
                "SELECT snapshot_json FROM config_revision WHERE revision = %s",
                (body.revision,),
            )
            row = await res.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"revision {body.revision} 不存在")
            snapshot: dict[str, Any] = row["snapshot_json"] or {}
            # 当前行
            cur = await conn.execute("SELECT key, value, is_encrypted FROM config_settings")
            existing = {r["key"]: dict(r) for r in await cur.fetchall()}
            revision = await _current_revision() + 1
            history_rows: list[tuple] = []
            for f in SCHEMA:
                new_raw = snapshot.get(f.key)
                if new_raw is None:
                    continue
                old = existing.get(f.key)
                old_value = old["value"] if old else None
                old_enc = bool(old["is_encrypted"]) if old else False
                new_enc = bool(f.secret)
                new_value = encrypt_secret(new_raw) if f.secret else new_raw
                await conn.execute(
                    """
                    INSERT INTO config_settings
                      (key, group_key, value, value_type, is_secret, is_encrypted, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET
                      value = EXCLUDED.value, value_type = EXCLUDED.value_type,
                      is_secret = EXCLUDED.is_secret, is_encrypted = EXCLUDED.is_encrypted,
                      updated_at = now(), updated_by = EXCLUDED.updated_by
                    """,
                    (f.key, f.group, new_value, f.type, f.secret, new_enc, actor.user_id),
                )
                history_rows.append((
                    new_id("cfg"), revision, f.key, f.group,
                    old_value, new_value, old_enc, new_enc, actor.user_id,
                ))
            if history_rows:
                _HIST_SQL = """
                    INSERT INTO config_history
                      (id, revision, key, group_key, old_value, new_value,
                       old_is_encrypted, new_is_encrypted, changed_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                for hr in history_rows:
                    await conn.execute(_HIST_SQL, hr)
            await conn.execute(
                """
                INSERT INTO config_revision (id, revision, snapshot_json, changed_by, note)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (new_id("rev"), revision, json_dumps(snapshot), actor.user_id,
                 body.note or f"rollback to rev {body.revision}"),
            )
    await _redis_bump_version()
    await apply_overrides_to_settings()
    eff = await get_effective_config(force=True)
    return {"version": await _redis_get_version(), "revision": revision, "values": eff}


@router.delete("/values/{key}")
async def delete_value(key: str, actor: Annotated[Actor, Depends(get_actor)]):
    """删除单键的 UI 覆盖：该键回落到 ENV / 代码默认（优先级链的自然结果）。

    与发布一致：写审计历史（old→删除）+ 生成新 revision 快照 + 版本号热生效。
    密钥行的历史 old_value 以掩码呈现，不泄露明文。
    """
    _require_admin(actor)
    f = _SCHEMA_BY_KEY.get(key)
    if not f:
        raise HTTPException(status_code=422, detail=f"未知配置项：{key}")
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT key, value, is_encrypted FROM config_settings WHERE key = %s",
                (key,),
            )
            row = await cur.fetchone()
            if not row:
                return {
                    "deleted": False,
                    "key": key,
                    "version": await _redis_get_version(),
                    "note": "该配置项当前无 UI 覆盖",
                }
            revision = await _current_revision() + 1
            await conn.execute("DELETE FROM config_settings WHERE key = %s", (key,))
            await conn.execute(
                """
                INSERT INTO config_history
                  (id, revision, key, group_key, old_value, new_value,
                   old_is_encrypted, new_is_encrypted, changed_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (new_id("cfg"), revision, key, f.group,
                 row["value"], None, bool(row["is_encrypted"]), False, actor.user_id),
            )
            snapshot = await _snapshot_now(conn)
            await conn.execute(
                """
                INSERT INTO config_revision (id, revision, snapshot_json, changed_by, note)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (new_id("rev"), revision, json_dumps(snapshot), actor.user_id,
                 f"delete override {key}"),
            )
    await _redis_bump_version()
    await apply_overrides_to_settings()
    eff = await get_effective_config(force=True)
    return {
        "deleted": True,
        "key": key,
        "revision": revision,
        "version": await _redis_get_version(),
        "source": eff.get(key, {}).get("source"),
        "value": None if f.secret else eff.get(key, {}).get("value"),
    }


@router.post("/test")
async def test_connection(
    body: ConfigTest, actor: Annotated[Actor, Depends(get_actor)]
):
    """连接探测：对含 host:port 的配置做 TCP 连通性校验（不发送凭据）。

    用于保存前验证 SMTP / MinIO / Redis / NATS / 网关 等端点可达。
    """
    _require_admin(actor)
    f = _SCHEMA_BY_KEY.get(body.key)
    if not f:
        raise HTTPException(status_code=422, detail=f"未知配置项：{body.key}")
    # 决定待测值：覆盖值优先，否则取当前生效值
    if body.value is not None and not (isinstance(body.value, str) and body.value == KEEP_SENTINEL):
        target = str(body.value)
    else:
        eff = await get_effective_config()
        target = str(eff.get(body.key, {}).get("value") or f.default)
    host, port = _parse_host_port(f, target)
    if not host or not port:
        return {"ok": True, "note": "无可探测的 host:port，跳过连通性校验", "key": body.key}
    ok, detail = _tcp_probe(host, port, timeout=3.0)
    return {"ok": ok, "detail": detail, "key": body.key, "host": host, "port": port}


def _parse_host_port(f: ConfigField, value: str) -> tuple[str, int]:
    if f.type == "url":
        m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^:/]+)(?::(\d+))?", value)
        if m:
            return m.group(1), int(m.group(2) or 0)
    elif f.key == "minio_endpoint":
        m = re.match(r"^([^:]+)(?::(\d+))?", value)
        if m:
            return m.group(1), int(m.group(2) or 0)
    elif f.type == "list":
        # redis_sentinels: 取第一个
        first = value.split(",")[0].strip()
        m = re.match(r"^([^:]+)(?::(\d+))?", first)
        if m:
            return m.group(1), int(m.group(2) or 0)
    return "", 0


def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    if not port:
        return False, "端口缺失"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"成功连接到 {host}:{port}"
    except Exception as e:  # noqa: BLE001
        return False, f"无法连接到 {host}:{port}：{e}"


# ---------------------------------------------------------------------------
# 内部端点（供网关 / 其它控制面服务拉取生效配置）
# ---------------------------------------------------------------------------


@internal_router.get("/export")
async def export_runtime(actor: Annotated[Actor, Depends(require_internal)]):
    """导出当前生效配置（JSON），供网关等组件按版本轮询消费。

    - ``values``：非密钥字段的生效值（明文）；
    - ``secrets``：库内已加密密钥字段的 **Fernet 密文原样**（绝不导出明文）。
      消费方（Go 网关）用与 platform-api 相同的 ENCRYPTION_KEY 解密。

    网关据此热应用 llm_staging_* 覆盖渠道；version 变化即代表有新发布。
    """
    eff = await get_effective_config(force=True)
    rows = await _read_db_rows()
    out: dict[str, Any] = {
        "version": await _redis_get_version(),
        "values": {},
        "secrets": {},
    }
    for k, v in eff.items():
        if v.get("secret"):
            continue
        out["values"][k] = v.get("value")
    for k, row in rows.items():
        f = _SCHEMA_BY_KEY.get(k)
        if not f or not f.secret:
            continue
        raw = row.get("value")
        if row.get("is_encrypted") and raw:
            # 密文直传：消费方持相同 ENCRYPTION_KEY 解密；解密能力缺失时自然跳过。
            out["secrets"][k] = raw
    return out
