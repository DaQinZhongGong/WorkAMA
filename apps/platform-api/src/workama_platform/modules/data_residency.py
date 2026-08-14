"""Data residency: region routing middleware + compliance endpoints.

Exports:
- ``RegionRoutingMiddleware``: ASGI middleware enforcing workspace region_policy
  on ``/api/v1/`` requests. Skips ``/api/v1/system/`` health checks and requests
  without a parseable Bearer JWT (public endpoints).
- ``require_region_compliance``: FastAPI Dependency for data export / migration
  endpoints; raises 403 when the requested region is not allowed by policy.
- ``router``: region-policy query, region compliance check, and cross-border
  transfer audit endpoints.

The module reuses ``compliance.region_allows`` for policy evaluation and
``audit_exports.append_audit_chain`` for audit logging. It does not modify
either module.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    decode_token_cached,
    get_actor,
    json_dumps,
    new_id,
    pool,
    require_internal,
    settings,
)
from workama_platform.modules.audit_exports import append_audit_chain
from workama_platform.modules.compliance import region_allows


router = APIRouter(prefix="/api/v1/enterprise/compliance", tags=["enterprise-data-residency"])

# Module-level region policy cache: workspace_id -> (policy_or_None, monotonic_expiry).
# Simple in-process TTL cache so the middleware does not hit the DB on every request.
_REGION_POLICY_CACHE_TTL_SECONDS = 300
_region_policy_cache: dict[str, tuple[dict[str, Any] | None, float]] = {}


def _home_region() -> str:
    """Safe access to settings.home_region (defaults to 'us-east-1' when absent)."""
    return str(getattr(settings, "home_region", "us-east-1") or "us-east-1")


def _cache_get(workspace_id: str) -> tuple[dict[str, Any] | None, bool]:
    """Return (policy_or_None, cache_hit). cache_hit=False means stale or missing."""
    entry = _region_policy_cache.get(workspace_id)
    if entry is None:
        return None, False
    policy, expires_at = entry
    if time.monotonic() > expires_at:
        _region_policy_cache.pop(workspace_id, None)
        return None, False
    return policy, True


def _cache_set(workspace_id: str, policy: dict[str, Any] | None) -> None:
    _region_policy_cache[workspace_id] = (policy, time.monotonic() + _REGION_POLICY_CACHE_TTL_SECONDS)


def invalidate_region_policy_cache(workspace_id: str) -> None:
    """Invalidate cached region policy for a workspace (call on policy update)."""
    _region_policy_cache.pop(workspace_id, None)


async def _fetch_region_policy(workspace_id: str) -> dict[str, Any] | None:
    """Fetch the active region policy for a workspace (or None if missing)."""
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM sec_region_policy WHERE workspace_id=%s AND status='active'",
            (workspace_id,),
        )
        row = await result.fetchone()
    return dict(row) if row else None


async def _get_region_policy_cached(workspace_id: str) -> dict[str, Any] | None:
    """Return cached policy if fresh, otherwise fetch from DB and cache it."""
    policy, hit = _cache_get(workspace_id)
    if hit:
        return policy
    policy = await _fetch_region_policy(workspace_id)
    _cache_set(workspace_id, policy)
    return policy


def _scope_header(headers: list[tuple[bytes, bytes]], name: str) -> str:
    """Case-insensitive header lookup from ASGI scope headers (decoded value or '')."""
    target = name.lower().encode("latin-1")
    for key, value in headers:
        if key.lower() == target:
            try:
                return value.decode("latin-1").strip()
            except Exception:
                return ""
    return ""


def _json_403_response_body(requested_region: str, home_region: str) -> bytes:
    return json_dumps(
        {
            "detail": "region not allowed",
            "requested_region": requested_region,
            "home_region": home_region,
        }
    ).encode("utf-8")


# ============================================================================
# 1. Region routing ASGI middleware
# ============================================================================


class RegionRoutingMiddleware:
    """ASGI middleware: enforce workspace region_policy on ``/api/v1/`` requests.

    - Skips non-http scopes and non-``/api/v1/`` paths.
    - Skips ``/api/v1/system/`` health-check endpoints.
    - Skips requests without a parseable Bearer JWT (public endpoints).
    - Queries workspace ``sec_region_policy`` (cached 5 min in-process).
    - 403 when ``residency_required`` and request region != ``home_region``.
    - 403 when ``cross_border_mode='deny'`` and provider region not allowed.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        # Only enforce on /api/v1/ routes; skip /api/v1/system/ health checks.
        if not path.startswith("/api/v1/") or path.startswith("/api/v1/system/"):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        auth_header = _scope_header(headers, "authorization")
        if not auth_header:
            # No Authorization header: public endpoint, let downstream decide.
            await self.app(scope, receive, send)
            return

        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            await self.app(scope, receive, send)
            return

        token = parts[1].strip()
        if not token:
            await self.app(scope, receive, send)
            return

        # Decode JWT to extract workspace_id. On any failure, skip and let
        # get_actor return the proper 401 for invalid/expired tokens.
        try:
            payload = await decode_token_cached(token)
        except Exception:
            await self.app(scope, receive, send)
            return

        workspace_id = payload.get("ws")
        if not workspace_id:
            await self.app(scope, receive, send)
            return

        try:
            policy = await _get_region_policy_cached(workspace_id)
        except Exception:
            # DB error: fail open to preserve availability (audit catches misuse).
            await self.app(scope, receive, send)
            return

        # No policy configured = no residency restriction for this workspace.
        if not policy:
            await self.app(scope, receive, send)
            return

        requested_region = _scope_header(headers, "x-workama-region") or _home_region()
        provider_region = _home_region()

        if not region_allows(policy, requested_region, provider_region):
            home_region = str(policy.get("home_region") or _home_region())
            body = _json_403_response_body(requested_region, home_region)
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("latin-1")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


