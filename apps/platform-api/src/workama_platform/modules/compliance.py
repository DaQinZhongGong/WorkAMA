from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from workama_platform.core import Actor, get_actor, hash_secret, json_dumps, new_id, pool
from workama_platform.modules.audit_exports import append_audit_chain


router = APIRouter(prefix="/api/v1/enterprise/compliance", tags=["enterprise-compliance"])

REGION_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,48}:(?:[a-z][a-z0-9_.-]{0,80}|\*)$")
LICENSE_PREFIX = "wama-lic-"


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS bill_license (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      plan_code TEXT NOT NULL,
      license_key_hash TEXT NOT NULL UNIQUE,
      license_key_last_four TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','suspended','expired','revoked')),
      seats INTEGER NOT NULL CHECK (seats > 0),
      credit_limit BIGINT,
      concurrency_limit INTEGER CHECK (concurrency_limit IS NULL OR concurrency_limit > 0),
      features JSONB NOT NULL DEFAULT '{}'::jsonb,
      issued_by TEXT NOT NULL REFERENCES id_user(id),
      idempotency_key TEXT,
      valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
      valid_until TIMESTAMPTZ,
      revoked_at TIMESTAMPTZ,
      revoke_reason TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(org_id, workspace_id, idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bill_license_workspace_status ON bill_license(workspace_id, status, valid_until)",
    """
    CREATE TABLE IF NOT EXISTS bill_sla_policy (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL UNIQUE REFERENCES id_workspace(id) ON DELETE CASCADE,
      service_tier TEXT NOT NULL,
      availability_target NUMERIC(6,3) NOT NULL CHECK (availability_target > 0 AND availability_target <= 100),
      response_target_seconds INTEGER NOT NULL CHECK (response_target_seconds > 0),
      support_window TEXT NOT NULL,
      credits_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','draft','retired')),
      effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
      effective_until TIMESTAMPTZ,
      updated_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_region_policy (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL UNIQUE REFERENCES id_workspace(id) ON DELETE CASCADE,
      home_region TEXT NOT NULL,
      allowed_regions TEXT[] NOT NULL,
      provider_regions TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      cross_border_mode TEXT NOT NULL DEFAULT 'deny' CHECK (cross_border_mode IN ('deny','allowlist')),
      residency_required BOOLEAN NOT NULL DEFAULT TRUE,
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
      updated_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_jit_grant (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      subject_user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
      approved_by TEXT NOT NULL REFERENCES id_user(id),
      capabilities TEXT[] NOT NULL,
      resource_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
      reason TEXT NOT NULL,
      grant_hash TEXT NOT NULL UNIQUE,
      auth_strength SMALLINT NOT NULL CHECK (auth_strength BETWEEN 1 AND 4),
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','revoked')),
      starts_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      expires_at TIMESTAMPTZ NOT NULL,
      revoked_at TIMESTAMPTZ,
      revoke_reason TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sec_jit_grant_subject ON sec_jit_grant(workspace_id, subject_user_id, status, expires_at)",
    """
    CREATE TABLE IF NOT EXISTS sec_subprocessor (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      category TEXT NOT NULL,
      regions TEXT[] NOT NULL,
      data_classes TEXT[] NOT NULL,
      dpa_status TEXT NOT NULL DEFAULT 'pending' CHECK (dpa_status IN ('pending','reviewed','signed','expired')),
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','retired')),
      privacy_url TEXT,
      trust_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
      reviewed_at TIMESTAMPTZ,
      reviewed_by TEXT REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sec_privacy_event (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      event_type TEXT NOT NULL,
      severity TEXT NOT NULL CHECK (severity IN ('low','medium','high','critical')),
      status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','investigating','contained','closed')),
      summary TEXT NOT NULL,
      evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
      reported_by TEXT NOT NULL REFERENCES id_user(id),
      resolved_by TEXT REFERENCES id_user(id),
      resolved_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sec_privacy_event_workspace_status ON sec_privacy_event(workspace_id, status, created_at DESC)",
)


async def ensure_compliance_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


class LicenseCreate(BaseModel):
    plan_code: str = Field(min_length=1, max_length=64)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=128)
    seats: int = Field(default=1, ge=1, le=1_000_000)
    credit_limit: int | None = Field(default=None, ge=0)
    concurrency_limit: int | None = Field(default=None, ge=1, le=100_000)
    features: dict[str, Any] = Field(default_factory=dict)
    valid_until: datetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class RevokeRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class SlaPolicyUpsert(BaseModel):
    service_tier: str = Field(min_length=1, max_length=64)
    availability_target: float = Field(gt=0, le=100)
    response_target_seconds: int = Field(gt=0, le=7_776_000)
    support_window: str = Field(min_length=1, max_length=200)
    credits_policy: dict[str, Any] = Field(default_factory=dict)
    status: Literal["active", "draft", "retired"] = "active"
    effective_from: datetime | None = None
    effective_until: datetime | None = None


class RegionPolicyUpsert(BaseModel):
    home_region: str = Field(min_length=2, max_length=32)
    allowed_regions: list[str] = Field(min_length=1, max_length=32)
    provider_regions: list[str] = Field(default_factory=list, max_length=64)
    cross_border_mode: Literal["deny", "allowlist"] = "deny"
    residency_required: bool = True

    @field_validator("home_region")
    @classmethod
    def validate_home_region(cls, value: str) -> str:
        if not REGION_RE.fullmatch(value):
            raise ValueError("home_region must be a lowercase region identifier")
        return value

    @field_validator("allowed_regions", "provider_regions")
    @classmethod
    def validate_regions(cls, values: list[str]) -> list[str]:
        normalized = sorted(set(values))
        if any(not REGION_RE.fullmatch(value) for value in normalized):
            raise ValueError("regions must be lowercase region identifiers")
        return normalized


class LegalHoldCreate(BaseModel):
    resource_type: Literal["workspace", "notification", "artifact", "attachment", "session", "export", "all"]
    resource_id: str | None = Field(default=None, max_length=160)
    basis: str = Field(min_length=3, max_length=1000)
    expires_at: datetime | None = None


class JitGrantCreate(BaseModel):
    subject_user_id: str | None = Field(default=None, min_length=1, max_length=128)
    capabilities: list[str] = Field(min_length=1, max_length=32)
    resource_scope: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=3, max_length=1000)
    expires_in_seconds: int = Field(default=3600, ge=60, le=86_400)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, values: list[str]) -> list[str]:
        normalized = sorted(set(values))
        if any(not CAPABILITY_RE.fullmatch(value) for value in normalized):
            raise ValueError("invalid capability")
        return normalized


