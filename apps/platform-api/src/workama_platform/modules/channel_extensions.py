from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from workama_platform.core import Actor, capability_allows, encrypt_secret, get_actor, hash_secret, json_dumps, new_id, pool
from workama_platform.modules.security.service import validate_outbound_url


router = APIRouter(prefix="/api/v1", tags=["channel-extensions"])
public_router = APIRouter(prefix="/api/v1/public", tags=["channel-extensions-public"])

CONTROLLED_PREFIX = "mock://"
IM_KINDS = frozenset({"wecom", "dingtalk", "feishu", "telegram"})
POOL_PROVIDERS = frozenset({"openai", "anthropic", "gemini", "custom"})


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS gw_subscription_account_pool (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      provider TEXT NOT NULL,
      sticky_ttl_seconds INTEGER NOT NULL DEFAULT 3600 CHECK (sticky_ttl_seconds BETWEEN 60 AND 604800),
      billing_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gw_subscription_account (
      id TEXT PRIMARY KEY,
      pool_id TEXT NOT NULL REFERENCES gw_subscription_account_pool(id) ON DELETE CASCADE,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      display_name TEXT NOT NULL,
      account_ref_enc TEXT NOT NULL,
      account_ref_hash TEXT NOT NULL,
      last_four TEXT NOT NULL,
      region TEXT NOT NULL DEFAULT 'global',
      weight INTEGER NOT NULL DEFAULT 100 CHECK (weight > 0),
      quota_remaining BIGINT,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','exhausted','revoked')),
      lease_owner_hash TEXT,
      lease_expires_at TIMESTAMPTZ,
      last_used_at TIMESTAMPTZ,
      error_count INTEGER NOT NULL DEFAULT 0 CHECK (error_count >= 0),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(pool_id, account_ref_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gw_subscription_account_lease ON gw_subscription_account(pool_id, status, lease_expires_at, last_used_at)",
    """
    CREATE TABLE IF NOT EXISTS gw_subscription_session (
      id TEXT PRIMARY KEY,
      pool_id TEXT NOT NULL REFERENCES gw_subscription_account_pool(id) ON DELETE CASCADE,
      account_id TEXT NOT NULL REFERENCES gw_subscription_account(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      session_key_hash TEXT NOT NULL,
      model TEXT,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','released')),
      expires_at TIMESTAMPTZ NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(pool_id, session_key_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS im_channel (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      kind TEXT NOT NULL CHECK (kind IN ('wecom','dingtalk','feishu','telegram')),
      name TEXT NOT NULL,
      endpoint TEXT NOT NULL,
      signing_secret_enc TEXT,
      signing_secret_hash TEXT,
      agent_id TEXT,
      status TEXT NOT NULL DEFAULT 'disabled' CHECK (status IN ('disabled','active','pending_external','revoked')),
      config JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_channel_workspace_status ON im_channel(workspace_id, status, kind)",
    """
    CREATE TABLE IF NOT EXISTS im_message (
      id TEXT PRIMARY KEY,
      channel_id TEXT NOT NULL REFERENCES im_channel(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      external_message_id TEXT NOT NULL,
      direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
      sender_ref_hash TEXT,
      payload_min JSONB NOT NULL DEFAULT '{}'::jsonb,
      status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted','delivered','pending_external','failed','replayed')),
      response_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(channel_id, external_message_id, direction)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_im_message_channel_time ON im_message(channel_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS miniapp_session (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
      provider TEXT NOT NULL DEFAULT 'wechat',
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS miniapp_message (
      id TEXT PRIMARY KEY,
      session_id TEXT NOT NULL REFERENCES miniapp_session(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      role TEXT NOT NULL CHECK (role IN ('user','assistant')),
      content TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'delivered' CHECK (status IN ('queued','delivered','pending_external','failed')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS miniapp_subscription (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
      topic TEXT NOT NULL,
      provider TEXT NOT NULL DEFAULT 'wechat',
      status TEXT NOT NULL DEFAULT 'pending_external' CHECK (status IN ('pending_external','subscribed','revoked')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, user_id, topic, provider)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ag_channel_binding (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      channel_type TEXT NOT NULL CHECK (channel_type IN ('slack','teams','wecom','dingtalk','feishu','telegram','custom')),
      external_subject TEXT NOT NULL,
      credential_ref TEXT,
      mapping JSONB NOT NULL DEFAULT '{}'::jsonb,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','revoked')),
      last_sync TIMESTAMPTZ,
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, channel_type, external_subject)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ag_channel_binding_workspace_status ON ag_channel_binding(workspace_id, status, channel_type)",
    """
    CREATE TABLE IF NOT EXISTS gw_subscription_account_usage (
      id TEXT PRIMARY KEY,
      pool_id TEXT NOT NULL REFERENCES gw_subscription_account_pool(id) ON DELETE CASCADE,
      account_id TEXT REFERENCES gw_subscription_account(id) ON DELETE SET NULL,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      session_key_hash TEXT,
      model TEXT,
      prompt_tokens BIGINT NOT NULL DEFAULT 0,
      completion_tokens BIGINT NOT NULL DEFAULT 0,
      cost_credits BIGINT NOT NULL DEFAULT 0,
      billing_period TEXT NOT NULL DEFAULT 'current',
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','billed','voided')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_account_usage_pool ON gw_subscription_account_usage(pool_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_account_usage_period ON gw_subscription_account_usage(pool_id, billing_period, status)",
    """
    CREATE TABLE IF NOT EXISTS gw_subscription_pool_billing_event (
      id TEXT PRIMARY KEY,
      pool_id TEXT NOT NULL REFERENCES gw_subscription_account_pool(id) ON DELETE CASCADE,
      account_id TEXT REFERENCES gw_subscription_account(id) ON DELETE SET NULL,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      event_type TEXT NOT NULL CHECK (event_type IN ('lease','renew','release','topup')),
      idempotency_key TEXT NOT NULL,
      amount BIGINT NOT NULL DEFAULT 0,
      balance_after BIGINT,
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','succeeded','failed')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(pool_id, idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pool_billing_event ON gw_subscription_pool_billing_event(pool_id, created_at DESC)",
)


async def ensure_channel_extensions_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


class AccountPoolCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    provider: str = Field(min_length=2, max_length=64)
    sticky_ttl_seconds: int = Field(default=3600, ge=60, le=604800)
    billing_policy: dict[str, Any] = Field(default_factory=dict)


class PoolAccountCreate(BaseModel):
    display_name: str = Field(min_length=2, max_length=120)
    account_ref: str = Field(min_length=8, max_length=4000)
    region: str = Field(default="global", min_length=2, max_length=32)
    weight: int = Field(default=100, ge=1, le=1000)
    quota_remaining: int | None = Field(default=None, ge=0)


class LeaseRequest(BaseModel):
    session_key: str = Field(min_length=8, max_length=400)
    model: str | None = Field(default=None, max_length=160)


class IMChannelCreate(BaseModel):
    kind: Literal["wecom", "dingtalk", "feishu", "telegram"]
    name: str = Field(min_length=2, max_length=120)
    endpoint: str = Field(min_length=8, max_length=1000)
    signing_secret: str | None = Field(default=None, min_length=8, max_length=4000)
    agent_id: str | None = Field(default=None, max_length=160)
    config: dict[str, Any] = Field(default_factory=dict)


class IMMessageCreate(BaseModel):
    external_message_id: str = Field(min_length=1, max_length=256)
    sender_ref: str | None = Field(default=None, max_length=400)
    content: str = Field(min_length=1, max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MiniappMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class MiniappSubscriptionCreate(BaseModel):
    topics: list[str] = Field(min_length=1, max_length=16)


CHANNEL_TYPES = ("slack", "teams", "wecom", "dingtalk", "feishu", "telegram", "custom")


class ChannelBindingCreate(BaseModel):
    channel_type: Literal["slack", "teams", "wecom", "dingtalk", "feishu", "telegram", "custom"]
    external_subject: str = Field(min_length=1, max_length=400)
    credential_ref: str | None = Field(default=None, max_length=400)
    mapping: dict[str, Any] = Field(default_factory=dict)


class ChannelBindingPatch(BaseModel):
    external_subject: str | None = Field(default=None, min_length=1, max_length=400)
    credential_ref: str | None = Field(default=None, max_length=400)
    mapping: dict[str, Any] | None = None
    status: Literal["active", "disabled", "revoked"] | None = None


def _admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


def _require_channel_binding(actor: Actor, action: Literal["read", "write", "create", "delete"]) -> None:
    required = f"channel_binding:{action}"
    if capability_allows(actor.capabilities, required) or capability_allows(actor.capabilities, "channel_binding:*"):
        return
    if actor.role in {"owner", "admin"}:
        return
    raise HTTPException(status_code=403, detail=f"Missing capability: {required}")


def _pool_write(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


def _pool_read(actor: Actor) -> None:
    if actor.role not in {"owner", "admin", "member"}:
        raise HTTPException(status_code=403, detail="Member role required")


def _controlled(endpoint: str) -> bool:
    return endpoint.startswith(CONTROLLED_PREFIX)


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sensitive = {"token", "secret", "password", "authorization", "access_token", "refresh_token", "api_key", "credential"}
    return {key: "[redacted]" if key.lower() in sensitive else value for key, value in payload.items()}


def _channel_view(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result.pop("signing_secret_enc", None)
    result.pop("signing_secret_hash", None)
    return result


def _account_view(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result.pop("account_ref_enc", None)
    result.pop("account_ref_hash", None)
    return result


def _session_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _deduct_lease_cost(conn, pool_id: str, account_id: str, workspace_id: str, session_key_hash: str, model: str | None) -> dict[str, Any]:
    """Deduct usage-based cost from account quota and record billing event.

    Returns {"succeeded": True, "cost_credits": N, "balance_after": M} or
    {"succeeded": False, "reason": "insufficient_quota"}.
    """
    # Read billing policy from pool
    pool_result = await conn.execute("SELECT billing_policy FROM gw_subscription_account_pool WHERE id=%s", (pool_id,))
    pool_row = await pool_result.fetchone()
    policy = pool_row.get("billing_policy") or {} if pool_row else {}
    cost_per_lease = int(policy.get("cost_per_lease", 0))
    if cost_per_lease <= 0:
        return {"succeeded": True, "cost_credits": 0, "balance_after": None}
    # Check account quota
    acct_result = await conn.execute("SELECT quota_remaining FROM gw_subscription_account WHERE id=%s", (account_id,))
    acct = await acct_result.fetchone()
    quota = acct.get("quota_remaining") if acct else None
    if quota is not None and quota < cost_per_lease:
        return {"succeeded": False, "reason": "insufficient_quota"}
    # Deduct
    if quota is not None:
        new_quota = quota - cost_per_lease
        await conn.execute("UPDATE gw_subscription_account SET quota_remaining=%s,updated_at=now() WHERE id=%s", (new_quota, account_id))
    else:
        new_quota = None
    # Record usage
    usage_id = new_id("usg")
    await conn.execute(
        """INSERT INTO gw_subscription_account_usage(id,pool_id,account_id,workspace_id,session_key_hash,model,cost_credits,billing_period,status)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (usage_id, pool_id, account_id, workspace_id, session_key_hash, model, cost_per_lease, "current", "active"),
    )
    # Record billing event (idempotent by usage_id)
    await conn.execute(
        """INSERT INTO gw_subscription_pool_billing_event(id,pool_id,account_id,workspace_id,event_type,idempotency_key,amount,balance_after,status)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(pool_id,idempotency_key) DO UPDATE SET status='succeeded',updated_at=now()""",
        (new_id("bev"), pool_id, account_id, workspace_id, "lease", usage_id, cost_per_lease, new_quota, "succeeded"),
    )
    return {"succeeded": True, "cost_credits": cost_per_lease, "balance_after": new_quota}


async def renew_expired_leases(worker_id: str, limit: int = 20) -> dict[str, int]:
    """Auto-renew leases that are about to expire if billing policy allows."""
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT s.id,s.pool_id,s.account_id,s.workspace_id,s.session_key_hash,s.model,s.expires_at,
                       p.sticky_ttl_seconds,p.billing_policy
                FROM gw_subscription_session s
                JOIN gw_subscription_account_pool p ON p.id=s.pool_id
                WHERE s.status='active'
                  AND s.expires_at <= now() + interval '5 minutes'
                ORDER BY s.expires_at
                LIMIT %s
                FOR UPDATE OF s SKIP LOCKED
                """,
                (limit,),
            )
            items = await result.fetchall()
            renewed = 0
            released = 0
            for item in items:
                policy = item.get("billing_policy") or {}
                auto_renew = bool(policy.get("auto_renew", False))
                if auto_renew:
                    deduct = await _deduct_lease_cost(
                        conn, item["pool_id"], item["account_id"], item["workspace_id"],
                        item["session_key_hash"], item["model"],
                    )
                    if deduct["succeeded"]:
                        new_expires = datetime.now(UTC) + timedelta(seconds=int(item["sticky_ttl_seconds"]))
                        await conn.execute(
                            "UPDATE gw_subscription_session SET expires_at=%s,updated_at=now() WHERE id=%s",
                            (new_expires, item["id"]),
                        )
                        await conn.execute(
                            "UPDATE gw_subscription_account SET lease_expires_at=%s,updated_at=now() WHERE id=%s",
                            (new_expires, item["account_id"]),
                        )
                        renewed += 1
                    else:
                        await conn.execute(
                            "UPDATE gw_subscription_session SET status='expired',updated_at=now() WHERE id=%s",
                            (item["id"],),
                        )
                        await conn.execute(
                            "UPDATE gw_subscription_account SET lease_owner_hash=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=%s",
                            (item["account_id"],),
                        )
                        released += 1
                else:
                    await conn.execute(
                        "UPDATE gw_subscription_session SET status='expired',updated_at=now() WHERE id=%s",
                        (item["id"],),
                    )
                    await conn.execute(
                        "UPDATE gw_subscription_account SET lease_owner_hash=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=%s",
                        (item["account_id"],),
                    )
                    released += 1
    return {"renewed": renewed, "released": released, "claimed": len(items)}


async def cleanup_expired_sessions(worker_id: str, limit: int = 50) -> dict[str, int]:
    """Release expired sessions and clear lease locks."""
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT s.id,s.account_id
                FROM gw_subscription_session s
                WHERE s.status='active' AND s.expires_at < now()
                ORDER BY s.expires_at
                LIMIT %s
                FOR UPDATE OF s SKIP LOCKED
                """,
                (limit,),
            )
            items = await result.fetchall()
            for item in items:
                await conn.execute(
                    "UPDATE gw_subscription_session SET status='expired',updated_at=now() WHERE id=%s",
                    (item["id"],),
                )
                await conn.execute(
                    "UPDATE gw_subscription_account SET lease_owner_hash=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=%s AND lease_owner_hash IS NOT NULL",
                    (item["account_id"],),
                )
    return {"cleaned": len(items)}


async def release_expired_leases(worker_id: str, limit: int = 50) -> dict[str, int]:
    """Release expired leases and clear account lease locks."""
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT s.id, s.account_id
                FROM gw_subscription_session s
                WHERE s.status = 'active' AND s.expires_at < now()
                ORDER BY s.expires_at
                LIMIT %s
                FOR UPDATE OF s SKIP LOCKED
                """,
                (limit,),
            )
            items = await result.fetchall()
            for item in items:
                await conn.execute(
                    "UPDATE gw_subscription_session SET status='expired', updated_at=now() WHERE id=%s",
                    (item["id"],),
                )
                await conn.execute(
                    """
                    UPDATE gw_subscription_account
                    SET status='active', lease_owner_hash=NULL, lease_expires_at=NULL, updated_at=now()
                    WHERE id=%s AND lease_owner_hash IS NOT NULL
                    """,
                    (item["account_id"],),
                )
    return {"released": len(items)}


async def sweep_exhausted_accounts(worker_id: str, limit: int = 50) -> dict[str, int]:
    """Scan accounts with error_count>=5 or quota_remaining==0 and mark as exhausted."""
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT id
                FROM gw_subscription_account
                WHERE status = 'active'
                  AND (error_count >= 5 OR quota_remaining = 0)
                ORDER BY last_used_at NULLS FIRST
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            items = await result.fetchall()
            for item in items:
                await conn.execute(
                    "UPDATE gw_subscription_account SET status='exhausted', updated_at=now() WHERE id=%s",
                    (item["id"],),
                )
    return {"swept": len(items), "scanned": len(items)}


async def auto_topup(worker_id: str, limit: int = 20) -> dict[str, int]:
    """Auto-topup exhausted accounts if billing_policy allows. Idempotent by idempotency_key."""
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT a.id, a.pool_id, a.workspace_id, a.quota_remaining, p.billing_policy
                FROM gw_subscription_account a
                JOIN gw_subscription_account_pool p ON p.id = a.pool_id
                WHERE a.status = 'exhausted'
                ORDER BY a.updated_at
                LIMIT %s
                FOR UPDATE OF a SKIP LOCKED
                """,
                (limit,),
            )
            items = await result.fetchall()
            topped_up = 0
            skipped = 0
            for item in items:
                policy = item.get("billing_policy") or {}
                if not bool(policy.get("auto_topup", False)):
                    skipped += 1
                    continue
                topup_amount = int(policy.get("topup_amount", 0))
                if topup_amount <= 0:
                    skipped += 1
                    continue
                idempotency_key = f"topup:{item['id']}:{datetime.now(UTC).strftime('%Y%m%d')}"
                existing = await conn.execute(
                    "SELECT 1 FROM gw_subscription_pool_billing_event WHERE pool_id=%s AND idempotency_key=%s AND status='succeeded'",
                    (item["pool_id"], idempotency_key),
                )
                if await existing.fetchone():
                    skipped += 1
                    continue
                new_quota = (item.get("quota_remaining") or 0) + topup_amount
                await conn.execute(
                    "UPDATE gw_subscription_account SET quota_remaining=%s, status='active', updated_at=now() WHERE id=%s",
                    (new_quota, item["id"]),
                )
                await conn.execute(
                    """INSERT INTO gw_subscription_pool_billing_event(id,pool_id,account_id,workspace_id,event_type,idempotency_key,amount,balance_after,status)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(pool_id,idempotency_key) DO UPDATE SET status='succeeded',updated_at=now()""",
                    (new_id("bev"), item["pool_id"], item["id"], item["workspace_id"], "topup", idempotency_key, topup_amount, new_quota, "succeeded"),
                )
                topped_up += 1
    return {"topped_up": topped_up, "skipped": skipped, "scanned": len(items)}


def choose_sticky_account(accounts: list[dict[str, Any]], session_key: str) -> dict[str, Any] | None:
    active = [account for account in accounts if account.get("status") == "active" and (account.get("quota_remaining") is None or account.get("quota_remaining", 0) > 0)]
    if not active:
        return None
    total = sum(max(1, int(account.get("weight", 100))) for account in active)
    cursor = int(hashlib.sha256(session_key.encode()).hexdigest()[:16], 16) % total
    for account in active:
        cursor -= max(1, int(account.get("weight", 100)))
        if cursor < 0:
            return account
    return active[-1]


def normalize_im_content(kind: str, content: str) -> str:
    prefix = f"[{kind}] "
    return content if content.startswith(prefix) else prefix + content


def miniapp_manifest() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "client": "react-miniapp-adapter",
        "capabilities": ["chat", "knowledge_read", "subscription_status"],
        "credential_storage": "memory_only",
        "provider_exchange": "pending_external",
        "webhook_verification": "server_side",
    }


@router.get("/gateway/account-pools")
async def list_account_pools(actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT p.*,count(a.id)::int AS account_count FROM gw_subscription_account_pool p LEFT JOIN gw_subscription_account a ON a.pool_id=p.id WHERE p.workspace_id=%s GROUP BY p.id ORDER BY p.created_at DESC", (actor.workspace_id,))
        # Contract《720》listAccountPools: ListQuery -> ListResponse<AccountPoolDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/gateway/account-pools", status_code=201)
async def create_account_pool(body: AccountPoolCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    if body.provider not in POOL_PROVIDERS:
        raise HTTPException(status_code=422, detail="Unsupported subscription pool provider")
    pool_id = new_id("pool")
    async with pool.connection() as conn:
        try:
            result = await conn.execute("INSERT INTO gw_subscription_account_pool(id,org_id,workspace_id,name,provider,sticky_ttl_seconds,billing_policy,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING *", (pool_id, actor.org_id, actor.workspace_id, body.name, body.provider, body.sticky_ttl_seconds, json_dumps(body.billing_policy), actor.user_id))
            row = await result.fetchone()
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            if "duplicate key" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Account pool name already exists") from exc
            raise
    return row


@router.get("/gateway/account-pools/{pool_id}/accounts")
async def list_pool_accounts(pool_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM gw_subscription_account WHERE pool_id=%s AND workspace_id=%s ORDER BY created_at DESC", (pool_id, actor.workspace_id))
        # Contract《720》listPoolAccounts: ListQuery -> ListResponse<PoolAccountDTO>
        # 保留 items 字段向后兼容
        data = [_account_view(row) for row in await result.fetchall()]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/gateway/account-pools/{pool_id}/accounts", status_code=201)
async def add_pool_account(pool_id: str, body: PoolAccountCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    account_id = new_id("acct")
    account_hash = hash_secret(body.account_ref)
    async with pool.connection() as conn:
        found = await conn.execute("SELECT 1 FROM gw_subscription_account_pool WHERE id=%s AND workspace_id=%s", (pool_id, actor.workspace_id))
        if not await found.fetchone():
            raise HTTPException(status_code=404, detail="Account pool not found")
        result = await conn.execute("INSERT INTO gw_subscription_account(id,pool_id,org_id,workspace_id,display_name,account_ref_enc,account_ref_hash,last_four,region,weight,quota_remaining) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *", (account_id, pool_id, actor.org_id, actor.workspace_id, body.display_name, encrypt_secret(body.account_ref), account_hash, body.account_ref[-4:], body.region, body.weight, body.quota_remaining))
        row = await result.fetchone()
        await conn.commit()
    return _account_view(row)


@router.post("/gateway/account-pools/{pool_id}/leases", status_code=201)
async def lease_pool_account(pool_id: str, body: LeaseRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    session_hash = _session_hash(body.session_key)
    async with pool.connection() as conn:
        pool_result = await conn.execute("SELECT * FROM gw_subscription_account_pool WHERE id=%s AND workspace_id=%s AND status='active'", (pool_id, actor.workspace_id))
        pool_row = await pool_result.fetchone()
        if not pool_row:
            raise HTTPException(status_code=404, detail="Active account pool not found")
        current_result = await conn.execute("SELECT s.*,a.display_name,a.last_four,a.region,a.status account_status FROM gw_subscription_session s JOIN gw_subscription_account a ON a.id=s.account_id WHERE s.pool_id=%s AND s.workspace_id=%s AND s.session_key_hash=%s AND s.status='active' AND s.expires_at>now()", (pool_id, actor.workspace_id, session_hash))
        current = await current_result.fetchone()
        if current:
            return {"lease_id": current["id"], "account_id": current["account_id"], "display_name": current["display_name"], "last_four": current["last_four"], "status": "replayed", "expires_at": current["expires_at"], "execution_mode": "controlled_mock" if pool_row["provider"] == "custom" else "pending_external"}
        account_result = await conn.execute("SELECT * FROM gw_subscription_account WHERE pool_id=%s AND workspace_id=%s AND (lease_expires_at IS NULL OR lease_expires_at<now()) ORDER BY last_used_at NULLS FIRST, weight DESC", (pool_id, actor.workspace_id))
        selected = choose_sticky_account(await account_result.fetchall(), body.session_key)
        if not selected:
            raise HTTPException(status_code=409, detail="No account is available in this pool")
        # Billing deduction (idempotent within transaction)
        deduct = await _deduct_lease_cost(conn, pool_id, selected["id"], actor.workspace_id, session_hash, body.model)
        if not deduct["succeeded"]:
            raise HTTPException(status_code=402, detail=f"Lease denied: {deduct.get('reason', 'billing failed')}")
        expires_at = datetime.now(UTC) + timedelta(seconds=int(pool_row["sticky_ttl_seconds"]))
        lease_id = new_id("lease")
        await conn.execute("INSERT INTO gw_subscription_session(id,pool_id,account_id,workspace_id,session_key_hash,model,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s)", (lease_id, pool_id, selected["id"], actor.workspace_id, session_hash, body.model, expires_at))
        await conn.execute("UPDATE gw_subscription_account SET lease_owner_hash=%s,lease_expires_at=%s,last_used_at=now(),updated_at=now() WHERE id=%s", (session_hash, expires_at, selected["id"]))
        await conn.commit()
    return {"lease_id": lease_id, "account_id": selected["id"], "display_name": selected["display_name"], "last_four": selected["last_four"], "status": "leased", "expires_at": expires_at, "execution_mode": "controlled_mock" if pool_row["provider"] == "custom" else "pending_external", "cost_credits": deduct.get("cost_credits", 0)}


@router.post("/gateway/account-pools/{pool_id}/leases/{session_key}/release")
async def release_pool_lease(pool_id: str, session_key: str, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    session_hash = _session_hash(session_key)
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE gw_subscription_session SET status='released',updated_at=now() WHERE pool_id=%s AND workspace_id=%s AND session_key_hash=%s AND status='active' RETURNING id,account_id", (pool_id, actor.workspace_id, session_hash))
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Active lease not found")
        await conn.execute("UPDATE gw_subscription_account SET lease_owner_hash=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=%s", (row["account_id"],))
        await conn.commit()
    return {"released": True, "lease_id": row["id"]}


@router.get("/channel-extensions/account-pools/{pool_id}/usage")
async def get_account_pool_usage(pool_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        pool_result = await conn.execute("SELECT 1 FROM gw_subscription_account_pool WHERE id=%s AND workspace_id=%s", (pool_id, actor.workspace_id))
        if not await pool_result.fetchone():
            raise HTTPException(status_code=404, detail="Account pool not found")
        usage_result = await conn.execute(
            """SELECT COALESCE(SUM(cost_credits),0)::bigint AS total_cost,
                      COALESCE(SUM(prompt_tokens),0)::bigint AS total_prompt_tokens,
                      COALESCE(SUM(completion_tokens),0)::bigint AS total_completion_tokens,
                      COUNT(*)::int AS total_sessions
               FROM gw_subscription_account_usage
               WHERE pool_id=%s AND workspace_id=%s AND billing_period='current' AND status='active'""",
            (pool_id, actor.workspace_id),
        )
        usage = await usage_result.fetchone()
        events_result = await conn.execute(
            """SELECT event_type,amount,status,created_at
               FROM gw_subscription_pool_billing_event
               WHERE pool_id=%s AND workspace_id=%s
               ORDER BY created_at DESC LIMIT 50""",
            (pool_id, actor.workspace_id),
        )
        events = await events_result.fetchall()
    return {
        "pool_id": pool_id,
        "total_cost_credits": usage["total_cost"],
        "total_prompt_tokens": usage["total_prompt_tokens"],
        "total_completion_tokens": usage["total_completion_tokens"],
        "total_sessions": usage["total_sessions"],
        "recent_events": events,
    }


# ============================================================================
# v7.165: 订阅账号池 REST 端点（channel-extensions/pools）
# ============================================================================


@router.post("/channel-extensions/pools", status_code=201)
async def create_channel_extension_pool(body: AccountPoolCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _pool_write(actor)
    if body.provider not in POOL_PROVIDERS:
        raise HTTPException(status_code=422, detail="Unsupported subscription pool provider")
    pool_id = new_id("pool")
    async with pool.connection() as conn:
        try:
            result = await conn.execute(
                "INSERT INTO gw_subscription_account_pool(id,org_id,workspace_id,name,provider,sticky_ttl_seconds,billing_policy,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING *",
                (pool_id, actor.org_id, actor.workspace_id, body.name, body.provider, body.sticky_ttl_seconds, json_dumps(body.billing_policy), actor.user_id),
            )
            row = await result.fetchone()
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            if "duplicate key" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Account pool name already exists") from exc
            raise
    return row


@router.get("/channel-extensions/pools")
async def list_channel_extension_pools(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    _pool_read(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT p.*, count(a.id)::int AS account_count FROM gw_subscription_account_pool p LEFT JOIN gw_subscription_account a ON a.pool_id=p.id WHERE p.workspace_id=%s GROUP BY p.id ORDER BY p.created_at DESC LIMIT %s OFFSET %s",
            (actor.workspace_id, limit, offset),
        )
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/channel-extensions/pools/{pool_id}/accounts", status_code=201)
async def add_pool_account_v2(pool_id: str, body: PoolAccountCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _pool_write(actor)
    account_id = new_id("acct")
    account_hash = hash_secret(body.account_ref)
    async with pool.connection() as conn:
        found = await conn.execute(
            "SELECT 1 FROM gw_subscription_account_pool WHERE id=%s AND workspace_id=%s",
            (pool_id, actor.workspace_id),
        )
        if not await found.fetchone():
            raise HTTPException(status_code=404, detail="Account pool not found")
        result = await conn.execute(
            """INSERT INTO gw_subscription_account(id,pool_id,org_id,workspace_id,display_name,account_ref_enc,account_ref_hash,last_four,region,weight,quota_remaining)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (account_id, pool_id, actor.org_id, actor.workspace_id, body.display_name, encrypt_secret(body.account_ref), account_hash, body.account_ref[-4:], body.region, body.weight, body.quota_remaining),
        )
        row = await result.fetchone()
        await conn.commit()
    return _account_view(row)


@router.get("/channel-extensions/pools/{pool_id}/accounts")
async def list_pool_accounts_v2(pool_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _pool_read(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM gw_subscription_account WHERE pool_id=%s AND workspace_id=%s ORDER BY created_at DESC",
            (pool_id, actor.workspace_id),
        )
        data = [_account_view(row) for row in await result.fetchall()]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/channel-extensions/pools/{pool_id}/lease", status_code=201)
async def lease_pool_account_v2(pool_id: str, body: LeaseRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _pool_write(actor)
    session_hash = _session_hash(body.session_key)
    async with pool.connection() as conn:
        pool_result = await conn.execute(
            "SELECT * FROM gw_subscription_account_pool WHERE id=%s AND workspace_id=%s AND status='active'",
            (pool_id, actor.workspace_id),
        )
        pool_row = await pool_result.fetchone()
        if not pool_row:
            raise HTTPException(status_code=404, detail="Active account pool not found")
        current_result = await conn.execute(
            """SELECT s.*, a.display_name, a.last_four, a.region, a.status AS account_status
               FROM gw_subscription_session s
               JOIN gw_subscription_account a ON a.id=s.account_id
               WHERE s.pool_id=%s AND s.workspace_id=%s AND s.session_key_hash=%s AND s.status='active' AND s.expires_at>now()""",
            (pool_id, actor.workspace_id, session_hash),
        )
        current = await current_result.fetchone()
        if current:
            return {
                "lease_id": current["id"],
                "account_id": current["account_id"],
                "display_name": current["display_name"],
                "last_four": current["last_four"],
                "status": "replayed",
                "expires_at": current["expires_at"],
                "execution_mode": "controlled_mock" if pool_row["provider"] == "custom" else "pending_external",
            }
        account_result = await conn.execute(
            "SELECT * FROM gw_subscription_account WHERE pool_id=%s AND workspace_id=%s AND status='active' AND (lease_expires_at IS NULL OR lease_expires_at<now()) ORDER BY last_used_at NULLS FIRST, weight DESC",
            (pool_id, actor.workspace_id),
        )
        selected = choose_sticky_account(await account_result.fetchall(), body.session_key)
        if not selected:
            raise HTTPException(status_code=409, detail="No account is available in this pool")
        deduct = await _deduct_lease_cost(conn, pool_id, selected["id"], actor.workspace_id, session_hash, body.model)
        if not deduct["succeeded"]:
            raise HTTPException(status_code=402, detail=f"Lease denied: {deduct.get('reason', 'billing failed')}")
        expires_at = datetime.now(UTC) + timedelta(seconds=int(pool_row["sticky_ttl_seconds"]))
        lease_id = new_id("lease")
        await conn.execute(
            "INSERT INTO gw_subscription_session(id,pool_id,account_id,workspace_id,session_key_hash,model,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (lease_id, pool_id, selected["id"], actor.workspace_id, session_hash, body.model, expires_at),
        )
        await conn.execute(
            "UPDATE gw_subscription_account SET lease_owner_hash=%s, lease_expires_at=%s, last_used_at=now(), updated_at=now() WHERE id=%s",
            (session_hash, expires_at, selected["id"]),
        )
        await conn.commit()
    return {
        "lease_id": lease_id,
        "account_id": selected["id"],
        "display_name": selected["display_name"],
        "last_four": selected["last_four"],
        "status": "leased",
        "expires_at": expires_at,
        "execution_mode": "controlled_mock" if pool_row["provider"] == "custom" else "pending_external",
        "cost_credits": deduct.get("cost_credits", 0),
    }


@router.post("/channel-extensions/pools/{pool_id}/release")
async def release_pool_lease_v2(pool_id: str, body: LeaseRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _pool_write(actor)
    session_hash = _session_hash(body.session_key)
    async with pool.connection() as conn:
        result = await conn.execute(
            "UPDATE gw_subscription_session SET status='released', updated_at=now() WHERE pool_id=%s AND workspace_id=%s AND session_key_hash=%s AND status='active' RETURNING id, account_id",
            (pool_id, actor.workspace_id, session_hash),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Active lease not found")
        await conn.execute(
            "UPDATE gw_subscription_account SET lease_owner_hash=NULL, lease_expires_at=NULL, updated_at=now() WHERE id=%s",
            (row["account_id"],),
        )
        await conn.commit()
    return {"released": True, "lease_id": row["id"]}


@router.get("/channel-extensions/pools/{pool_id}/usage")
async def get_pool_usage_v2(pool_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _pool_read(actor)
    async with pool.connection() as conn:
        pool_result = await conn.execute(
            "SELECT 1 FROM gw_subscription_account_pool WHERE id=%s AND workspace_id=%s",
            (pool_id, actor.workspace_id),
        )
        if not await pool_result.fetchone():
            raise HTTPException(status_code=404, detail="Account pool not found")
        usage_result = await conn.execute(
            """SELECT COALESCE(SUM(cost_credits),0)::bigint AS total_cost,
                      COALESCE(SUM(prompt_tokens),0)::bigint AS total_prompt_tokens,
                      COALESCE(SUM(completion_tokens),0)::bigint AS total_completion_tokens,
                      COUNT(*)::int AS total_sessions
               FROM gw_subscription_account_usage
               WHERE pool_id=%s AND workspace_id=%s AND billing_period='current' AND status='active'""",
            (pool_id, actor.workspace_id),
        )
        usage = await usage_result.fetchone()
        events_result = await conn.execute(
            """SELECT event_type, amount, status, created_at
               FROM gw_subscription_pool_billing_event
               WHERE pool_id=%s AND workspace_id=%s
               ORDER BY created_at DESC LIMIT 50""",
            (pool_id, actor.workspace_id),
        )
        events = await events_result.fetchall()
    return {
        "pool_id": pool_id,
        "total_cost_credits": usage["total_cost"],
        "total_prompt_tokens": usage["total_prompt_tokens"],
        "total_completion_tokens": usage["total_completion_tokens"],
        "total_sessions": usage["total_sessions"],
        "recent_events": events,
    }


@router.get("/im/channels")
async def list_im_channels(actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,workspace_id,kind,name,endpoint,agent_id,status,config,created_at,updated_at FROM im_channel WHERE workspace_id=%s ORDER BY created_at DESC", (actor.workspace_id,))
        # Contract《720》listIMChannels: ListQuery -> ListResponse<IMChannelDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/im/channels", status_code=201)
async def create_im_channel(body: IMChannelCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    if body.kind not in IM_KINDS:
        raise HTTPException(status_code=422, detail="Unsupported IM channel")
    if _controlled(body.endpoint):
        if not body.endpoint.startswith(f"mock://im/{body.kind}"):
            raise HTTPException(status_code=422, detail="Controlled IM endpoint must match channel kind")
        initial_status = "active"
    else:
        validation = validate_outbound_url(body.endpoint)
        if not validation.allowed:
            raise HTTPException(status_code=422, detail=validation.reason)
        initial_status = "pending_external"
    channel_id = new_id("imc")
    async with pool.connection() as conn:
        result = await conn.execute("INSERT INTO im_channel(id,org_id,workspace_id,kind,name,endpoint,signing_secret_enc,signing_secret_hash,agent_id,status,config,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING id,workspace_id,kind,name,endpoint,agent_id,status,config,created_at,updated_at", (channel_id, actor.org_id, actor.workspace_id, body.kind, body.name, body.endpoint, encrypt_secret(body.signing_secret), hash_secret(body.signing_secret) if body.signing_secret else None, body.agent_id, initial_status, json_dumps(body.config), actor.user_id))
        row = await result.fetchone()
        await conn.commit()
    return row


@router.get("/im/channels/{channel_id}/messages")
async def list_im_messages(channel_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,channel_id,external_message_id,direction,sender_ref_hash,payload_min,status,response_summary,created_at FROM im_message WHERE channel_id=%s AND workspace_id=%s ORDER BY created_at DESC LIMIT 100", (channel_id, actor.workspace_id))
        # Contract《720》listIMMessages: ListQuery -> ListResponse<IMMessageDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/im/channels/{channel_id}/events", status_code=201)
async def receive_im_event(channel_id: str, body: IMMessageCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        channel_result = await conn.execute("SELECT * FROM im_channel WHERE id=%s AND workspace_id=%s AND status IN ('active','pending_external')", (channel_id, actor.workspace_id))
        channel = await channel_result.fetchone()
        if not channel:
            raise HTTPException(status_code=404, detail="IM channel not found")
        duplicate = await conn.execute("SELECT id,status FROM im_message WHERE channel_id=%s AND external_message_id=%s AND direction='inbound'", (channel_id, body.external_message_id))
        existing = await duplicate.fetchone()
        if existing:
            return {"id": existing["id"], "status": "replayed", "replayed": True}
        mode = "controlled_mock" if _controlled(channel["endpoint"]) else "external_http"
        message_status = "accepted" if mode == "controlled_mock" else "pending_external"
        result = await conn.execute("INSERT INTO im_message(id,channel_id,workspace_id,external_message_id,direction,sender_ref_hash,payload_min,status,response_summary) VALUES(%s,%s,%s,%s,'inbound',%s,%s::jsonb,%s,%s::jsonb) RETURNING id,status,response_summary", (new_id("imm"), channel_id, actor.workspace_id, body.external_message_id, hash_secret(body.sender_ref) if body.sender_ref else None, json_dumps({"content": normalize_im_content(channel["kind"], body.content), "metadata": _safe_payload(body.metadata)}), message_status, json_dumps({"execution_mode": mode, "agent_status": "queued" if mode == "controlled_mock" else "pending_external"})))
        row = await result.fetchone()
        await conn.commit()
    return {**row, "replayed": False, "execution_mode": mode}


@router.post("/im/channels/{channel_id}/messages", status_code=201)
async def send_im_message(channel_id: str, body: IMMessageCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _admin(actor)
    async with pool.connection() as conn:
        channel_result = await conn.execute("SELECT * FROM im_channel WHERE id=%s AND workspace_id=%s AND status IN ('active','pending_external')", (channel_id, actor.workspace_id))
        channel = await channel_result.fetchone()
        if not channel:
            raise HTTPException(status_code=404, detail="IM channel not found")
        duplicate = await conn.execute("SELECT id,status FROM im_message WHERE channel_id=%s AND external_message_id=%s AND direction='outbound'", (channel_id, body.external_message_id))
        existing = await duplicate.fetchone()
        if existing:
            return {"id": existing["id"], "status": "replayed", "replayed": True}
        mode = "controlled_mock" if _controlled(channel["endpoint"]) else "external_http"
        message_status = "delivered" if mode == "controlled_mock" else "pending_external"
        result = await conn.execute("INSERT INTO im_message(id,channel_id,workspace_id,external_message_id,direction,payload_min,status,response_summary) VALUES(%s,%s,%s,%s,'outbound',%s::jsonb,%s,%s::jsonb) RETURNING id,status,response_summary", (new_id("imm"), channel_id, actor.workspace_id, body.external_message_id, json_dumps({"content": body.content, "metadata": _safe_payload(body.metadata)}), message_status, json_dumps({"execution_mode": mode, "delivery": message_status})))
        row = await result.fetchone()
        await conn.commit()
    return {**row, "replayed": False, "execution_mode": mode}


@public_router.get("/miniapp/manifest")
async def get_miniapp_manifest():
    return miniapp_manifest()


@router.get("/miniapp/bootstrap")
async def miniapp_bootstrap(actor: Annotated[Actor, Depends(get_actor)]):
    return {**miniapp_manifest(), "workspace_id": actor.workspace_id, "user_id": actor.user_id, "provider": "wechat", "subscription_delivery": "pending_external"}


@router.get("/miniapp/sessions")
async def list_miniapp_sessions(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,provider,status,created_at,updated_at FROM miniapp_session WHERE workspace_id=%s AND user_id=%s ORDER BY updated_at DESC LIMIT 50", (actor.workspace_id, actor.user_id))
        # Contract《720》listMiniappSessions: ListQuery -> ListResponse<MiniappSessionDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/miniapp/sessions", status_code=201)
async def create_miniapp_session(actor: Annotated[Actor, Depends(get_actor)]):
    session_id = new_id("mps")
    async with pool.connection() as conn:
        result = await conn.execute("INSERT INTO miniapp_session(id,org_id,workspace_id,user_id) VALUES(%s,%s,%s,%s) RETURNING id,provider,status,created_at,updated_at", (session_id, actor.org_id, actor.workspace_id, actor.user_id))
        row = await result.fetchone()
        await conn.commit()
    return row


@router.get("/miniapp/sessions/{session_id}/messages")
async def list_miniapp_messages(session_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        owner = await conn.execute("SELECT 1 FROM miniapp_session WHERE id=%s AND workspace_id=%s AND user_id=%s", (session_id, actor.workspace_id, actor.user_id))
        if not await owner.fetchone():
            raise HTTPException(status_code=404, detail="Miniapp session not found")
        result = await conn.execute("SELECT id,role,content,status,created_at FROM miniapp_message WHERE session_id=%s AND workspace_id=%s ORDER BY created_at", (session_id, actor.workspace_id))
        # Contract《720》listMiniappMessages: ListQuery -> ListResponse<MiniappMessageDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/miniapp/sessions/{session_id}/messages", status_code=201)
async def create_miniapp_message(session_id: str, body: MiniappMessageCreate, actor: Annotated[Actor, Depends(get_actor)]):
    user_message_id = new_id("mpm")
    assistant_message_id = new_id("mpm")
    async with pool.connection() as conn:
        owner = await conn.execute("SELECT 1 FROM miniapp_session WHERE id=%s AND workspace_id=%s AND user_id=%s AND status='active'", (session_id, actor.workspace_id, actor.user_id))
        if not await owner.fetchone():
            raise HTTPException(status_code=404, detail="Miniapp session not found")
        safe_content = body.content.strip()
        reply = f"WorkAMA controlled miniapp reply: {safe_content[:500]}"
        await conn.execute("INSERT INTO miniapp_message(id,session_id,workspace_id,role,content,status) VALUES(%s,%s,%s,'user',%s,'delivered')", (user_message_id, session_id, actor.workspace_id, safe_content))
        await conn.execute("INSERT INTO miniapp_message(id,session_id,workspace_id,role,content,status) VALUES(%s,%s,%s,'assistant',%s,'delivered')", (assistant_message_id, session_id, actor.workspace_id, reply))
        await conn.execute("UPDATE miniapp_session SET updated_at=now() WHERE id=%s", (session_id,))
        await conn.commit()
    return {"user_message_id": user_message_id, "assistant_message_id": assistant_message_id, "content": reply, "execution_mode": "controlled_mock", "provider_exchange": "pending_external"}


@router.post("/miniapp/subscriptions", status_code=201)
async def create_miniapp_subscriptions(body: MiniappSubscriptionCreate, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        ids: list[str] = []
        for topic in sorted(set(body.topics)):
            result = await conn.execute("INSERT INTO miniapp_subscription(id,workspace_id,user_id,topic) VALUES(%s,%s,%s,%s) ON CONFLICT(workspace_id,user_id,topic,provider) DO UPDATE SET status='pending_external',updated_at=now() RETURNING id", (new_id("mpsub"), actor.workspace_id, actor.user_id, topic))
            ids.append((await result.fetchone())["id"])
        await conn.commit()
    return {"ids": ids, "status": "pending_external", "provider": "wechat"}


@router.get("/channel-bindings")
async def list_channel_bindings(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
):
    _require_channel_binding(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id,org_id,workspace_id,channel_type,external_subject,credential_ref,mapping,status,last_sync,
                   created_by,created_at,updated_at
            FROM ag_channel_binding
            WHERE workspace_id=%s
            ORDER BY updated_at DESC LIMIT %s
            """,
            (actor.workspace_id, limit),
        )
        # Contract《720》listChannelBindings: ListQuery -> ListResponse<ChannelBindingDTO>
        # 保留 items 字段向后兼容
        data = await result.fetchall()
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/channel-bindings", status_code=201)
async def create_channel_binding(body: ChannelBindingCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require_channel_binding(actor, "create")
    binding_id = new_id("chbind")
    async with pool.connection() as conn:
        try:
            result = await conn.execute(
                """
                INSERT INTO ag_channel_binding(
                  id,org_id,workspace_id,channel_type,external_subject,credential_ref,mapping,created_by
                ) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING *
                """,
                (binding_id, actor.org_id, actor.workspace_id, body.channel_type, body.external_subject, body.credential_ref, json_dumps(body.mapping), actor.user_id),
            )
            row = await result.fetchone()
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            if "duplicate key" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Channel binding already exists for this channel and subject") from exc
            raise
    return row


@router.get("/channel-bindings/{binding_id}")
async def get_channel_binding(binding_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_channel_binding(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id,org_id,workspace_id,channel_type,external_subject,credential_ref,mapping,status,last_sync,
                   created_by,created_at,updated_at
            FROM ag_channel_binding WHERE id=%s AND workspace_id=%s
            """,
            (binding_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Channel binding not found")
    return row


@router.patch("/channel-bindings/{binding_id}")
async def update_channel_binding(
    binding_id: str,
    body: ChannelBindingPatch,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_channel_binding(actor, "write")
    updates: list[str] = []
    values: list[Any] = []
    if body.external_subject is not None:
        updates.append("external_subject=%s")
        values.append(body.external_subject)
    if body.credential_ref is not None:
        updates.append("credential_ref=%s")
        values.append(body.credential_ref)
    if body.mapping is not None:
        updates.append("mapping=%s::jsonb")
        values.append(json_dumps(body.mapping))
    if body.status is not None:
        updates.append("status=%s")
        values.append(body.status)
    if not updates:
        return await get_channel_binding(binding_id, actor)
    updates.append("updated_at=now()")
    values.extend([binding_id, actor.workspace_id])
    async with pool.connection() as conn:
        try:
            result = await conn.execute(
                f"""
                UPDATE ag_channel_binding SET {', '.join(updates)}
                WHERE id=%s AND workspace_id=%s
                RETURNING id,org_id,workspace_id,channel_type,external_subject,credential_ref,mapping,status,last_sync,
                          created_by,created_at,updated_at
                """,
                values,
            )
            row = await result.fetchone()
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            if "duplicate key" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Channel binding already exists for this channel and subject") from exc
            raise
    if not row:
        raise HTTPException(status_code=404, detail="Channel binding not found")
    return row


@router.delete("/channel-bindings/{binding_id}", status_code=204)
async def delete_channel_binding(binding_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_channel_binding(actor, "delete")
    async with pool.connection() as conn:
        result = await conn.execute(
            "DELETE FROM ag_channel_binding WHERE id=%s AND workspace_id=%s RETURNING id",
            (binding_id, actor.workspace_id),
        )
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Channel binding not found")
    return Response(status_code=204)