# ============================================================================
# 2. Data export region compliance dependency
# ============================================================================


async def require_region_compliance(
    actor: Annotated[Actor, Depends(get_actor)],
    requested_region: str = "",
) -> dict[str, Any]:
    """FastAPI Dependency: validate the workspace region_policy allows the requested region.

    Intended for data export / migration endpoints. ``requested_region`` is read
    from the query string; when empty it defaults to the deployment home region.
    Raises 403 when the policy denies the region.
    """
    policy = await _get_region_policy_cached(actor.workspace_id)
    region = requested_region or _home_region()
    provider_region = _home_region()
    if not policy:
        return {"policy": None, "requested_region": region, "allowed": True}
    if not region_allows(policy, region, provider_region):
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "region not allowed",
                "requested_region": region,
                "home_region": policy.get("home_region", _home_region()),
            },
        )
    return {"policy": policy, "requested_region": region, "allowed": True}


# ============================================================================
# 3. Region policy query endpoints
# ============================================================================


@router.get("/region-policy")
async def get_region_policy(actor: Annotated[Actor, Depends(get_actor)]):
    """Return the workspace region policy plus current request region and compliance status.

    Note: this route resolves to ``GET /api/v1/enterprise/compliance/region-policy``
    which also exists in compliance.py. When data_residency_router is registered
    BEFORE compliance_router in main.py, this richer variant takes precedence
    (FastAPI first-match). The PUT /region-policy in compliance.py is unaffected.
    """
    # Explicit query bypasses the middleware cache for a fresh read.
    invalidate_region_policy_cache(actor.workspace_id)
    policy = await _get_region_policy_cached(actor.workspace_id)
    requested_region = _home_region()
    provider_region = _home_region()
    if not policy:
        return {
            "policy": None,
            "status": "missing",
            "requested_region": requested_region,
            "provider_region": provider_region,
            "compliant": True,
        }
    compliant = region_allows(policy, requested_region, provider_region)
    return {
        "policy": policy,
        "status": policy.get("status", "active"),
        "requested_region": requested_region,
        "provider_region": provider_region,
        "compliant": compliant,
    }


class RegionCheckRequest(BaseModel):
    requested_region: str = Field(min_length=2, max_length=32)
    provider_region: str | None = Field(default=None, max_length=32)


@router.post("/region-policy/check")
async def check_region_compliance(
    body: RegionCheckRequest, actor: Annotated[Actor, Depends(get_actor)]
):
    """Check whether a specific region combination is compliant for the current workspace."""
    policy = await _get_region_policy_cached(actor.workspace_id)
    if not policy:
        return {
            "allowed": True,
            "reason": "no policy configured",
            "requested_region": body.requested_region,
            "provider_region": body.provider_region,
        }
    allowed = region_allows(policy, body.requested_region, body.provider_region)
    return {
        "allowed": allowed,
        "requested_region": body.requested_region,
        "provider_region": body.provider_region,
        "home_region": policy.get("home_region"),
        "residency_required": policy.get("residency_required"),
        "cross_border_mode": policy.get("cross_border_mode"),
    }