class SubprocessorUpsert(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    category: str = Field(min_length=2, max_length=120)
    regions: list[str] = Field(min_length=1, max_length=32)
    data_classes: list[str] = Field(min_length=1, max_length=32)
    dpa_status: Literal["pending", "reviewed", "signed", "expired"] = "pending"
    status: Literal["active", "paused", "retired"] = "active"
    privacy_url: str | None = Field(default=None, max_length=500)
    trust_evidence: dict[str, Any] = Field(default_factory=dict)


class PrivacyEventCreate(BaseModel):
    event_type: str = Field(min_length=2, max_length=120)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    summary: str = Field(min_length=3, max_length=2000)
    evidence: dict[str, Any] = Field(default_factory=dict)


def _admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


def _high_assurance(actor: Actor) -> None:
    if actor.auth_strength < 2:
        raise HTTPException(status_code=403, detail="Step-up authentication required")


def _workspace(actor: Actor, workspace_id: str | None) -> str:
    if workspace_id and workspace_id != actor.workspace_id:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return actor.workspace_id


def _now() -> datetime:
    return datetime.now(UTC)


def _license_view(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result.pop("license_key_hash", None)
    return result


def generate_license_key() -> str:
    return LICENSE_PREFIX + secrets.token_urlsafe(32)


def license_state(row: dict[str, Any], now: datetime | None = None) -> str:
    current = now or _now()
    if row.get("status") != "active":
        return str(row.get("status"))
    valid_from = row.get("valid_from")
    valid_until = row.get("valid_until")
    if valid_from and valid_from > current:
        return "pending"
    if valid_until and valid_until <= current:
        return "expired"
    return "active"


def region_allows(policy: dict[str, Any], requested_region: str, provider_region: str | None = None) -> bool:
    allowed = set(policy.get("allowed_regions") or [])
    providers = set(policy.get("provider_regions") or [])
    if requested_region not in allowed:
        return False
    if policy.get("residency_required") and requested_region != policy.get("home_region"):
        return False
    if provider_region and provider_region != requested_region:
        if policy.get("cross_border_mode") == "deny" or provider_region not in providers:
            return False
    return True


def jit_grant_allows(grant: dict[str, Any], capability: str, resource_id: str | None = None, now: datetime | None = None) -> bool:
    current = now or _now()
    if grant.get("status") != "active" or grant.get("starts_at", current) > current or grant.get("expires_at", current) <= current:
        return False
    if capability not in set(grant.get("capabilities") or []):
        return False
    resources = grant.get("resource_scope") or {}
    allowed_ids = resources.get("resource_ids")
    return not allowed_ids or resource_id in allowed_ids


@router.post("/licenses", status_code=201)
async def create_license(body: LicenseCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    workspace_id = _workspace(actor, body.workspace_id)
    raw_key = generate_license_key()
    async with pool.connection() as conn:
        async with conn.transaction():
            if body.idempotency_key:
                existing = await conn.execute("SELECT * FROM bill_license WHERE org_id=%s AND workspace_id=%s AND idempotency_key=%s", (actor.org_id, workspace_id, body.idempotency_key))
                row = await existing.fetchone()
                if row:
                    return {**_license_view(row), "replayed": True}
            license_id = new_id("lic")
            await conn.execute(
                """
                INSERT INTO bill_license(
                  id,org_id,workspace_id,plan_code,license_key_hash,license_key_last_four,
                  seats,credit_limit,concurrency_limit,features,issued_by,idempotency_key,valid_until
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                """,
                (license_id, actor.org_id, workspace_id, body.plan_code, hash_secret(raw_key), raw_key[-4:], body.seats, body.credit_limit, body.concurrency_limit, json_dumps(body.features), actor.user_id, body.idempotency_key, body.valid_until),
            )
            await append_audit_chain(conn, event_id=new_id("aud"), org_id=actor.org_id, workspace_id=workspace_id, actor_user_id=actor.user_id, action="enterprise.license.created", resource_type="license", resource_id=license_id, details={"plan_code": body.plan_code, "last_four": raw_key[-4:]})
            result = await conn.execute("SELECT * FROM bill_license WHERE id=%s", (license_id,))
            row = await result.fetchone()
    return {**_license_view(row), "license_key": raw_key, "replayed": False}


@router.get("/licenses")
async def list_licenses(actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM bill_license WHERE org_id=%s AND workspace_id=%s ORDER BY created_at DESC", (actor.org_id, actor.workspace_id))
        return {"items": [_license_view(row) for row in await result.fetchall()]}


@router.post("/licenses/{license_id}/revoke")
async def revoke_license(license_id: str, body: RevokeRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    _high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute("UPDATE bill_license SET status='revoked',revoked_at=now(),revoke_reason=%s,updated_at=now() WHERE id=%s AND org_id=%s AND workspace_id=%s AND status <> 'revoked' RETURNING *", (body.reason, license_id, actor.org_id, actor.workspace_id))
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="License not found or already revoked")
            await append_audit_chain(conn, event_id=new_id("aud"), org_id=actor.org_id, workspace_id=actor.workspace_id, actor_user_id=actor.user_id, action="enterprise.license.revoked", resource_type="license", resource_id=license_id, details={"reason": body.reason})
    return _license_view(row)


@router.get("/entitlements")
async def get_entitlements(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        license_result = await conn.execute("SELECT * FROM bill_license WHERE org_id=%s AND workspace_id=%s ORDER BY created_at DESC", (actor.org_id, actor.workspace_id))
        licenses = await license_result.fetchall()
        sla_result = await conn.execute("SELECT * FROM bill_sla_policy WHERE org_id=%s AND workspace_id=%s", (actor.org_id, actor.workspace_id))
        sla = await sla_result.fetchone()
        region_result = await conn.execute("SELECT * FROM sec_region_policy WHERE org_id=%s AND workspace_id=%s", (actor.org_id, actor.workspace_id))
        region = await region_result.fetchone()
    current = next((item for item in licenses if license_state(item) == "active"), None)
    return {"license": _license_view(current) if current else None, "license_state": license_state(current) if current else "missing", "sla": sla, "region_policy": region, "external_provider_exchange": "pending_external"}


@router.get("/sla")
async def get_sla(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM bill_sla_policy WHERE org_id=%s AND workspace_id=%s", (actor.org_id, actor.workspace_id))
        row = await result.fetchone()
    return row or {"status": "missing"}


@router.put("/sla")
async def put_sla(body: SlaPolicyUpsert, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("""
          INSERT INTO bill_sla_policy(id,org_id,workspace_id,service_tier,availability_target,response_target_seconds,support_window,credits_policy,status,effective_from,effective_until,updated_by)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,COALESCE(%s,now()),%s,%s)
          ON CONFLICT(workspace_id) DO UPDATE SET service_tier=EXCLUDED.service_tier,availability_target=EXCLUDED.availability_target,response_target_seconds=EXCLUDED.response_target_seconds,support_window=EXCLUDED.support_window,credits_policy=EXCLUDED.credits_policy,status=EXCLUDED.status,effective_from=EXCLUDED.effective_from,effective_until=EXCLUDED.effective_until,updated_by=EXCLUDED.updated_by,updated_at=now()
          RETURNING *
        """, (new_id("sla"), actor.org_id, actor.workspace_id, body.service_tier, body.availability_target, body.response_target_seconds, body.support_window, json_dumps(body.credits_policy), body.status, body.effective_from, body.effective_until, actor.user_id))
        row = await result.fetchone()
        await conn.commit()
    return row


# NOTE(v7.178): `GET /api/v1/enterprise/compliance/region-policy` 的唯一实现位于
# `modules/data_residency.py::get_region_policy`，其返回体包含 region policy + 当前请求
# region + 合规状态。此处原有的精简版 GET 与之路径完全重复，且因 compliance_router 在
# main.py 中先于 data_residency_router 注册（FastAPI first-match），实际会遮蔽掉更完整的
# 实现，导致线上恒返回 {"status":"missing"}。已删除该重复 handler，PUT 保持不变。


@router.put("/region-policy")
async def put_region_policy(body: RegionPolicyUpsert, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    if body.home_region not in set(body.allowed_regions):
        raise HTTPException(status_code=422, detail="home_region must be in allowed_regions")
    async with pool.connection() as conn:
        result = await conn.execute("""
          INSERT INTO sec_region_policy(id,org_id,workspace_id,home_region,allowed_regions,provider_regions,cross_border_mode,residency_required,updated_by)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT(workspace_id) DO UPDATE SET home_region=EXCLUDED.home_region,allowed_regions=EXCLUDED.allowed_regions,provider_regions=EXCLUDED.provider_regions,cross_border_mode=EXCLUDED.cross_border_mode,residency_required=EXCLUDED.residency_required,version=sec_region_policy.version+1,updated_by=EXCLUDED.updated_by,updated_at=now()
          RETURNING *
        """, (new_id("reg"), actor.org_id, actor.workspace_id, body.home_region, body.allowed_regions, body.provider_regions, body.cross_border_mode, body.residency_required, actor.user_id))
        row = await result.fetchone()
        await conn.commit()
    return row


@router.get("/legal-holds")
async def list_legal_holds(actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,workspace_id,resource_type,resource_id,basis,status,approved_by,starts_at,expires_at,released_at FROM sec_legal_hold WHERE workspace_id=%s ORDER BY starts_at DESC", (actor.workspace_id,))
        return {"items": await result.fetchall()}


@router.post("/legal-holds", status_code=201)
async def create_legal_hold(body: LegalHoldCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    _high_assurance(actor)
    if body.resource_type == "all" and body.resource_id:
        raise HTTPException(status_code=422, detail="A global hold cannot target a resource id")
    hold_id = new_id("hold")
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("INSERT INTO sec_legal_hold(id,workspace_id,resource_type,resource_id,basis,approved_by,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s)", (hold_id, actor.workspace_id, body.resource_type, body.resource_id, body.basis, actor.user_id, body.expires_at))
            await append_audit_chain(conn, event_id=new_id("aud"), org_id=actor.org_id, workspace_id=actor.workspace_id, actor_user_id=actor.user_id, action="privacy.legal_hold.created", resource_type="legal_hold", resource_id=hold_id, details={"resource_type": body.resource_type, "resource_id": body.resource_id})
            result = await conn.execute("SELECT id,workspace_id,resource_type,resource_id,basis,status,approved_by,starts_at,expires_at,released_at FROM sec_legal_hold WHERE id=%s", (hold_id,))
            row = await result.fetchone()
    return row


@router.post("/legal-holds/{hold_id}/release")
async def release_legal_hold(hold_id: str, body: RevokeRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    _high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute("UPDATE sec_legal_hold SET status='released',released_at=now() WHERE id=%s AND workspace_id=%s AND status='active' RETURNING id,workspace_id,resource_type,resource_id,basis,status,approved_by,starts_at,expires_at,released_at", (hold_id, actor.workspace_id))
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Active legal hold not found")
            await append_audit_chain(conn, event_id=new_id("aud"), org_id=actor.org_id, workspace_id=actor.workspace_id, actor_user_id=actor.user_id, action="privacy.legal_hold.released", resource_type="legal_hold", resource_id=hold_id, details={"reason": body.reason})
    return row


@router.get("/jit-grants")
async def list_jit_grants(actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,workspace_id,subject_user_id,approved_by,capabilities,resource_scope,reason,auth_strength,status,starts_at,expires_at,revoked_at,revoke_reason,created_at FROM sec_jit_grant WHERE workspace_id=%s ORDER BY created_at DESC", (actor.workspace_id,))
        return {"items": await result.fetchall()}


@router.post("/jit-grants", status_code=201)
async def create_jit_grant(body: JitGrantCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    _high_assurance(actor)
    subject = body.subject_user_id or actor.user_id
    expires_at = _now() + timedelta(seconds=body.expires_in_seconds)
    grant_id = new_id("jit")
    grant_hash = hashlib.sha256(f"{grant_id}:{actor.org_id}:{subject}:{expires_at.isoformat()}:{secrets.token_hex(16)}".encode()).hexdigest()
    async with pool.connection() as conn:
        async with conn.transaction():
            member = await conn.execute("SELECT 1 FROM id_member WHERE user_id=%s AND workspace_id=%s AND org_id=%s AND role IS NOT NULL", (subject, actor.workspace_id, actor.org_id))
            if not await member.fetchone():
                raise HTTPException(status_code=404, detail="Subject is not a workspace member")
            await conn.execute("INSERT INTO sec_jit_grant(id,org_id,workspace_id,subject_user_id,approved_by,capabilities,resource_scope,reason,grant_hash,auth_strength,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)", (grant_id, actor.org_id, actor.workspace_id, subject, actor.user_id, body.capabilities, json_dumps(body.resource_scope), body.reason, grant_hash, actor.auth_strength, expires_at))
            await append_audit_chain(conn, event_id=new_id("aud"), org_id=actor.org_id, workspace_id=actor.workspace_id, actor_user_id=actor.user_id, action="security.jit_grant.created", resource_type="jit_grant", resource_id=grant_id, details={"subject_user_id": subject, "capabilities": body.capabilities, "expires_at": expires_at.isoformat()})
            result = await conn.execute("SELECT id,workspace_id,subject_user_id,approved_by,capabilities,resource_scope,reason,auth_strength,status,starts_at,expires_at,created_at FROM sec_jit_grant WHERE id=%s", (grant_id,))
            row = await result.fetchone()
    return row


@router.post("/jit-grants/{grant_id}/revoke")
async def revoke_jit_grant(grant_id: str, body: RevokeRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    _high_assurance(actor)
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE sec_jit_grant SET status='revoked',revoked_at=now(),revoke_reason=%s WHERE id=%s AND workspace_id=%s AND status='active' RETURNING id,status,revoked_at,revoke_reason", (body.reason, grant_id, actor.workspace_id))
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Active JIT grant not found")
        await conn.commit()
    return row


@router.get("/subprocessors")
async def list_subprocessors(actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,workspace_id,name,category,regions,data_classes,dpa_status,status,privacy_url,trust_evidence,reviewed_at,reviewed_by,created_at,updated_at FROM sec_subprocessor WHERE workspace_id=%s ORDER BY name", (actor.workspace_id,))
        return {"items": await result.fetchall()}


@router.put("/subprocessors/{subprocessor_id}")
async def put_subprocessor(subprocessor_id: str, body: SubprocessorUpsert, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("""
          INSERT INTO sec_subprocessor(id,org_id,workspace_id,name,category,regions,data_classes,dpa_status,status,privacy_url,trust_evidence,reviewed_at,reviewed_by)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,CASE WHEN %s IN ('reviewed','signed') THEN now() END,CASE WHEN %s IN ('reviewed','signed') THEN %s END)
          ON CONFLICT(workspace_id,name) DO UPDATE SET category=EXCLUDED.category,regions=EXCLUDED.regions,data_classes=EXCLUDED.data_classes,dpa_status=EXCLUDED.dpa_status,status=EXCLUDED.status,privacy_url=EXCLUDED.privacy_url,trust_evidence=EXCLUDED.trust_evidence,reviewed_at=CASE WHEN EXCLUDED.dpa_status IN ('reviewed','signed') THEN now() ELSE sec_subprocessor.reviewed_at END,reviewed_by=CASE WHEN EXCLUDED.dpa_status IN ('reviewed','signed') THEN EXCLUDED.reviewed_by ELSE sec_subprocessor.reviewed_by END,updated_at=now()
          RETURNING id,workspace_id,name,category,regions,data_classes,dpa_status,status,privacy_url,trust_evidence,reviewed_at,reviewed_by,created_at,updated_at
        """, (subprocessor_id, actor.org_id, actor.workspace_id, body.name, body.category, body.regions, body.data_classes, body.dpa_status, body.status, body.privacy_url, json_dumps(body.trust_evidence), body.dpa_status, body.dpa_status, actor.user_id))
        row = await result.fetchone()
        await conn.commit()
    return row


@router.get("/privacy-events")
async def list_privacy_events(actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,event_type,severity,status,summary,evidence,reported_by,resolved_by,resolved_at,created_at,updated_at FROM sec_privacy_event WHERE workspace_id=%s ORDER BY created_at DESC", (actor.workspace_id,))
        return {"items": await result.fetchall()}


@router.post("/privacy-events", status_code=201)
async def create_privacy_event(body: PrivacyEventCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    event_id = new_id("pev")
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("INSERT INTO sec_privacy_event(id,org_id,workspace_id,event_type,severity,summary,evidence,reported_by) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s)", (event_id, actor.org_id, actor.workspace_id, body.event_type, body.severity, body.summary, json_dumps(body.evidence), actor.user_id))
            await append_audit_chain(conn, event_id=new_id("aud"), org_id=actor.org_id, workspace_id=actor.workspace_id, actor_user_id=actor.user_id, action="privacy.event.reported", resource_type="privacy_event", resource_id=event_id, details={"event_type": body.event_type, "severity": body.severity})
            result = await conn.execute("SELECT id,event_type,severity,status,summary,evidence,reported_by,created_at FROM sec_privacy_event WHERE id=%s", (event_id,))
            row = await result.fetchone()
    return row


@router.post("/privacy-events/{event_id}/close")
async def close_privacy_event(event_id: str, body: RevokeRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    _high_assurance(actor)
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE sec_privacy_event SET status='closed',resolved_by=%s,resolved_at=now(),updated_at=now(),evidence=evidence || %s::jsonb WHERE id=%s AND workspace_id=%s AND status <> 'closed' RETURNING id,status,resolved_by,resolved_at,evidence", (actor.user_id, json_dumps({"resolution_reason": body.reason}), event_id, actor.workspace_id))
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Open privacy event not found")
        await conn.commit()
    return row
