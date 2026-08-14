from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from workama_platform.core import Actor, capability_allows, get_actor, hash_secret, json_dumps, new_id, pool
from workama_platform.modules.security.service import validate_outbound_url, validate_resolved_outbound_url


router = APIRouter(prefix="/api/v1", tags=["open-platform"])
public_router = APIRouter(prefix="/api/v1/public", tags=["open-platform-public"])

_CLIENT_ID_RE = re.compile(r"^wama_client_[A-Za-z0-9_-]{16,80}$")
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,63}$")
_EVENT_RE = re.compile(r"^[a-z0-9]+(?:[._:-][a-z0-9]+)*$|^\*$")
_CONTROLLED_WEBHOOK_RE = re.compile(
    r"^(?:mock|local)://webhook/[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,63}){0,3}$"
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
OAUTH_CODE_TTL = timedelta(minutes=5)
ACCESS_TOKEN_TTL = timedelta(minutes=15)
REFRESH_TOKEN_TTL = timedelta(days=30)
WEBHOOK_MAX_BODY_BYTES = 256 * 1024
WEBHOOK_MAX_RESPONSE_BYTES = 256 * 1024
WEBHOOK_MAX_ATTEMPTS = 5
WEBHOOK_RETRY_BASE_SECONDS = 2
WEBHOOK_RETRY_MAX_SECONDS = 300
WEBHOOK_TIMEOUT_SECONDS = 10.0
WEBHOOK_EVENTS = frozenset(
    {
        "*",
        "artifact.created",
        "assistant.created",
        "automation.run.updated",
        "dataset.updated",
        "workflow.run.updated",
        # P1 expansion to full 720 §11 event catalog
        "session.created",
        "session.completed",
        "session.failed",
        "approval.requested",
        "approval.decided",
        "dataset.indexed",
        "dataset.failed",
        "app.published",
        "app.unpublished",
        "workflow.completed",
        "workflow.failed",
        "billing.balance.low",
        "billing.subscription.changed",
        "quota.blocked",
        "member.created",
        "member.removed",
        "security.policy.changed",
        "data_request.completed",
    }
)


def _require(actor: Actor, capability: str) -> None:
    if not capability_allows(actor.capabilities, capability):
        raise HTTPException(status_code=403, detail=f"Missing capability: {capability}")


def _normalize_list(values: list[str], *, pattern: re.Pattern[str], name: str, max_items: int) -> list[str]:
    if len(values) > max_items:
        raise ValueError(f"{name} contains too many items")
    normalized: list[str] = []
    for value in values:
        item = value.strip().lower()
        if not pattern.fullmatch(item):
            raise ValueError(f"{name} contains an invalid value")
        if item not in normalized:
            normalized.append(item)
    return sorted(normalized)


def validate_redirect_uri(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.fragment or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("redirect_uri must be an absolute URL without fragment or userinfo")
    if parsed.scheme == "https":
        result = validate_outbound_url(value)
        if not result.allowed:
            raise ValueError(f"redirect_uri is unsafe: {result.reason}")
        return value
    if parsed.scheme == "http" and parsed.hostname.lower().rstrip(".") in _LOCAL_HOSTS:
        return value
    raise ValueError("redirect_uri must use HTTPS except for localhost development")


def _is_controlled_webhook_url(value: str) -> bool:
    return bool(_CONTROLLED_WEBHOOK_RE.fullmatch(value.strip()))


def _validate_webhook_url(value: str) -> str:
    value = value.strip()
    if _is_controlled_webhook_url(value):
        return value
    result = validate_outbound_url(value)
    if not result.allowed:
        raise ValueError(f"webhook url is unsafe: {result.reason}")
    return value


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _token(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


def _last4(value: str) -> str:
    return value[-4:]


class OAuthClientCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    redirect_uris: list[str] = Field(min_length=1, max_length=16)
    scopes: list[str] = Field(default_factory=lambda: ["openid"], max_length=32)
    grant_types: list[Literal["authorization_code", "refresh_token", "client_credentials"]] = Field(
        default_factory=lambda: ["authorization_code", "refresh_token"], max_length=3
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("name is required")
        return value

    @field_validator("redirect_uris")
    @classmethod
    def normalize_redirects(cls, values: list[str]) -> list[str]:
        normalized = [validate_redirect_uri(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("redirect_uris must be unique")
        return normalized

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, values: list[str]) -> list[str]:
        return _normalize_list(values, pattern=_SCOPE_RE, name="scopes", max_items=32)

    @model_validator(mode="after")
    def validate_grants(self) -> "OAuthClientCreate":
        if "authorization_code" not in self.grant_types:
            raise ValueError("authorization_code grant is required for this baseline")
        return self


class OAuthClientPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    redirect_uris: list[str] | None = Field(default=None, max_length=16)
    scopes: list[str] | None = Field(default=None, max_length=32)
    status: Literal["active", "disabled"] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value is not None else None

    @field_validator("redirect_uris")
    @classmethod
    def normalize_redirects(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [validate_redirect_uri(value) for value in values]
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("redirect_uris must be non-empty and unique")
        return normalized

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, values: list[str] | None) -> list[str] | None:
        return _normalize_list(values, pattern=_SCOPE_RE, name="scopes", max_items=32) if values is not None else None


class OAuthAuthorizeQuery(BaseModel):
    client_id: str = Field(min_length=20, max_length=96)
    redirect_uri: str = Field(min_length=1, max_length=2048)
    response_type: Literal["code"] = "code"
    code_challenge: str = Field(min_length=43, max_length=128)
    code_challenge_method: Literal["S256"] = "S256"
    scope: str = Field(default="openid", min_length=1, max_length=512)
    state: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, value: str) -> str:
        if not _CLIENT_ID_RE.fullmatch(value):
            raise ValueError("client_id is invalid")
        return value

    @field_validator("redirect_uri")
    @classmethod
    def validate_redirect(cls, value: str) -> str:
        return validate_redirect_uri(value)

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        values = _normalize_list(value.split(), pattern=_SCOPE_RE, name="scope", max_items=32)
        return " ".join(values)


class OAuthTokenRequest(BaseModel):
    grant_type: Literal["authorization_code", "refresh_token"]
    client_id: str = Field(min_length=20, max_length=96)
    client_secret: str = Field(min_length=1, max_length=256)
    code: str | None = Field(default=None, max_length=256)
    refresh_token: str | None = Field(default=None, max_length=256)
    redirect_uri: str | None = Field(default=None, max_length=2048)
    code_verifier: str | None = Field(default=None, min_length=43, max_length=256)

    @model_validator(mode="after")
    def validate_grant(self) -> "OAuthTokenRequest":
        if self.grant_type == "authorization_code" and (not self.code or not self.redirect_uri or not self.code_verifier):
            raise ValueError("authorization_code requires code, redirect_uri and code_verifier")
        if self.grant_type == "refresh_token" and not self.refresh_token:
            raise ValueError("refresh_token grant requires refresh_token")
        if self.redirect_uri:
            validate_redirect_uri(self.redirect_uri)
        return self


class OAuthRevokeRequest(BaseModel):
    client_id: str = Field(min_length=20, max_length=96)
    client_secret: str = Field(min_length=1, max_length=256)
    token: str = Field(min_length=16, max_length=256)


class WebhookCreate(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    events: list[str] = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=500)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_webhook_url(value)

    @field_validator("events")
    @classmethod
    def validate_events(cls, values: list[str]) -> list[str]:
        normalized = _normalize_list(values, pattern=_EVENT_RE, name="events", max_items=32)
        unknown = sorted(set(normalized) - WEBHOOK_EVENTS)
        if unknown:
            raise ValueError(f"unsupported webhook events: {', '.join(unknown)}")
        return normalized


class WebhookPatch(BaseModel):
    url: str | None = Field(default=None, max_length=2048)
    events: list[str] | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=500)
    status: Literal["active", "disabled"] | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_webhook_url(value)

    @field_validator("events")
    @classmethod
    def validate_events(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = _normalize_list(values, pattern=_EVENT_RE, name="events", max_items=32)
        if set(normalized) - WEBHOOK_EVENTS:
            raise ValueError("events contains unsupported event type")
        return normalized


class WebhookTestRequest(BaseModel):
    event_type: str = Field(default="artifact.created", min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=160)

    @field_validator("event_type")
    @classmethod
    def validate_event(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EVENT_RE.fullmatch(value) or value not in WEBHOOK_EVENTS - {"*"}:
            raise ValueError("event_type is unsupported")
        return value

    @model_validator(mode="after")
    def validate_payload_size(self) -> "WebhookTestRequest":
        if len(json_dumps({"event_type": self.event_type, "payload": self.payload}).encode("utf-8")) > WEBHOOK_MAX_BODY_BYTES:
            raise ValueError("webhook payload is too large")
        return self


class OpenPlatformDocCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(default="", max_length=50_000)
    doc_type: Literal["guide", "api_reference", "sdk", "quickstart", "webhook", "oauth"] = "guide"
    sort_order: int = Field(default=0, ge=0)
    status: Literal["draft", "published", "archived"] = "published"


class OpenPlatformDocPatch(BaseModel):
    slug: str | None = Field(default=None, min_length=1, max_length=120)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    content: str | None = Field(default=None, max_length=50_000)
    doc_type: Literal["guide", "api_reference", "sdk", "quickstart", "webhook", "oauth"] | None = None
    sort_order: int | None = Field(default=None, ge=0)
    status: Literal["draft", "published", "archived"] | None = None


@public_router.get("/docs")
async def list_public_docs():
    """Return public platform documentation links and published doc blocks."""
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT slug, title, content, doc_type, sort_order, status, created_at, updated_at
               FROM pf_open_platform_doc
               WHERE status='published'
               ORDER BY sort_order, created_at""",
        )
        docs = await result.fetchall()
    return {
        "openapi_url": "/api/v1/openapi.json",
        "sdk_downloads": {
            "python": {"npm_or_pip": "pip install workama-sdk", "repo": "packages/sdk-python", "version": "0.1.0"},
            "javascript": {"npm_or_pip": "pnpm add @workama/sdk", "repo": "packages/sdk-js", "version": "0.1.0"},
            "go": {"status": "planned", "version": None},
        },
        "quickstart_url": "/docs/quickstart",
        "webhook_guide_url": "/docs/webhooks",
        "oauth_guide_url": "/docs/oauth",
        "docs": docs,
    }


@router.get("/open-platform/docs")
async def list_open_platform_docs(actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "oauth_client:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id, slug, title, content, doc_type, sort_order, status, created_by, created_at, updated_at
               FROM pf_open_platform_doc
               WHERE workspace_id=%s OR workspace_id IS NULL
               ORDER BY sort_order, created_at""",
            (actor.workspace_id,),
        )
        docs = await result.fetchall()
    return {
        "items": docs,
        "data": docs,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(docs)},
    }


@router.post("/open-platform/docs", status_code=201)
async def create_open_platform_doc(body: OpenPlatformDocCreate, actor: Annotated[Actor, Depends(get_actor)]):
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")
    doc_id = new_id("opdoc")
    async with pool.connection() as conn:
        try:
            result = await conn.execute(
                """INSERT INTO pf_open_platform_doc(id,workspace_id,slug,title,content,doc_type,sort_order,status,created_by)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id,slug,title,content,doc_type,sort_order,status,created_by,created_at,updated_at""",
                (doc_id, actor.workspace_id, body.slug, body.title, body.content, body.doc_type, body.sort_order, body.status, actor.user_id),
            )
            row = await result.fetchone()
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Doc slug already exists") from exc
            raise
    return row


@router.get("/open-platform/docs/{doc_id}")
async def get_open_platform_doc(doc_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "oauth_client:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,slug,title,content,doc_type,sort_order,status,created_by,created_at,updated_at
               FROM pf_open_platform_doc WHERE id=%s AND (workspace_id=%s OR workspace_id IS NULL)""",
            (doc_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Doc not found")
    return row


@router.patch("/open-platform/docs/{doc_id}")
async def patch_open_platform_doc(doc_id: str, body: OpenPlatformDocPatch, actor: Annotated[Actor, Depends(get_actor)]):
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")
    updates: list[str] = []
    values: list[Any] = []
    for field in ("slug", "title", "content", "doc_type", "sort_order", "status"):
        value = getattr(body, field)
        if value is not None:
            updates.append(f"{field}=%s")
            values.append(value)
    if not updates:
        return await get_open_platform_doc(doc_id, actor)
    updates.append("updated_at=now()")
    values.extend([doc_id, actor.workspace_id])
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""UPDATE pf_open_platform_doc SET {', '.join(updates)}
                WHERE id=%s AND (workspace_id=%s OR workspace_id IS NULL)
                RETURNING id,slug,title,content,doc_type,sort_order,status,created_by,created_at,updated_at""",
            values,
        )
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Doc not found")
    return row


@router.delete("/open-platform/docs/{doc_id}", status_code=204)
async def delete_open_platform_doc(doc_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")
    async with pool.connection() as conn:
        result = await conn.execute(
            "DELETE FROM pf_open_platform_doc WHERE id=%s AND (workspace_id=%s OR workspace_id IS NULL) RETURNING id",
            (doc_id, actor.workspace_id),
        )
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Doc not found")
    return Response(status_code=204)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS pf_oauth_client (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      client_id TEXT NOT NULL UNIQUE,
      name TEXT NOT NULL,
      client_secret_hash TEXT NOT NULL,
      client_secret_last4 TEXT NOT NULL,
      redirect_uris TEXT[] NOT NULL DEFAULT ARRAY[]::text[],
      scopes TEXT[] NOT NULL DEFAULT ARRAY['openid'],
      grant_types TEXT[] NOT NULL DEFAULT ARRAY['authorization_code','refresh_token'],
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','revoked')),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      last_used_at TIMESTAMPTZ,
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_oauth_client_workspace_status ON pf_oauth_client(workspace_id,status,updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS pf_oauth_code (
      id TEXT PRIMARY KEY,
      client_id TEXT NOT NULL REFERENCES pf_oauth_client(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
      code_hash TEXT NOT NULL UNIQUE,
      redirect_uri TEXT NOT NULL,
      scope TEXT NOT NULL,
      code_challenge TEXT NOT NULL,
      expires_at TIMESTAMPTZ NOT NULL,
      consumed_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_oauth_code_expiry ON pf_oauth_code(expires_at,consumed_at)",
    """
    CREATE TABLE IF NOT EXISTS pf_oauth_token (
      id TEXT PRIMARY KEY,
      client_id TEXT NOT NULL REFERENCES pf_oauth_client(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES id_user(id) ON DELETE CASCADE,
      access_token_hash TEXT NOT NULL UNIQUE,
      refresh_token_hash TEXT NOT NULL UNIQUE,
      scope TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked','expired')),
      access_expires_at TIMESTAMPTZ NOT NULL,
      refresh_expires_at TIMESTAMPTZ NOT NULL,
      last_used_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_oauth_token_workspace ON pf_oauth_token(workspace_id,status,created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS pf_webhook (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      url TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      events TEXT[] NOT NULL,
      secret_hash TEXT NOT NULL,
      secret_last4 TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','revoked')),
      failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
      last_delivered_at TIMESTAMPTZ,
      version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_webhook_workspace_status ON pf_webhook(workspace_id,status,updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS pf_webhook_delivery (
      id TEXT PRIMARY KEY,
      webhook_id TEXT NOT NULL REFERENCES pf_webhook(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
      event_type TEXT NOT NULL,
      idempotency_key TEXT NOT NULL,
      payload_hash TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivering','retry_wait','delivered','failed','disabled','blocked_external')),
      attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
      next_attempt_at TIMESTAMPTZ,
      response_code INTEGER,
      error_code TEXT,
      delivery_mode TEXT NOT NULL DEFAULT 'external' CHECK (delivery_mode IN ('controlled_mock','external','blocked_external')),
      payload JSONB NOT NULL DEFAULT '{}'::jsonb,
      signature TEXT,
      response_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
      claimed_at TIMESTAMPTZ,
      delivered_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(webhook_id,idempotency_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_webhook_delivery_webhook_time ON pf_webhook_delivery(webhook_id,created_at DESC)",
    "ALTER TABLE pf_webhook_delivery ADD COLUMN IF NOT EXISTS delivery_mode TEXT NOT NULL DEFAULT 'blocked_external'",
    "ALTER TABLE pf_webhook_delivery ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE pf_webhook_delivery ADD COLUMN IF NOT EXISTS signature TEXT",
    "ALTER TABLE pf_webhook_delivery ADD COLUMN IF NOT EXISTS response_summary JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE pf_webhook_delivery ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ",
    "ALTER TABLE pf_webhook_delivery ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ",
    "ALTER TABLE pf_webhook_delivery DROP CONSTRAINT IF EXISTS pf_webhook_delivery_status_check",
    "ALTER TABLE pf_webhook_delivery ADD CONSTRAINT pf_webhook_delivery_status_check CHECK (status IN ('pending','delivering','retry_wait','delivered','failed','disabled','blocked_external'))",
    "ALTER TABLE pf_webhook_delivery DROP CONSTRAINT IF EXISTS pf_webhook_delivery_delivery_mode_check",
    "ALTER TABLE pf_webhook_delivery ADD CONSTRAINT pf_webhook_delivery_delivery_mode_check CHECK (delivery_mode IN ('controlled_mock','external','blocked_external'))",
    "ALTER TABLE pf_webhook_delivery ALTER COLUMN status SET DEFAULT 'pending'",
    "ALTER TABLE pf_webhook_delivery ALTER COLUMN delivery_mode SET DEFAULT 'external'",
    "UPDATE pf_webhook_delivery SET status='pending' WHERE status='blocked_external'",
    "UPDATE pf_webhook_delivery SET delivery_mode='external' WHERE delivery_mode='blocked_external' AND status='pending'",
    """
    CREATE TABLE IF NOT EXISTS pf_open_platform_doc (
      id TEXT PRIMARY KEY,
      workspace_id TEXT REFERENCES id_workspace(id) ON DELETE CASCADE,
      slug TEXT NOT NULL,
      title TEXT NOT NULL,
      content TEXT NOT NULL DEFAULT '',
      doc_type TEXT NOT NULL DEFAULT 'guide' CHECK (doc_type IN ('guide','api_reference','sdk','quickstart','webhook','oauth')),
      sort_order INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'published' CHECK (status IN ('draft','published','archived')),
      created_by TEXT NOT NULL REFERENCES id_user(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, slug)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_open_platform_doc_published ON pf_open_platform_doc(status, doc_type, sort_order)",
)


async def ensure_open_platform_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


def _client_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "name": row["name"],
        "redirect_uris": row["redirect_uris"],
        "scopes": row["scopes"],
        "grant_types": row["grant_types"],
        "status": row["status"],
        "secret_status": "configured",
        "secret_last4": row["client_secret_last4"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _webhook_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "url": _masked_url(row["url"]),
        "events": row["events"],
        "description": row["description"],
        "secret_status": "configured",
        "secret_last4": row["secret_last4"],
        "status": row["status"],
        "failure_count": row["failure_count"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _masked_url(value: str) -> str:
    parsed = urlsplit(value)
    path = parsed.path or "/"
    return f"{parsed.scheme}://{parsed.hostname}{path}"


@router.get("/oauth/clients")
async def list_oauth_clients(actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "oauth_client:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,client_id,name,redirect_uris,scopes,grant_types,status,client_secret_last4,version,created_at,updated_at
               FROM pf_oauth_client WHERE workspace_id=%s ORDER BY created_at DESC""",
            (actor.workspace_id,),
        )
        data = [_client_public(row) for row in await result.fetchall()]
    # Contract 720 listOAuthClients ListQuery -> ListResponse<OAuthClientDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/oauth/clients", status_code=201)
async def create_oauth_client(body: OAuthClientCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "oauth_client:create")
    client_id = _token("wama_client_")
    client_secret = _token("wama_secret_")
    client_db_id = new_id("oauth")
    async with pool.connection() as conn:
        try:
            await conn.execute(
                """INSERT INTO pf_oauth_client(id,org_id,workspace_id,client_id,name,client_secret_hash,client_secret_last4,redirect_uris,scopes,grant_types,created_by)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (client_db_id, actor.org_id, actor.workspace_id, client_id, body.name, hash_secret(client_secret), _last4(client_secret), body.redirect_uris, body.scopes, body.grant_types, actor.user_id),
            )
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="OAuth client name already exists") from exc
            raise
    return {"client_id": client_id, "client_secret": client_secret, "secret_status": "shown_once", "name": body.name, "status": "active"}


@router.get("/oauth/clients/{client_id}")
async def get_oauth_client(client_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "oauth_client:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,client_id,name,redirect_uris,scopes,grant_types,status,client_secret_last4,version,created_at,updated_at
               FROM pf_oauth_client WHERE client_id=%s AND workspace_id=%s""",
            (client_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="OAuth client not found")
    return _client_public(row)


@router.patch("/oauth/clients/{client_id}")
async def patch_oauth_client(client_id: str, body: OAuthClientPatch, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "oauth_client:write")
    updates: list[str] = []
    values: list[Any] = []
    if body.name is not None:
        updates.append("name=%s")
        values.append(body.name)
    if body.redirect_uris is not None:
        updates.append("redirect_uris=%s")
        values.append(body.redirect_uris)
    if body.scopes is not None:
        updates.append("scopes=%s")
        values.append(body.scopes)
    if body.status is not None:
        updates.append("status=%s")
        values.append(body.status)
    if not updates:
        return await get_oauth_client(client_id, actor)
    updates.extend(["version=version+1", "updated_at=now()"])
    values.extend([client_id, actor.workspace_id])
    async with pool.connection() as conn:
        result = await conn.execute(
            f"UPDATE pf_oauth_client SET {', '.join(updates)} WHERE client_id=%s AND workspace_id=%s RETURNING id,client_id,name,redirect_uris,scopes,grant_types,status,client_secret_last4,version,created_at,updated_at",
            values,
        )
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="OAuth client not found")
    return _client_public(row)


@router.delete("/oauth/clients/{client_id}", status_code=204)
async def delete_oauth_client(client_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "oauth_client:delete")
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE pf_oauth_client SET status='revoked',version=version+1,updated_at=now() WHERE client_id=%s AND workspace_id=%s", (client_id, actor.workspace_id))
        await conn.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="OAuth client not found")
    return Response(status_code=204)


@router.post("/oauth/clients/{client_id}/secret-rotations")
async def rotate_oauth_client_secret(client_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "oauth_client:write")
    new_secret = _token("wama_secret_")
    async with pool.connection() as conn:
        result = await conn.execute(
            """UPDATE pf_oauth_client
               SET client_secret_hash=%s, client_secret_last4=%s, version=version+1, updated_at=now()
               WHERE client_id=%s AND workspace_id=%s AND status='active'
               RETURNING id, client_id, name, status, version, updated_at""",
            (hash_secret(new_secret), _last4(new_secret), client_id, actor.workspace_id),
        )
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="OAuth client not found")
    return {"client_id": row["client_id"], "client_secret": new_secret, "secret_status": "shown_once", "status": row["status"], "version": row["version"]}


@router.get("/oauth/authorize")
async def authorize_oauth(query: Annotated[OAuthAuthorizeQuery, Query()], actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,workspace_id,redirect_uris,scopes,status FROM pf_oauth_client WHERE client_id=%s AND workspace_id=%s", (query.client_id, actor.workspace_id))
        client = await result.fetchone()
        if not client or client["status"] != "active":
            raise HTTPException(status_code=400, detail="OAuth client is unavailable")
        if query.redirect_uri not in client["redirect_uris"]:
            raise HTTPException(status_code=400, detail="redirect_uri is not registered")
        requested = set(query.scope.split())
        if not requested.issubset(set(client["scopes"])):
            raise HTTPException(status_code=400, detail="scope is not allowed")
        code = _token("wama_code_")
        await conn.execute(
            """INSERT INTO pf_oauth_code(id,client_id,workspace_id,user_id,code_hash,redirect_uri,scope,code_challenge,expires_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (new_id("oauthcode"), client["id"], actor.workspace_id, actor.user_id, hash_secret(code), query.redirect_uri, query.scope, query.code_challenge, datetime.now(UTC) + OAUTH_CODE_TTL),
        )
        await conn.commit()
    return {"code": code, "state": query.state, "redirect_uri": query.redirect_uri, "expires_in": int(OAUTH_CODE_TTL.total_seconds()), "provider_execution": "pending_external_exchange"}


@router.post("/oauth/token")
async def exchange_oauth_token(body: OAuthTokenRequest):
    async with pool.connection() as conn:
        client_result = await conn.execute("SELECT id,workspace_id,client_secret_hash,status FROM pf_oauth_client WHERE client_id=%s", (body.client_id,))
        client = await client_result.fetchone()
        if not client or client["status"] != "active" or not hmac.compare_digest(client["client_secret_hash"], hash_secret(body.client_secret)):
            raise HTTPException(status_code=401, detail="OAuth client credentials are invalid")
        if body.grant_type == "authorization_code":
            result = await conn.execute("SELECT id,user_id,workspace_id,redirect_uri,scope,code_challenge,expires_at,consumed_at FROM pf_oauth_code WHERE client_id=%s AND code_hash=%s FOR UPDATE", (client["id"], hash_secret(body.code or "")))
            code = await result.fetchone()
            if not code or code["consumed_at"] or code["expires_at"] <= datetime.now(UTC) or code["redirect_uri"] != body.redirect_uri or _pkce_challenge(body.code_verifier or "") != code["code_challenge"]:
                raise HTTPException(status_code=400, detail="Authorization code is invalid, expired, consumed, or PKCE verification failed")
            await conn.execute("UPDATE pf_oauth_code SET consumed_at=now() WHERE id=%s", (code["id"],))
            user_id, workspace_id, scope = code["user_id"], code["workspace_id"], code["scope"]
        else:
            result = await conn.execute("SELECT id,user_id,workspace_id,scope,refresh_expires_at,status FROM pf_oauth_token WHERE client_id=%s AND refresh_token_hash=%s FOR UPDATE", (client["id"], hash_secret(body.refresh_token or "")))
            token = await result.fetchone()
            if not token or token["status"] != "active" or token["refresh_expires_at"] <= datetime.now(UTC):
                raise HTTPException(status_code=400, detail="Refresh token is invalid or expired")
            await conn.execute("UPDATE pf_oauth_token SET status='revoked',updated_at=now() WHERE id=%s", (token["id"],))
            user_id, workspace_id, scope = token["user_id"], token["workspace_id"], token["scope"]
        access = _token("wama_at_")
        refresh = _token("wama_rt_")
        await conn.execute(
            """INSERT INTO pf_oauth_token(id,client_id,workspace_id,user_id,access_token_hash,refresh_token_hash,scope,access_expires_at,refresh_expires_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (new_id("oauthtoken"), client["id"], workspace_id, user_id, hash_secret(access), hash_secret(refresh), scope, datetime.now(UTC) + ACCESS_TOKEN_TTL, datetime.now(UTC) + REFRESH_TOKEN_TTL),
        )
        await conn.commit()
    return {"token_type": "Bearer", "access_token": access, "refresh_token": refresh, "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()), "scope": scope}


@router.post("/oauth/revocations", status_code=204)
async def revoke_oauth_token(body: OAuthRevokeRequest):
    async with pool.connection() as conn:
        client_result = await conn.execute("SELECT id,client_secret_hash FROM pf_oauth_client WHERE client_id=%s", (body.client_id,))
        client = await client_result.fetchone()
        if not client or not hmac.compare_digest(client["client_secret_hash"], hash_secret(body.client_secret)):
            raise HTTPException(status_code=401, detail="OAuth client credentials are invalid")
        await conn.execute("UPDATE pf_oauth_token SET status='revoked',updated_at=now() WHERE client_id=%s AND (access_token_hash=%s OR refresh_token_hash=%s)", (client["id"], hash_secret(body.token), hash_secret(body.token)))
        await conn.commit()
    return Response(status_code=204)


@router.get("/webhooks")
async def list_webhooks(actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "webhook:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,url,description,events,secret_last4,status,failure_count,version,created_at,updated_at FROM pf_webhook WHERE workspace_id=%s ORDER BY created_at DESC", (actor.workspace_id,))
        data = [_webhook_public(row) for row in await result.fetchall()]
    # Contract 720 listWebhooks ListQuery -> ListResponse<WebhookDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/webhooks", status_code=201)
async def create_webhook(body: WebhookCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "webhook:create")
    secret = _token("whsec_")
    webhook_id = new_id("wh")
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO pf_webhook(id,org_id,workspace_id,url,description,events,secret_hash,secret_last4,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)", (webhook_id, actor.org_id, actor.workspace_id, body.url, body.description, body.events, hash_secret(secret), _last4(secret), actor.user_id))
        result = await conn.execute("SELECT id,url,description,events,secret_last4,status,failure_count,version,created_at,updated_at FROM pf_webhook WHERE id=%s", (webhook_id,))
        row = await result.fetchone()
        await conn.commit()
    return {**_webhook_public(row), "secret": secret, "secret_status": "shown_once"}


@router.get("/webhooks/{webhook_id}")
async def get_webhook(webhook_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "webhook:read")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,url,description,events,secret_last4,status,failure_count,version,created_at,updated_at FROM pf_webhook WHERE id=%s AND workspace_id=%s", (webhook_id, actor.workspace_id))
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return _webhook_public(row)


@router.patch("/webhooks/{webhook_id}")
async def patch_webhook(webhook_id: str, body: WebhookPatch, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "webhook:write")
    updates: list[str] = []
    values: list[Any] = []
    for field in ("url", "events", "description", "status"):
        value = getattr(body, field)
        if value is not None:
            updates.append(f"{field}=%s")
            values.append(value)
    if not updates:
        return await get_webhook(webhook_id, actor)
    updates.extend(["version=version+1", "updated_at=now()"])
    values.extend([webhook_id, actor.workspace_id])
    async with pool.connection() as conn:
        result = await conn.execute(f"UPDATE pf_webhook SET {', '.join(updates)} WHERE id=%s AND workspace_id=%s RETURNING id,url,description,events,secret_last4,status,failure_count,version,created_at,updated_at", values)
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return _webhook_public(row)


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "webhook:delete")
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE pf_webhook SET status='revoked',version=version+1,updated_at=now() WHERE id=%s AND workspace_id=%s", (webhook_id, actor.workspace_id))
        await conn.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return Response(status_code=204)


@router.post("/webhooks/{webhook_id}/tests")
async def test_webhook(webhook_id: str, body: WebhookTestRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "webhook:test")
    idempotency_key = body.idempotency_key or f"test:{body.event_type}:{hashlib.sha256(json_dumps(body.payload).encode()).hexdigest()}"
    payload = json_dumps({"event_type": body.event_type, "payload": body.payload})
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,url,status,events,secret_hash FROM pf_webhook WHERE id=%s AND workspace_id=%s", (webhook_id, actor.workspace_id))
        webhook = await result.fetchone()
        if not webhook:
            raise HTTPException(status_code=404, detail="Webhook not found")
        if webhook["status"] != "active":
            raise HTTPException(status_code=409, detail="Webhook is not active")
        if body.event_type not in webhook["events"] and "*" not in webhook["events"]:
            raise HTTPException(status_code=422, detail="Webhook is not subscribed to this event")
        controlled = _is_controlled_webhook_url(webhook["url"])
        result = await conn.execute(
            "SELECT id,event_type,idempotency_key,payload_hash,status,attempt,next_attempt_at,response_code,error_code,delivery_mode,signature,delivered_at,created_at,updated_at FROM pf_webhook_delivery WHERE webhook_id=%s AND idempotency_key=%s FOR UPDATE",
            (webhook_id, idempotency_key),
        )
        row = await result.fetchone()
        replayed = False
        if row:
            if row["payload_hash"] != payload_hash:
                raise HTTPException(status_code=409, detail="Idempotency key was used with a different webhook payload")
            replayed = True
        else:
            result = await conn.execute(
                """INSERT INTO pf_webhook_delivery(id,webhook_id,workspace_id,event_type,idempotency_key,payload_hash,status,attempt,next_attempt_at,response_code,error_code,delivery_mode,payload,signature,response_summary,delivered_at)
                   VALUES(%s,%s,%s,%s,%s,%s,'pending',0,now(),NULL,NULL,%s,%s::jsonb,NULL,'{}'::jsonb,NULL)
                   RETURNING id,event_type,idempotency_key,payload_hash,status,attempt,next_attempt_at,response_code,error_code,delivery_mode,signature,delivered_at,created_at,updated_at""",
                (
                    new_id("whdel"),
                    webhook_id,
                    actor.workspace_id,
                    body.event_type,
                    idempotency_key,
                    payload_hash,
                    "controlled_mock" if controlled else "external",
                    json_dumps({"event_type": body.event_type, "payload": body.payload}),
                ),
            )
            row = await result.fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail="Webhook delivery could not be created")
        await conn.commit()
    return {
        **row,
        "external_execution": "queued",
        "idempotency_replayed": replayed,
    }


@router.get("/webhooks/{webhook_id}/deliveries")
async def list_webhook_deliveries(webhook_id: str, actor: Annotated[Actor, Depends(get_actor)], limit: int = Query(default=50, ge=1, le=100)):
    _require(actor, "webhook:read")
    async with pool.connection() as conn:
        owner = await conn.execute("SELECT 1 FROM pf_webhook WHERE id=%s AND workspace_id=%s", (webhook_id, actor.workspace_id))
        if not await owner.fetchone():
            raise HTTPException(status_code=404, detail="Webhook not found")
        result = await conn.execute("SELECT id,event_type,idempotency_key,payload_hash,status,attempt,next_attempt_at,response_code,error_code,delivery_mode,signature,delivered_at,created_at,updated_at FROM pf_webhook_delivery WHERE webhook_id=%s AND workspace_id=%s ORDER BY created_at DESC LIMIT %s", (webhook_id, actor.workspace_id, limit))
        data = await result.fetchall()
    # Contract 720 listWebhookDeliveries ListQuery -> ListResponse<WebhookDeliveryDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.get("/webhook-deliveries/{delivery_id}")
async def get_webhook_delivery(delivery_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "webhook:read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """SELECT id,webhook_id,event_type,idempotency_key,payload_hash,status,attempt,next_attempt_at,
                      response_code,error_code,delivery_mode,signature,delivered_at,created_at,updated_at
               FROM pf_webhook_delivery WHERE id=%s AND workspace_id=%s""",
            (delivery_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook delivery not found")
    return row


@router.post("/webhook-deliveries/{delivery_id}/replays")
async def replay_webhook_delivery(delivery_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require(actor, "webhook:write")
    async with pool.connection() as conn:
        result = await conn.execute(
            """UPDATE pf_webhook_delivery
               SET status='pending', attempt=0, next_attempt_at=now(), response_code=NULL, error_code=NULL,
                   response_summary='{}'::jsonb, delivered_at=NULL, updated_at=now()
               WHERE id=%s AND workspace_id=%s
               RETURNING id,webhook_id,event_type,idempotency_key,payload_hash,status,attempt,next_attempt_at,
                         response_code,error_code,delivery_mode,signature,delivered_at,created_at,updated_at""",
            (delivery_id, actor.workspace_id),
        )
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook delivery not found")
    return {**row, "external_execution": "queued"}


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip credential-like fields from webhook payloads before persistence."""
    sensitive = {"secret", "token", "password", "authorization", "api_key", "private_key", "credential", "content"}

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(k): "[redacted]" if str(k).lower() in sensitive else walk(v) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v) for v in item]
        return item

    return walk(payload)


async def publish_webhook_event(
    workspace_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Enqueue a webhook delivery for every active webhook subscribed to event_type.

    ``*`` wildcards receive all events. Payloads are stored as a summary and must
    never contain secrets, full content, or credentials. Returns a summary of
    enqueued deliveries for observability.
    """
    if event_type not in WEBHOOK_EVENTS - {"*"}:
        raise ValueError(f"unsupported webhook event type: {event_type}")
    delivery_key = idempotency_key or new_id("whidm")
    payload_hash = hashlib.sha256(json_dumps({"event_type": event_type, "payload": payload}).encode("utf-8")).hexdigest()
    created: list[dict[str, Any]] = []
    async with pool.connection() as conn:
        webhooks = await conn.execute(
            """
            SELECT id, url, secret_hash
            FROM pf_webhook
            WHERE workspace_id = %s AND status = 'active'
              AND (%s = ANY(events) OR '*' = ANY(events))
            """,
            (workspace_id, event_type),
        )
        for webhook in await webhooks.fetchall():
            result = await conn.execute(
                """
                INSERT INTO pf_webhook_delivery(
                    id, webhook_id, workspace_id, event_type, idempotency_key, payload_hash,
                    status, attempt, next_attempt_at, delivery_mode, payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', 0, now(), %s, %s::jsonb)
                ON CONFLICT (webhook_id, idempotency_key) DO NOTHING
                RETURNING id, event_type, idempotency_key, payload_hash, status, attempt, next_attempt_at, delivery_mode, created_at
                """,
                (
                    new_id("whdel"),
                    webhook["id"],
                    workspace_id,
                    event_type,
                    delivery_key,
                    payload_hash,
                    "controlled_mock" if _is_controlled_webhook_url(webhook["url"]) else "external",
                    json_dumps({"event_type": event_type, "payload": _safe_payload(payload)}),
                ),
            )
            row = await result.fetchone()
            if row:
                created.append(dict(row))
        await conn.commit()
    return {"event_type": event_type, "enqueued": len(created), "deliveries": created}


def webhook_signature(secret: str, payload: str, timestamp: int | None = None) -> str:
    timestamp = timestamp or int(datetime.now(UTC).timestamp())
    digest = hmac.new(secret.encode(), f"{timestamp}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def webhook_raw_body(event_type: str, payload: dict[str, Any]) -> bytes:
    return json_dumps({"event_type": event_type, "payload": payload}).encode("utf-8")


def webhook_retry_delay(attempt: int) -> int:
    return min(WEBHOOK_RETRY_MAX_SECONDS, WEBHOOK_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))


def _safe_delivery_summary(*, status_code: int | None, bytes_read: int = 0, reason: str | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {"status_code": status_code, "response_bytes": min(bytes_read, WEBHOOK_MAX_RESPONSE_BYTES)}
    if reason:
        summary["reason"] = reason
    return summary


async def _controlled_webhook_executor(url: str, raw_body: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """Complete a local/mock delivery without opening a socket or persisting the body."""
    return {"success": True, "response_code": 204, "error_code": None, "retryable": False, "disable": False,
            "summary": _safe_delivery_summary(status_code=204, bytes_read=len(raw_body))}


async def deliver_webhook_attempt(
    delivery: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    executor=_controlled_webhook_executor,
) -> dict[str, Any]:
    """Perform exactly one bounded webhook attempt; callers own DB state transitions."""
    raw_body = webhook_raw_body(delivery["event_type"], delivery.get("payload") or {})
    if len(raw_body) > WEBHOOK_MAX_BODY_BYTES:
        return {"success": False, "response_code": None, "error_code": "payload_too_large", "retryable": False, "disable": False,
                "signature": None, "summary": _safe_delivery_summary(status_code=None, bytes_read=0, reason="payload_too_large")}
    timestamp = int(datetime.now(UTC).timestamp())
    signature = webhook_signature(delivery["secret_hash"], raw_body.decode("utf-8"), timestamp)
    headers = {
        "content-type": "application/json",
        "user-agent": "WorkAMA-Webhook/1",
        "x-workama-event": delivery["event_type"],
        "x-workama-signature": signature,
        "idempotency-key": delivery["idempotency_key"],
    }
    url = delivery["url"]
    if _is_controlled_webhook_url(url):
        result = await executor(url, raw_body, headers)
        return {**result, "signature": signature}

    validation = await validate_resolved_outbound_url(url)
    if not validation.allowed:
        return {"success": False, "response_code": None, "error_code": "unsafe_endpoint", "retryable": False, "disable": False,
                "signature": signature, "summary": _safe_delivery_summary(status_code=None, reason=validation.reason or "unsafe_endpoint")}
    timeout = httpx.Timeout(WEBHOOK_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport) as client:
            async with client.stream("POST", url, content=raw_body, headers=headers) as response:
                bytes_read = 0
                async for chunk in response.aiter_bytes():
                    bytes_read += len(chunk)
                    if bytes_read > WEBHOOK_MAX_RESPONSE_BYTES:
                        return {"success": False, "response_code": response.status_code, "error_code": "response_too_large", "retryable": False, "disable": False,
                                "signature": signature, "summary": _safe_delivery_summary(status_code=response.status_code, bytes_read=bytes_read, reason="response_too_large")}
                code = response.status_code
                if 200 <= code < 300:
                    return {"success": True, "response_code": code, "error_code": None, "retryable": False, "disable": False,
                            "signature": signature, "summary": _safe_delivery_summary(status_code=code, bytes_read=bytes_read)}
                if code == 410:
                    return {"success": False, "response_code": code, "error_code": "endpoint_gone", "retryable": False, "disable": True,
                            "signature": signature, "summary": _safe_delivery_summary(status_code=code, bytes_read=bytes_read, reason="endpoint_gone")}
                retryable = code == 429 or 500 <= code <= 599
                return {"success": False, "response_code": code, "error_code": f"webhook_http_{code}", "retryable": retryable, "disable": False,
                        "signature": signature, "summary": _safe_delivery_summary(status_code=code, bytes_read=bytes_read)}
    except httpx.TimeoutException:
        return {"success": False, "response_code": None, "error_code": "webhook_timeout", "retryable": True, "disable": False,
                "signature": signature, "summary": _safe_delivery_summary(status_code=None, reason="timeout")}
    except httpx.RequestError:
        return {"success": False, "response_code": None, "error_code": "webhook_network_error", "retryable": True, "disable": False,
                "signature": signature, "summary": _safe_delivery_summary(status_code=None, reason="network_error")}
    except httpx.HTTPError:
        return {"success": False, "response_code": None, "error_code": "webhook_protocol_error", "retryable": False, "disable": False,
                "signature": signature, "summary": _safe_delivery_summary(status_code=None, reason="protocol_error")}