# ============================================================================
# 4. Cross-border transfer audit endpoint
# ============================================================================


class CrossBorderTransfer(BaseModel):
    destination_region: str = Field(min_length=2, max_length=32)
    data_type: str = Field(min_length=1, max_length=120)
    data_volume_mb: int = Field(default=0, ge=0)
    legal_basis: str = Field(default="unspecified", min_length=1, max_length=120)


@router.post("/cross-border-transfer", status_code=201)
async def log_cross_border_transfer(
    body: CrossBorderTransfer, actor: Annotated[Actor, Depends(get_actor)]
):
    """Record a cross-border data transfer event for compliance audit.

    Writes an entry to the workspace audit chain (``sec_audit_chain`` via
    ``append_audit_chain``) and returns the generated ``transfer_id``.
    """
    transfer_id = new_id("cbt")
    async with pool.connection() as conn:
        async with conn.transaction():
            await append_audit_chain(
                conn,
                event_id=new_id("aud"),
                org_id=actor.org_id,
                workspace_id=actor.workspace_id,
                actor_user_id=actor.user_id,
                action="compliance.cross_border_transfer",
                resource_type="cross_border_transfer",
                resource_id=transfer_id,
                details={
                    "destination_region": body.destination_region,
                    "data_type": body.data_type,
                    "data_volume_mb": body.data_volume_mb,
                    "legal_basis": body.legal_basis,
                },
            )
    return {"transfer_id": transfer_id, "status": "recorded"}


# ============================================================================
# P3: 海外区部署选项 + GDPR 数据本地化
# ============================================================================


class Region(str, Enum):
    """数据驻留区域枚举。每个 workspace 绑定一个区域，写入 workspace.region。"""

    CN = "CN"
    EU = "EU"
    US = "US"
    SG = "SG"


# 区域法规元数据：CN 区数据不得跨境；EU 适用 GDPR；US 适用 CCPA；SG 亚太数据驻留。
REGION_REGULATION: dict[str, dict[str, Any]] = {
    Region.CN.value: {"regulation": "PIPL", "cross_border_default": False, "localization_default": True, "description": "数据不得跨境（PIPL）"},
    Region.EU.value: {"regulation": "GDPR", "cross_border_default": True, "localization_default": True, "description": "适用 GDPR"},
    Region.US.value: {"regulation": "CCPA", "cross_border_default": True, "localization_default": False, "description": "适用 CCPA"},
    Region.SG.value: {"regulation": "PDPA", "cross_border_default": True, "localization_default": True, "description": "亚太数据驻留（PDPA）"},
}

VALID_REGIONS = frozenset({r.value for r in Region})

# 《410》§7 删除传播矩阵：(table_name, user_column)。删除 ag_session 级联 ag_attachment/ag_artifact。
ERASURE_PROPAGATION_TABLES: tuple[tuple[str, str], ...] = (
    ("ag_session", "user_id"),
    ("ag_memory", "user_id"),
    ("id_notification", "user_id"),
    ("ag_approval", "requester_id"),
    ("ops_product_event", "user_id"),
)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE id_workspace ADD COLUMN IF NOT EXISTS region TEXT",
    """
    CREATE TABLE IF NOT EXISTS data_residency_policy (
      workspace_id TEXT PRIMARY KEY REFERENCES id_workspace(id) ON DELETE CASCADE,
      region TEXT NOT NULL CHECK (region IN ('CN','EU','US','SG')),
      data_localization_enforced BOOLEAN NOT NULL DEFAULT TRUE,
      cross_region_allowed BOOLEAN NOT NULL DEFAULT FALSE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dsar_request (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
      request_type TEXT NOT NULL CHECK (request_type IN ('access','erasure','portability')),
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','rejected')),
      payload JSONB NOT NULL DEFAULT '{}'::jsonb,
      result_url TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      completed_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dsar_request_workspace_status ON dsar_request(workspace_id, status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dsar_request_user ON dsar_request(user_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS cross_region_access_audit (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      user_id TEXT REFERENCES id_user(id) ON DELETE SET NULL,
      region_from TEXT NOT NULL,
      region_to TEXT NOT NULL,
      resource_type TEXT NOT NULL,
      resource_id TEXT NOT NULL,
      audit_reason TEXT NOT NULL DEFAULT '',
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cross_region_access_audit_workspace_time ON cross_region_access_audit(workspace_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cross_region_access_audit_user ON cross_region_access_audit(user_id, created_at DESC)",
)


async def ensure_data_residency_schema(conn: Any) -> None:
    """Apply all data-residency schema statements (idempotent)."""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


async def get_workspace_region(conn: Any, workspace_id: str) -> str:
    """查询 workspace 绑定的数据驻留区域。未绑定时返回 settings.default_region。"""
    result = await conn.execute("SELECT region FROM id_workspace WHERE id=%s", (workspace_id,))
    row = await result.fetchone()
    if row and row.get("region"):
        return str(row["region"])
    return str(getattr(settings, "default_region", "CN") or "CN")


async def assert_data_localization(conn: Any, workspace_id: str, region_to: str) -> bool:
    """校验从 workspace 所在区域访问 region_to 是否符合数据本地化策略。"""
    home_region = await get_workspace_region(conn, workspace_id)
    if region_to == home_region:
        return True
    if home_region == Region.CN.value:
        return False
    result = await conn.execute(
        "SELECT cross_region_allowed, data_localization_enforced FROM data_residency_policy WHERE workspace_id=%s",
        (workspace_id,),
    )
    row = await result.fetchone()
    if not row:
        meta = REGION_REGULATION.get(home_region)
        if meta:
            return bool(meta.get("cross_border_default", False))
        return False
    if row.get("data_localization_enforced"):
        return False
    return bool(row.get("cross_region_allowed"))


async def trigger_erasure_propagation(conn: Any, workspace_id: str, user_id: str) -> dict[str, Any]:
    """触发用户数据删除传播（按《410》§7 删除传播矩阵执行），返回每表删除行数。"""
    counts: dict[str, int] = {}
    for table, user_col in ERASURE_PROPAGATION_TABLES:
        result = await conn.execute(
            f"DELETE FROM {table} WHERE workspace_id=%s AND {user_col}=%s",
            (workspace_id, user_id),
        )
        counts[table] = int(getattr(result, "rowcount", 0) or 0)
    return {"workspace_id": workspace_id, "user_id": user_id, "deleted_counts": counts, "total_tables": len(counts)}


# ============================================================================
# Pydantic 模型
# ============================================================================


class DataResidencyPolicyUpdate(BaseModel):
    """PATCH /region：仅可调整 cross_region_allowed（已绑定区域不可更改）。"""

    cross_region_allowed: bool = False


class DsarCreate(BaseModel):
    """POST /dsar：用户发起 DSAR 请求。"""

    request_type: Literal["access", "erasure", "portability"]
    payload: dict[str, Any] = Field(default_factory=dict)


class CrossRegionAuditCreate(BaseModel):
    """POST /cross-region-audit：记录跨区域访问（内部 API）。"""

    workspace_id: str = Field(min_length=1, max_length=128)
    user_id: str | None = None
    region_from: str = Field(min_length=2, max_length=32)
    region_to: str = Field(min_length=2, max_length=32)
    resource_type: str = Field(min_length=1, max_length=120)
    resource_id: str = Field(min_length=1, max_length=160)
    audit_reason: str = Field(default="", max_length=500)


# ============================================================================
# 新路由：/api/v1/compliance（P3 数据驻留 / DSAR / 跨境审计）
# ============================================================================


compliance_router = APIRouter(prefix="/api/v1/compliance", tags=["data-residency"])


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


def _require_owner(actor: Actor) -> None:
    if actor.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role required")


@compliance_router.get("/region")
async def get_workspace_data_residency(actor: Annotated[Actor, Depends(get_actor)]):
    """查询当前 workspace 的数据驻留策略。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM data_residency_policy WHERE workspace_id=%s",
            (actor.workspace_id,),
        )
        row = await result.fetchone()
        region = await get_workspace_region(conn, actor.workspace_id)
    if not row:
        meta = REGION_REGULATION.get(region, {})
        return {
            "workspace_id": actor.workspace_id,
            "region": region,
            "data_localization_enforced": meta.get("localization_default", True),
            "cross_region_allowed": meta.get("cross_border_default", False),
            "regulation": meta.get("regulation"),
            "status": "default",
        }
    return {**row, "status": "configured"}


@compliance_router.patch("/region")
async def update_workspace_data_residency(
    body: DataResidencyPolicyUpdate, actor: Annotated[Actor, Depends(get_actor)]
):
    """更新数据驻留策略（仅 owner，已绑定区域不可更改，仅可调整 cross_region_allowed）。"""
    _require_owner(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM data_residency_policy WHERE workspace_id=%s",
            (actor.workspace_id,),
        )
        existing = await result.fetchone()
        if existing:
            row = await _update_policy_cross_region(conn, actor.workspace_id, body.cross_region_allowed)
        else:
            region = await get_workspace_region(conn, actor.workspace_id)
            if region not in VALID_REGIONS:
                raise HTTPException(status_code=422, detail=f"invalid workspace region: {region}")
            meta = REGION_REGULATION.get(region, {})
            row = await _create_policy(
                conn, actor.workspace_id, region,
                bool(meta.get("localization_default", True)), body.cross_region_allowed,
            )
        await conn.commit()
    invalidate_region_policy_cache(actor.workspace_id)
    return row


async def _update_policy_cross_region(conn: Any, workspace_id: str, cross_region_allowed: bool) -> dict[str, Any]:
    result = await conn.execute(
        "UPDATE data_residency_policy SET cross_region_allowed=%s, updated_at=now() WHERE workspace_id=%s RETURNING *",
        (cross_region_allowed, workspace_id),
    )
    row = await result.fetchone()
    return dict(row) if row else {}


async def _create_policy(conn: Any, workspace_id: str, region: str, localization: bool, cross_region: bool) -> dict[str, Any]:
    result = await conn.execute(
        """
        INSERT INTO data_residency_policy(workspace_id, region, data_localization_enforced, cross_region_allowed)
        VALUES(%s, %s, %s, %s)
        ON CONFLICT(workspace_id) DO UPDATE SET cross_region_allowed=EXCLUDED.cross_region_allowed, updated_at=now()
        RETURNING *
        """,
        (workspace_id, region, localization, cross_region),
    )
    row = await result.fetchone()
    return dict(row) if row else {}


@compliance_router.post("/dsar", status_code=201)
async def create_dsar_request(body: DsarCreate, actor: Annotated[Actor, Depends(get_actor)]):
    """用户发起 DSAR 请求（access/erasure/portability）。"""
    request_id = new_id("dsar")
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO dsar_request(id, workspace_id, user_id, request_type, status, payload)
                VALUES(%s, %s, %s, %s, 'pending', %s::jsonb)
                """,
                (request_id, actor.workspace_id, actor.user_id, body.request_type, json_dumps(body.payload)),
            )
            await append_audit_chain(
                conn, event_id=new_id("aud"), org_id=actor.org_id, workspace_id=actor.workspace_id,
                actor_user_id=actor.user_id, action="compliance.dsar.created",
                resource_type="dsar_request", resource_id=request_id,
                details={"request_type": body.request_type},
            )
            result = await conn.execute("SELECT * FROM dsar_request WHERE id=%s", (request_id,))
            row = await result.fetchone()
    return row


@compliance_router.get("/dsar")
async def list_dsar_requests(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列出我的 DSAR 请求（admin 可看 workspace 全部）。"""
    is_admin = actor.role in {"owner", "admin"}
    async with pool.connection() as conn:
        if is_admin:
            result = await conn.execute(
                "SELECT * FROM dsar_request WHERE workspace_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (actor.workspace_id, limit, offset),
            )
            total_result = await conn.execute(
                "SELECT COUNT(*) AS total FROM dsar_request WHERE workspace_id=%s",
                (actor.workspace_id,),
            )
        else:
            result = await conn.execute(
                "SELECT * FROM dsar_request WHERE workspace_id=%s AND user_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (actor.workspace_id, actor.user_id, limit, offset),
            )
            total_result = await conn.execute(
                "SELECT COUNT(*) AS total FROM dsar_request WHERE workspace_id=%s AND user_id=%s",
                (actor.workspace_id, actor.user_id),
            )
        rows = await result.fetchall()
        total_row = await total_result.fetchone()
    total = int(total_row["total"]) if total_row else 0
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@compliance_router.get("/dsar/{request_id}")
async def get_dsar_request(request_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """DSAR 请求详情。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM dsar_request WHERE id=%s AND workspace_id=%s",
            (request_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="DSAR request not found")
    if actor.role not in {"owner", "admin"} and row.get("user_id") != actor.user_id:
        raise HTTPException(status_code=404, detail="DSAR request not found")
    return row


@compliance_router.post("/dsar/{request_id}/process")
async def process_dsar_request(request_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    """处理 DSAR 请求（admin only）：access→清单 / erasure→删除传播 / portability→JSON 包。"""
    _require_admin(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM dsar_request WHERE id=%s AND workspace_id=%s FOR UPDATE",
                (request_id, actor.workspace_id),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="DSAR request not found")
            if row["status"] not in {"pending", "processing"}:
                raise HTTPException(status_code=409, detail=f"DSAR request already {row['status']}")
            await conn.execute("UPDATE dsar_request SET status='processing' WHERE id=%s", (request_id,))
            request_type = row["request_type"]
            target_user_id = row["user_id"]
            if request_type == "erasure":
                summary: dict[str, Any] = {"propagation": await trigger_erasure_propagation(conn, actor.workspace_id, target_user_id)}
                result_url = None
            elif request_type == "access":
                summary = {"inventory": await _build_user_data_inventory(conn, actor.workspace_id, target_user_id)}
                result_url = f"workama://dsar/{request_id}/access-manifest"
            elif request_type == "portability":
                result_url = f"workama://dsar/{request_id}/export.json"
                summary = {"format": "json", "export_url": result_url}
            else:  # pragma: no cover
                raise HTTPException(status_code=422, detail="unsupported request_type")
            await conn.execute(
                "UPDATE dsar_request SET status='completed', result_url=%s, completed_at=now() WHERE id=%s",
                (result_url, request_id),
            )
            await append_audit_chain(
                conn, event_id=new_id("aud"), org_id=actor.org_id, workspace_id=actor.workspace_id,
                actor_user_id=actor.user_id, action=f"compliance.dsar.processed.{request_type}",
                resource_type="dsar_request", resource_id=request_id, details=summary,
            )
            final = await conn.execute("SELECT * FROM dsar_request WHERE id=%s", (request_id,))
            final_row = await final.fetchone()
    return {"request": final_row, "summary": summary}


async def _build_user_data_inventory(conn: Any, workspace_id: str, user_id: str) -> dict[str, int]:
    """统计用户在各业务表的数据量（用于 DSAR access 清单）。"""
    inventory: dict[str, int] = {}
    for table, user_col in ERASURE_PROPAGATION_TABLES:
        result = await conn.execute(
            f"SELECT COUNT(*) AS cnt FROM {table} WHERE workspace_id=%s AND {user_col}=%s",
            (workspace_id, user_id),
        )
        row = await result.fetchone()
        inventory[table] = int(row["cnt"]) if row else 0
    return inventory


@compliance_router.get("/cross-region-audit")
async def list_cross_region_audit(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """跨区域访问审计列表（admin only，分页）。"""
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM cross_region_access_audit WHERE workspace_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (actor.workspace_id, limit, offset),
        )
        rows = await result.fetchall()
        total_result = await conn.execute(
            "SELECT COUNT(*) AS total FROM cross_region_access_audit WHERE workspace_id=%s",
            (actor.workspace_id,),
        )
        total_row = await total_result.fetchone()
    total = int(total_row["total"]) if total_row else 0
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


@compliance_router.post("/cross-region-audit", status_code=201)
async def record_cross_region_audit(
    body: CrossRegionAuditCreate,
    _: Annotated[None, Depends(require_internal)],
):
    """记录跨区域访问（内部 API，X-Internal-Token 鉴权，供其他模块调用）。"""
    audit_id = new_id("cra")
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO cross_region_access_audit(id, workspace_id, user_id, region_from, region_to, resource_type, resource_id, audit_reason)
            VALUES(%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (audit_id, body.workspace_id, body.user_id, body.region_from, body.region_to,
             body.resource_type, body.resource_id, body.audit_reason),
        )
        await conn.commit()
    return {"audit_id": audit_id, "status": "recorded"}
