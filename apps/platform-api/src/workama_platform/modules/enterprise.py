from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field, model_validator

from workama_platform.core import (
    Actor,
    capability_allows,
    create_access_token,
    get_actor,
    hash_secret,
    json_dumps,
    new_id,
    pool,
)
from workama_platform.modules.audit_exports import append_audit_chain
from workama_platform.modules.jobs import request_cancellation, submit_operation


router = APIRouter(prefix="/api/v1", tags=["enterprise-identity"])

SERVICE_ACCOUNT_TOKEN_PREFIX = "sa-wama-"
HIGH_RISK_AUTH_STRENGTH = 2
DEFAULT_ORG_RETENTION_DAYS = 30
MAX_ORG_RETENTION_DAYS = 90
SERVICE_ACCOUNT_STATUSES = frozenset({"active", "revoked", "expired"})
DELETION_STATUSES = frozenset({"retention", "cancelled", "deleting", "deleted"})
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_.-]*:(?:[a-z][a-z0-9_.-]*|\*)$")
OWNER_TRANSFER_OPERATION_TYPE = "org.owner_transfer"
OWNER_TRANSFER_JOB_TYPE = "org.owner_transfer.resource_migration"
ORG_DELETION_OPERATION_TYPE = "org.deletion"
ORG_DELETION_JOB_TYPE = "org.deletion.execute"


class ServiceAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=100)
    owner_user_id: str | None = Field(default=None, min_length=1, max_length=100)
    purpose: str = Field(default="", max_length=500)
    expires_at: datetime | None = None
    network_policy: dict[str, Any] = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=lambda: ["platform:read"])
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class ServiceAccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    owner_user_id: str | None = Field(default=None, min_length=1, max_length=100)
    purpose: str | None = Field(default=None, max_length=500)
    expires_at: datetime | None = None
    network_policy: dict[str, Any] | None = None
    scopes: list[str] | None = None


class DeleteReason(BaseModel):
    reason: str = Field(default="", max_length=500)


class CredentialRotationRequest(BaseModel):
    reason: str = Field(default="", max_length=500)
    expires_at: datetime | None = None


class OwnerTransferRequest(BaseModel):
    target_user_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="", max_length=500)
    expires_in_seconds: int = Field(default=15 * 60, ge=60, le=24 * 3600)


class OwnerTransferConfirmRequest(BaseModel):
    confirmation_token: str = Field(min_length=20, max_length=512)

    @model_validator(mode="before")
    @classmethod
    def accept_token_alias(cls, value: Any) -> Any:
        if isinstance(value, dict) and "confirmation_token" not in value and "token" in value:
            value = dict(value)
            value["confirmation_token"] = value["token"]
        return value


class OwnerTransferCancelRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class OrganizationDeletionRequest(BaseModel):
    reason: str = Field(default="", max_length=500)
    retention_days: int = Field(
        default=DEFAULT_ORG_RETENTION_DAYS,
        ge=1,
        le=MAX_ORG_RETENTION_DAYS,
    )


class OrganizationDeletionCancelRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


# This is intentionally additive so an existing development volume can be
# upgraded without requiring main.py to know about this module yet.
SCHEMA_STATEMENTS = (
    "ALTER TABLE id_org ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE id_org ADD COLUMN IF NOT EXISTS deletion_requested_at TIMESTAMPTZ",
    "ALTER TABLE id_org ADD COLUMN IF NOT EXISTS deletion_scheduled_at TIMESTAMPTZ",
    "ALTER TABLE id_org ADD COLUMN IF NOT EXISTS deletion_cancelled_at TIMESTAMPTZ",
    "ALTER TABLE id_member ALTER COLUMN workspace_id DROP NOT NULL",
    "ALTER TABLE id_member DROP CONSTRAINT IF EXISTS id_member_workspace_id_user_id_key",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_id_member_org_workspace_user
    ON id_member(org_id, workspace_id, user_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS id_service_account (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        owner_user_id TEXT NOT NULL REFERENCES id_user(id),
        purpose TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'revoked', 'expired')),
        expires_at TIMESTAMPTZ,
        network_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
        scopes TEXT[] NOT NULL DEFAULT ARRAY['platform:read']::text[],
        active_credential_version INTEGER NOT NULL DEFAULT 1 CHECK (active_credential_version > 0),
        last_used_at TIMESTAMPTZ,
        created_by TEXT NOT NULL REFERENCES id_user(id),
        create_idempotency_key TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(workspace_id, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_service_account_org_status ON id_service_account(org_id, status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_id_service_account_workspace_status ON id_service_account(workspace_id, status, updated_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_service_account_idempotency ON id_service_account(workspace_id, create_idempotency_key) WHERE create_idempotency_key IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS id_service_account_credential (
        id TEXT PRIMARY KEY,
        service_account_id TEXT NOT NULL REFERENCES id_service_account(id) ON DELETE CASCADE,
        version INTEGER NOT NULL CHECK (version > 0),
        token_hash TEXT NOT NULL UNIQUE,
        last_four TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'rotated', 'revoked')),
        created_by TEXT NOT NULL REFERENCES id_user(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        revoked_at TIMESTAMPTZ,
        revoke_reason TEXT,
        UNIQUE(service_account_id, version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_service_account_credential_active ON id_service_account_credential(service_account_id, status, version DESC)",
    """
    CREATE TABLE IF NOT EXISTS id_org_owner_transfer (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
        from_owner_user_id TEXT NOT NULL REFERENCES id_user(id),
        to_owner_user_id TEXT NOT NULL REFERENCES id_user(id),
        initiated_by TEXT NOT NULL REFERENCES id_user(id),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'confirmed', 'cancelled', 'expired')),
        confirmation_token_hash TEXT UNIQUE,
        action_hash TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        confirmed_by TEXT REFERENCES id_user(id),
        confirmed_at TIMESTAMPTZ,
        cancelled_by TEXT REFERENCES id_user(id),
        cancelled_at TIMESTAMPTZ
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_org_owner_transfer_pending ON id_org_owner_transfer(org_id) WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS idx_id_org_owner_transfer_org_time ON id_org_owner_transfer(org_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS id_org_owner_transfer_fact (
        id TEXT PRIMARY KEY,
        transfer_id TEXT NOT NULL REFERENCES id_org_owner_transfer(id) ON DELETE CASCADE,
        org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
        step SMALLINT NOT NULL CHECK (step IN (1, 2)),
        fact_type TEXT NOT NULL CHECK (fact_type IN ('initiated', 'confirmed')),
        actor_user_id TEXT NOT NULL REFERENCES id_user(id),
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(transfer_id, step)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_org_owner_transfer_fact_org_time ON id_org_owner_transfer_fact(org_id, occurred_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS id_org_deletion_request (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
        requested_by TEXT NOT NULL REFERENCES id_user(id),
        status TEXT NOT NULL DEFAULT 'retention'
            CHECK (status IN ('retention', 'cancelled', 'deleting', 'deleted')),
        reason TEXT NOT NULL DEFAULT '',
        retention_until TIMESTAMPTZ NOT NULL,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        cancelled_by TEXT REFERENCES id_user(id),
        cancelled_at TIMESTAMPTZ,
        cancel_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_org_deletion_request_active ON id_org_deletion_request(org_id) WHERE status IN ('retention', 'deleting')",
    "CREATE INDEX IF NOT EXISTS idx_id_org_deletion_request_org_time ON id_org_deletion_request(org_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS id_enterprise_audit_event (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
        actor_user_id TEXT REFERENCES id_user(id) ON DELETE SET NULL,
        action TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_enterprise_audit_org_time ON id_enterprise_audit_event(org_id, occurred_at DESC)",
)


async def ensure_enterprise_schema(conn) -> None:
    """Apply the additive enterprise identity schema to one connection."""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _not_found(resource: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{resource} not found")


def _same_tenant(actor_org_id: str, resource_org_id: str) -> bool:
    return secrets.compare_digest(str(actor_org_id), str(resource_org_id))


def _require_user(actor: Actor) -> None:
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail="A human user actor is required")


def _require_high_assurance(actor: Actor) -> None:
    if actor.auth_strength < HIGH_RISK_AUTH_STRENGTH:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "AUTH_STRENGTH_REQUIRED",
                "required_auth_strength": HIGH_RISK_AUTH_STRENGTH,
                "step_up_required": True,
            },
        )


def capability_granted(actor: Actor, required: str) -> bool:
    """Return whether the actor has this capability or its existing alias.

    Older role snapshots do not yet contain the P1 service-account names. The
    existing api_key/workspace grants are the compatibility gate until the
    capability registry is extended by the owning auth module.
    """
    if capability_allows(actor.capabilities, required):
        return True
    aliases = {
        "service_account:read": ("api_key:read", "api_key:*", "workspace:*"),
        "service_account:create": ("api_key:*", "workspace:*"),
        "service_account:write": ("api_key:*", "workspace:*"),
        "service_account:delete": ("api_key:*", "workspace:*"),
        "service_account:credential": ("api_key:*", "workspace:*"),
        "org:read": ("workspace:read", "workspace:*"),
    }.get(required, ())
    return any(capability_allows(actor.capabilities, alias) for alias in aliases)


def _require_capability(actor: Actor, required: str) -> None:
    if not capability_granted(actor, required):
        raise HTTPException(status_code=403, detail=f"Missing capability: {required}")


def _require_org_status(org: dict[str, Any], *, allow_deletion_request: bool = False) -> None:
    org_status = org.get("status") or "active"
    if org_status != "active" and not allow_deletion_request:
        raise HTTPException(status_code=409, detail=f"Organization is not active: {org_status}")


def _validate_expiry(expires_at: datetime | None) -> datetime | None:
    normalized = _utc(expires_at)
    if normalized is not None and normalized <= _now():
        raise HTTPException(status_code=400, detail="Expiration must be in the future")
    return normalized


def _clean_required_text(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail=f"{field_name} must not be blank")
    return cleaned


def normalize_service_account_scopes(scopes: list[str]) -> list[str]:
    normalized = sorted({scope.strip() for scope in scopes if scope and scope.strip()})
    if not normalized:
        raise HTTPException(status_code=422, detail="At least one service-account scope is required")
    invalid = [scope for scope in normalized if not _SCOPE_RE.fullmatch(scope)]
    if invalid:
        raise HTTPException(status_code=422, detail="Service-account scopes have an invalid format")
    forbidden = {"org:delete", "org:owner_transfer", "service_account:credential"}
    if forbidden.intersection(normalized):
        raise HTTPException(status_code=422, detail="High-risk organization capabilities cannot be delegated")
    return normalized


def service_account_token_hash(token: str) -> str:
    if not token or not token.startswith(SERVICE_ACCOUNT_TOKEN_PREFIX):
        raise ValueError("Invalid service-account token format")
    return hash_secret(token)


def generate_service_account_token() -> tuple[str, str]:
    token = SERVICE_ACCOUNT_TOKEN_PREFIX + secrets.token_urlsafe(48)
    return token, service_account_token_hash(token)


def _action_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


async def _get_org(conn, actor: Actor, org_id: str, *, owner_required: bool = False) -> dict[str, Any]:
    _require_user(actor)
    if not _same_tenant(actor.org_id, org_id):
        raise _not_found("Organization")
    result = await conn.execute(
        """
        SELECT id, name, owner_user_id, status, deletion_requested_at,
               deletion_scheduled_at, deletion_cancelled_at, created_at
        FROM id_org
        WHERE id=%s
        """,
        (org_id,),
    )
    org = await result.fetchone()
    if not org:
        raise _not_found("Organization")
    if owner_required and org["owner_user_id"] != actor.user_id:
        raise HTTPException(status_code=403, detail="Organization owner role required")
    return org


async def _get_workspace(conn, actor: Actor, org_id: str, workspace_id: str) -> dict[str, Any]:
    result = await conn.execute(
        """
        SELECT w.id, w.org_id, w.name, w.slug, w.status
        FROM id_workspace w
        JOIN id_org o ON o.id=w.org_id
        WHERE w.id=%s AND w.org_id=%s AND w.status='active'
          AND (
            o.owner_user_id=%s
            OR EXISTS (
                SELECT 1 FROM id_member m
                WHERE m.org_id=w.org_id AND m.workspace_id=w.id AND m.user_id=%s
            )
          )
        """,
        (workspace_id, org_id, actor.user_id, actor.user_id),
    )
    workspace = await result.fetchone()
    if not workspace:
        raise _not_found("Workspace")
    return workspace


async def _get_active_org_user(conn, org_id: str, user_id: str) -> dict[str, Any]:
    result = await conn.execute(
        """
        SELECT u.id, u.email, u.display_name
        FROM id_user u
        JOIN id_org o ON o.id=%s
        WHERE u.id=%s AND u.status='active'
          AND (
            o.owner_user_id=u.id
            OR EXISTS (
                SELECT 1 FROM id_member m
                WHERE m.org_id=%s AND m.user_id=u.id
            )
          )
        """,
        (org_id, user_id, org_id),
    )
    user = await result.fetchone()
    if not user:
        raise HTTPException(status_code=422, detail="Service-account owner must be an active organization member")
    return user


async def _audit(
    conn,
    *,
    org_id: str,
    actor_user_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str,
    reason: str = "",
    details: dict[str, Any] | None = None,
    workspace_id: str | None = None,
) -> None:
    event_id = new_id("eau")
    await conn.execute(
        """
        INSERT INTO id_enterprise_audit_event(
            id, org_id, actor_user_id, action, resource_type, resource_id, reason, details
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            event_id,
            org_id,
            actor_user_id,
            action,
            resource_type,
            resource_id,
            reason,
            json_dumps(details or {}),
        ),
    )
    await append_audit_chain(
        conn,
        event_id=event_id,
        org_id=org_id,
        workspace_id=workspace_id or (details or {}).get("workspace_id"),
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
    )


async def _service_account_row(
    conn,
    actor: Actor,
    service_account_id: str,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    lock = " FOR UPDATE OF sa" if for_update else ""
    result = await conn.execute(
        f"""
        SELECT sa.id, sa.org_id, sa.workspace_id, sa.name, sa.owner_user_id,
               sa.purpose, sa.status, sa.expires_at, sa.network_policy, sa.scopes,
               sa.active_credential_version, sa.last_used_at, sa.created_by,
               sa.create_idempotency_key, sa.created_at, sa.updated_at,
               c.version AS credential_version, c.last_four,
               CASE WHEN sa.status='active' AND sa.expires_at IS NOT NULL
                         AND sa.expires_at<=now() THEN 'expired'
                    ELSE sa.status END AS effective_status
        FROM id_service_account sa
        LEFT JOIN id_service_account_credential c
          ON c.service_account_id=sa.id
         AND c.version=sa.active_credential_version
         AND c.status='active'
        WHERE sa.id=%s AND sa.org_id=%s
        {lock}
        """,
        (service_account_id, actor.org_id),
    )
    row = await result.fetchone()
    if not row:
        raise _not_found("Service account")
    return row


def _service_account_payload(row: dict[str, Any]) -> dict[str, Any]:
    credential_version = row.get("credential_version")
    return {
        "id": row["id"],
        "org_id": row["org_id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "owner_user_id": row["owner_user_id"],
        "purpose": row["purpose"],
        "status": row.get("effective_status") or row.get("status"),
        "expires_at": row.get("expires_at"),
        "network_policy": row.get("network_policy") or {},
        "scopes": list(row.get("scopes") or []),
        "credential": {
            "configured": credential_version is not None,
            "last4": row.get("last_four"),
            "version": credential_version,
        },
        "last_used_at": row.get("last_used_at"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "version": row.get("active_credential_version") or 0,
    }


def _with_secret(payload: dict[str, Any], token: str, version: int, last_four: str) -> dict[str, Any]:
    result = dict(payload)
    result["credential"] = {
        "configured": True,
        "last4": last_four,
        "version": version,
        "token": token,
    }
    # Keep the one-time secret at the top level for CLI/native clients. It is
    # never selected from the database and is absent from all later responses.
    result["token"] = token
    return result


async def authenticate_service_account_token(token: str) -> dict[str, Any] | None:
    """Resolve a service-account token without ever returning its plaintext.

    The main auth dependency is intentionally not changed in this slice. This
    helper is the narrow hand-off for the future service-account Actor adapter.
    """
    try:
        token_hash = service_account_token_hash(token)
    except ValueError:
        return None
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT sa.id AS service_account_id, sa.org_id, sa.workspace_id,
                   sa.owner_user_id, sa.scopes, sa.network_policy,
                   sa.expires_at, sa.status, c.version
            FROM id_service_account_credential c
            JOIN id_service_account sa ON sa.id=c.service_account_id
            JOIN id_org o ON o.id=sa.org_id
            WHERE c.token_hash=%s AND c.status='active' AND sa.status='active'
              AND o.status='active'
              AND (sa.expires_at IS NULL OR sa.expires_at>now())
            """,
            (token_hash,),
        )
        row = await result.fetchone()
        if not row:
            return None
        await conn.execute(
            "UPDATE id_service_account SET last_used_at=now(), updated_at=now() WHERE id=%s",
            (row["service_account_id"],),
        )
        await conn.commit()
    return {
        "actor_type": "service_account",
        "service_account_id": row["service_account_id"],
        "org_id": row["org_id"],
        "workspace_id": row["workspace_id"],
        "owner_user_id": row["owner_user_id"],
        "scopes": list(row.get("scopes") or []),
        "network_policy": row.get("network_policy") or {},
        "credential_version": row["version"],
    }


@router.get("/orgs/{org_id}")
async def get_organization(
    org_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_capability(actor, "org:read")
    async with pool.connection() as conn:
        org = await _get_org(conn, actor, org_id)
        deletion_result = await conn.execute(
            """
            SELECT id, status, reason, retention_until, requested_at,
                   cancelled_at, updated_at
            FROM id_org_deletion_request
            WHERE org_id=%s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (org_id,),
        )
        deletion = await deletion_result.fetchone()
    return {
        "id": org["id"],
        "name": org["name"],
        "owner_user_id": org["owner_user_id"],
        "status": org.get("status") or "active",
        "deletion": deletion,
        "created_at": org.get("created_at"),
    }


@router.get("/service-accounts")
async def list_service_accounts(
    actor: Annotated[Actor, Depends(get_actor)],
    workspace_id: str | None = None,
    state: Literal["active", "revoked", "expired"] | None = None,
):
    _require_user(actor)
    _require_capability(actor, "service_account:read")
    async with pool.connection() as conn:
        org = await _get_org(conn, actor, actor.org_id)
        filters = ["sa.org_id=%s"]
        params: list[Any] = [actor.org_id]
        if workspace_id:
            await _get_workspace(conn, actor, actor.org_id, workspace_id)
            filters.append("sa.workspace_id=%s")
            params.append(workspace_id)
        if state:
            if state == "expired":
                filters.append("(sa.status='expired' OR (sa.status='active' AND sa.expires_at<=now()))")
            else:
                filters.append("sa.status=%s")
                params.append(state)
        result = await conn.execute(
            f"""
            SELECT sa.id, sa.org_id, sa.workspace_id, sa.name, sa.owner_user_id,
                   sa.purpose, sa.status, sa.expires_at, sa.network_policy, sa.scopes,
                   sa.active_credential_version, sa.last_used_at, sa.created_by,
                   sa.created_at, sa.updated_at, c.version AS credential_version,
                   c.last_four,
                   CASE WHEN sa.status='active' AND sa.expires_at IS NOT NULL
                             AND sa.expires_at<=now() THEN 'expired'
                        ELSE sa.status END AS effective_status
            FROM id_service_account sa
            LEFT JOIN id_service_account_credential c
              ON c.service_account_id=sa.id
             AND c.version=sa.active_credential_version AND c.status='active'
            WHERE {' AND '.join(filters)}
            ORDER BY sa.created_at DESC, sa.id DESC
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    # Contract《720》listServiceAccounts: ListQuery -> ListResponse<ServiceAccountDTO>
    # 保留 items 字段向后兼容
    data = [_service_account_payload(row) for row in rows]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.post("/service-accounts", status_code=201)
async def create_service_account(
    body: ServiceAccountCreate,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _require_user(actor)
    _require_capability(actor, "service_account:create")
    name = _clean_required_text(body.name, "name")
    workspace_id = body.workspace_id or actor.workspace_id
    owner_user_id = body.owner_user_id or actor.user_id
    expires_at = _validate_expiry(body.expires_at)
    scopes = normalize_service_account_scopes(body.scopes)
    request_key = idempotency_header or body.idempotency_key
    async with pool.connection() as conn:
        org = await _get_org(conn, actor, actor.org_id)
        _require_org_status(org)
        await _get_workspace(conn, actor, actor.org_id, workspace_id)
        await _get_active_org_user(conn, actor.org_id, owner_user_id)
        async with conn.transaction():
            if request_key:
                existing_result = await conn.execute(
                    """
                    SELECT sa.id, sa.org_id, sa.workspace_id, sa.name, sa.owner_user_id, sa.purpose,
                           sa.status, sa.expires_at, sa.network_policy, sa.scopes,
                           sa.active_credential_version, sa.last_used_at, sa.created_by,
                           sa.created_at, sa.updated_at, c.version AS credential_version,
                           c.last_four,
                           CASE WHEN sa.status='active' AND sa.expires_at IS NOT NULL
                                     AND sa.expires_at<=now() THEN 'expired'
                                ELSE sa.status END AS effective_status
                    FROM id_service_account sa
                    LEFT JOIN id_service_account_credential c
                      ON c.service_account_id=sa.id
                     AND c.version=sa.active_credential_version AND c.status='active'
                    WHERE sa.workspace_id=%s AND sa.create_idempotency_key=%s
                    """,
                    (workspace_id, request_key),
                )
                existing = await existing_result.fetchone()
                if existing:
                    if (
                        existing["name"] != name
                        or existing["owner_user_id"] != owner_user_id
                        or existing["scopes"] != scopes
                    ):
                        raise HTTPException(status_code=409, detail="Idempotency key was used for different service-account input")
                    payload = _service_account_payload(existing)
                    payload["idempotent_replay"] = True
                    return payload
            duplicate_result = await conn.execute(
                "SELECT id FROM id_service_account WHERE workspace_id=%s AND name=%s",
                (workspace_id, name),
            )
            if await duplicate_result.fetchone():
                raise HTTPException(status_code=409, detail="A service account with this name already exists")
            service_account_id = new_id("sac")
            token, token_hash = generate_service_account_token()
            await conn.execute(
                """
                INSERT INTO id_service_account(
                    id, org_id, workspace_id, name, owner_user_id, purpose,
                    status, expires_at, network_policy, scopes,
                    active_credential_version, created_by, create_idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, %s::jsonb, %s, 1, %s, %s)
                """,
                (
                    service_account_id,
                    actor.org_id,
                    workspace_id,
                    name,
                    owner_user_id,
                    body.purpose.strip(),
                    expires_at,
                    json_dumps(body.network_policy),
                    scopes,
                    actor.user_id,
                    request_key,
                ),
            )
            await conn.execute(
                """
                INSERT INTO id_service_account_credential(
                    id, service_account_id, version, token_hash, last_four,
                    status, created_by
                ) VALUES (%s, %s, 1, %s, %s, 'active', %s)
                """,
                (new_id("sacred"), service_account_id, token_hash, token[-4:], actor.user_id),
            )
            await _audit(
                conn,
                org_id=actor.org_id,
                actor_user_id=actor.user_id,
                action="service_account.created",
                resource_type="service_account",
                resource_id=service_account_id,
                details={"workspace_id": workspace_id, "owner_user_id": owner_user_id, "scopes": scopes},
                workspace_id=workspace_id,
            )
            row = await _service_account_row(conn, actor, service_account_id)
    return _with_secret(_service_account_payload(row), token, 1, token[-4:])


@router.get("/service-accounts/{service_account_id}")
async def get_service_account(
    service_account_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    _require_capability(actor, "service_account:read")
    async with pool.connection() as conn:
        row = await _service_account_row(conn, actor, service_account_id)
    return _service_account_payload(row)


@router.patch("/service-accounts/{service_account_id}")
async def update_service_account(
    service_account_id: str,
    body: ServiceAccountPatch,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    _require_capability(actor, "service_account:write")
    async with pool.connection() as conn:
        org = await _get_org(conn, actor, actor.org_id)
        _require_org_status(org)
        async with conn.transaction():
            current = await _service_account_row(conn, actor, service_account_id, for_update=True)
            if current["status"] != "active" or current.get("effective_status") == "expired":
                raise HTTPException(status_code=409, detail="Only an active service account can be updated")
            fields: list[str] = []
            values: list[Any] = []
            changed = body.model_fields_set
            if "name" in changed:
                fields.append("name=%s")
                name = _clean_required_text(body.name or "", "name")
                duplicate_result = await conn.execute(
                    "SELECT id FROM id_service_account WHERE workspace_id=%s AND name=%s AND id<>%s",
                    (current["workspace_id"], name, service_account_id),
                )
                if await duplicate_result.fetchone():
                    raise HTTPException(status_code=409, detail="A service account with this name already exists")
                values.append(name)
            if "purpose" in changed:
                fields.append("purpose=%s")
                values.append((body.purpose or "").strip())
            if "expires_at" in changed:
                fields.append("expires_at=%s")
                values.append(_validate_expiry(body.expires_at))
            if "network_policy" in changed:
                fields.append("network_policy=%s::jsonb")
                values.append(json_dumps(body.network_policy or {}))
            if "scopes" in changed:
                fields.append("scopes=%s")
                values.append(normalize_service_account_scopes(body.scopes or []))
            if "owner_user_id" in changed:
                owner_user_id = body.owner_user_id or ""
                await _get_active_org_user(conn, actor.org_id, owner_user_id)
                fields.append("owner_user_id=%s")
                values.append(owner_user_id)
            if fields:
                values.append(service_account_id)
                await conn.execute(
                    f"UPDATE id_service_account SET {', '.join(fields)}, updated_at=now() WHERE id=%s",
                    tuple(values),
                )
                await _audit(
                    conn,
                    org_id=actor.org_id,
                    actor_user_id=actor.user_id,
                    action="service_account.updated",
                    resource_type="service_account",
                    resource_id=service_account_id,
                    workspace_id=actor.workspace_id,
                )
            row = await _service_account_row(conn, actor, service_account_id)
    return _service_account_payload(row)


@router.delete("/service-accounts/{service_account_id}", status_code=204)
async def revoke_service_account(
    service_account_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    body: DeleteReason | None = Body(default=None),
):
    _require_user(actor)
    _require_capability(actor, "service_account:delete")
    _require_high_assurance(actor)
    reason = (body.reason if body else "").strip()
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _service_account_row(conn, actor, service_account_id, for_update=True)
            if row["status"] == "revoked":
                return Response(status_code=204)
            await conn.execute(
                "UPDATE id_service_account SET status='revoked', updated_at=now() WHERE id=%s",
                (service_account_id,),
            )
            await conn.execute(
                """
                UPDATE id_service_account_credential
                SET status='revoked', revoked_at=now(), revoke_reason=%s
                WHERE service_account_id=%s AND status='active'
                """,
                (reason, service_account_id),
            )
            await _audit(
                conn,
                org_id=actor.org_id,
                actor_user_id=actor.user_id,
                action="service_account.revoked",
                resource_type="service_account",
                resource_id=service_account_id,
                reason=reason,
                workspace_id=actor.workspace_id,
            )
    return Response(status_code=204)


@router.post("/service-accounts/{service_account_id}/credential-rotations")
async def rotate_service_account_credential(
    service_account_id: str,
    body: CredentialRotationRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    _require_capability(actor, "service_account:credential")
    _require_high_assurance(actor)
    new_expiry = _validate_expiry(body.expires_at)
    async with pool.connection() as conn:
        org = await _get_org(conn, actor, actor.org_id)
        _require_org_status(org)
        async with conn.transaction():
            row = await _service_account_row(conn, actor, service_account_id, for_update=True)
            if row["status"] != "active" or row.get("effective_status") == "expired":
                raise HTTPException(status_code=409, detail="Only an active service account can rotate credentials")
            version = int(row["active_credential_version"] or 0) + 1
            token, token_hash = generate_service_account_token()
            await conn.execute(
                """
                UPDATE id_service_account_credential
                SET status='rotated', revoked_at=now(), revoke_reason=%s
                WHERE service_account_id=%s AND status='active'
                """,
                (body.reason.strip() or "credential_rotated", service_account_id),
            )
            await conn.execute(
                """
                INSERT INTO id_service_account_credential(
                    id, service_account_id, version, token_hash, last_four,
                    status, created_by
                ) VALUES (%s, %s, %s, %s, %s, 'active', %s)
                """,
                (new_id("sacred"), service_account_id, version, token_hash, token[-4:], actor.user_id),
            )
            if new_expiry is not None:
                await conn.execute(
                    "UPDATE id_service_account SET active_credential_version=%s, expires_at=%s, updated_at=now() WHERE id=%s",
                    (version, new_expiry, service_account_id),
                )
            else:
                await conn.execute(
                    "UPDATE id_service_account SET active_credential_version=%s, updated_at=now() WHERE id=%s",
                    (version, service_account_id),
                )
            await _audit(
                conn,
                org_id=actor.org_id,
                actor_user_id=actor.user_id,
                action="service_account.credential_rotated",
                resource_type="service_account",
                resource_id=service_account_id,
                reason=body.reason.strip(),
                details={"version": version},
                workspace_id=actor.workspace_id,
            )
            updated = await _service_account_row(conn, actor, service_account_id)
    return _with_secret(_service_account_payload(updated), token, version, token[-4:])


async def _enqueue_owner_transfer_propagation(
    conn,
    *,
    org_id: str,
    workspace_id: str,
    transfer_id: str,
    from_owner_user_id: str,
    to_owner_user_id: str,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    """Create the async operation/job that migrates or tombstones the old owner resources."""
    return await submit_operation(
        conn,
        operation_type=OWNER_TRANSFER_OPERATION_TYPE,
        workspace_id=workspace_id,
        org_id=org_id,
        actor_id=actor_id,
        actor_role=actor_role,
        idempotency_key=f"owner-transfer:{transfer_id}",
        payload={
            "transfer_id": transfer_id,
            "from_owner_user_id": from_owner_user_id,
            "to_owner_user_id": to_owner_user_id,
            "org_id": org_id,
        },
        job_type=OWNER_TRANSFER_JOB_TYPE,
        queue="platform",
        max_attempts=3,
        priority=200,
        cancellable=True,
    )


async def _enqueue_org_deletion(
    conn,
    *,
    org_id: str,
    workspace_id: str,
    request_id: str,
    retention_until: datetime,
    actor_id: str,
    actor_role: str,
) -> dict[str, Any]:
    """Create the delayed async operation/job that executes organization deletion after retention."""
    return await submit_operation(
        conn,
        operation_type=ORG_DELETION_OPERATION_TYPE,
        workspace_id=workspace_id,
        org_id=org_id,
        actor_id=actor_id,
        actor_role=actor_role,
        idempotency_key=f"org-deletion:{request_id}",
        payload={
            "request_id": request_id,
            "org_id": org_id,
        },
        job_type=ORG_DELETION_JOB_TYPE,
        queue="platform",
        max_attempts=3,
        priority=200,
        cancellable=True,
        scheduled_at=retention_until,
    )


async def _owner_transfer_row(conn, actor: Actor, org_id: str, transfer_id: str, *, for_update: bool = False):
    lock = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        f"""
        SELECT t.id, t.org_id, t.from_owner_user_id, t.to_owner_user_id,
               t.initiated_by, t.status, t.action_hash, t.reason, t.expires_at,
               t.created_at, t.confirmed_by, t.confirmed_at,
               t.cancelled_by, t.cancelled_at
        FROM id_org_owner_transfer t
        WHERE t.id=%s AND t.org_id=%s
        {lock}
        """,
        (transfer_id, org_id),
    )
    row = await result.fetchone()
    if not row:
        raise _not_found("Owner transfer")
    return row


def _owner_transfer_payload(row: dict[str, Any], *, confirmation_token: str | None = None) -> dict[str, Any]:
    payload = {
        "id": row["id"],
        "org_id": row["org_id"],
        "from_owner_user_id": row["from_owner_user_id"],
        "to_owner_user_id": row["to_owner_user_id"],
        "initiated_by": row["initiated_by"],
        "status": row["status"],
        "reason": row["reason"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "confirmed_by": row.get("confirmed_by"),
        "confirmed_at": row.get("confirmed_at"),
        "cancelled_by": row.get("cancelled_by"),
        "cancelled_at": row.get("cancelled_at"),
    }
    if confirmation_token is not None:
        payload["confirmation_token"] = confirmation_token
    return payload


@router.post("/orgs/{org_id}/owner-transfers", status_code=202)
async def initiate_owner_transfer(
    org_id: str,
    body: OwnerTransferRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    _require_capability(actor, "org:owner_transfer")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        org = await _get_org(conn, actor, org_id, owner_required=True)
        _require_org_status(org)
        if body.target_user_id == actor.user_id:
            raise HTTPException(status_code=422, detail="The organization owner cannot be transferred to the current owner")
        await _get_active_org_user(conn, org_id, body.target_user_id)
        async with conn.transaction():
            pending_result = await conn.execute(
                "SELECT id FROM id_org_owner_transfer WHERE org_id=%s AND status='pending' FOR UPDATE",
                (org_id,),
            )
            if await pending_result.fetchone():
                raise HTTPException(status_code=409, detail="An owner transfer is already pending")
            transfer_id = new_id("otr")
            confirmation_token, confirmation_hash = generate_service_account_token()
            expires_at = _now() + timedelta(seconds=body.expires_in_seconds)
            action_hash = _action_hash(
                {
                    "org_id": org_id,
                    "from_owner_user_id": actor.user_id,
                    "to_owner_user_id": body.target_user_id,
                    "reason": body.reason.strip(),
                }
            )
            await conn.execute(
                """
                INSERT INTO id_org_owner_transfer(
                    id, org_id, from_owner_user_id, to_owner_user_id, initiated_by,
                    status, confirmation_token_hash, action_hash, reason, expires_at
                ) VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s)
                """,
                (
                    transfer_id,
                    org_id,
                    actor.user_id,
                    body.target_user_id,
                    actor.user_id,
                    confirmation_hash,
                    action_hash,
                    body.reason.strip(),
                    expires_at,
                ),
            )
            await conn.execute(
                """
                INSERT INTO id_org_owner_transfer_fact(
                    id, transfer_id, org_id, step, fact_type, actor_user_id, evidence
                ) VALUES (%s, %s, %s, 1, 'initiated', %s, %s::jsonb)
                """,
                (
                    new_id("otf"),
                    transfer_id,
                    org_id,
                    actor.user_id,
                    json_dumps({"action_hash": action_hash, "target_user_id": body.target_user_id}),
                ),
            )
            await _audit(
                conn,
                org_id=org_id,
                actor_user_id=actor.user_id,
                action="organization.owner_transfer_initiated",
                resource_type="owner_transfer",
                resource_id=transfer_id,
                reason=body.reason.strip(),
                details={"from_owner_user_id": actor.user_id, "to_owner_user_id": body.target_user_id},
                workspace_id=actor.workspace_id,
            )
            result = await conn.execute(
                """
                SELECT id, org_id, from_owner_user_id, to_owner_user_id, initiated_by,
                       status, reason, expires_at, created_at, confirmed_by,
                       confirmed_at, cancelled_by, cancelled_at
                FROM id_org_owner_transfer WHERE id=%s
                """,
                (transfer_id,),
            )
            row = await result.fetchone()
    return _owner_transfer_payload(row, confirmation_token=confirmation_token)


@router.post("/orgs/{org_id}/owner-transfers/{transfer_id}/confirm")
async def confirm_owner_transfer(
    org_id: str,
    transfer_id: str,
    body: OwnerTransferConfirmRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            org = await _get_org(conn, actor, org_id)
            _require_org_status(org)
            transfer = await _owner_transfer_row(conn, actor, org_id, transfer_id, for_update=True)
            if transfer["status"] != "pending":
                raise HTTPException(status_code=409, detail="Owner transfer is no longer pending")
            if _utc(transfer["expires_at"]) <= _now():
                raise HTTPException(status_code=410, detail="Owner transfer confirmation has expired")
            if transfer["to_owner_user_id"] != actor.user_id:
                raise HTTPException(status_code=403, detail="Only the proposed owner can confirm this transfer")
            try:
                token_hash = service_account_token_hash(body.confirmation_token)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Owner transfer confirmation token is invalid") from exc
            token_result = await conn.execute(
                "SELECT confirmation_token_hash FROM id_org_owner_transfer WHERE id=%s FOR UPDATE",
                (transfer_id,),
            )
            token_row = await token_result.fetchone()
            if not token_row or not token_row["confirmation_token_hash"] or not secrets.compare_digest(
                token_row["confirmation_token_hash"], token_hash
            ):
                raise HTTPException(status_code=400, detail="Owner transfer confirmation token is invalid")
            owner_result = await conn.execute(
                "SELECT owner_user_id FROM id_org WHERE id=%s FOR UPDATE",
                (org_id,),
            )
            current_owner = await owner_result.fetchone()
            if not current_owner or current_owner["owner_user_id"] != transfer["from_owner_user_id"]:
                raise HTTPException(status_code=409, detail="Organization owner changed while transfer was pending")
            await conn.execute(
                """
                UPDATE id_member SET role='admin'
                WHERE org_id=%s AND user_id=%s AND role='owner'
                """,
                (org_id, transfer["from_owner_user_id"]),
            )
            existing_member_result = await conn.execute(
                """
                SELECT id FROM id_member
                WHERE org_id=%s AND workspace_id IS NULL AND user_id=%s
                FOR UPDATE
                """,
                (org_id, actor.user_id),
            )
            existing_member = await existing_member_result.fetchone()
            if existing_member:
                await conn.execute(
                    "UPDATE id_member SET role='owner', joined_at=COALESCE(joined_at, now()) WHERE id=%s",
                    (existing_member["id"],),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO id_member(id, org_id, workspace_id, user_id, role, joined_at)
                    VALUES (%s, %s, NULL, %s, 'owner', now())
                    """,
                    (new_id("mem"), org_id, actor.user_id),
                )
            await conn.execute(
                """
                UPDATE id_member SET role='admin'
                WHERE org_id=%s AND workspace_id=%s AND user_id=%s AND role='owner'
                """,
                (org_id, actor.workspace_id, transfer["from_owner_user_id"]),
            )
            await conn.execute(
                """
                UPDATE id_member SET role='owner', joined_at=COALESCE(joined_at, now())
                WHERE org_id=%s AND workspace_id=%s AND user_id=%s
                """,
                (org_id, actor.workspace_id, actor.user_id),
            )
            await conn.execute(
                """
                UPDATE id_org SET owner_user_id=%s WHERE id=%s
                """,
                (actor.user_id, org_id),
            )
            await conn.execute(
                """
                UPDATE id_org_owner_transfer
                SET status='confirmed', confirmed_by=%s, confirmed_at=now(), confirmation_token_hash=NULL
                WHERE id=%s
                """,
                (actor.user_id, transfer_id),
            )
            await conn.execute(
                """
                INSERT INTO id_org_owner_transfer_fact(
                    id, transfer_id, org_id, step, fact_type, actor_user_id, evidence
                ) VALUES (%s, %s, %s, 2, 'confirmed', %s, %s::jsonb)
                """,
                (
                    new_id("otf"),
                    transfer_id,
                    org_id,
                    actor.user_id,
                    json_dumps({"action_hash": transfer["action_hash"], "confirmed_by": actor.user_id}),
                ),
            )
            await _audit(
                conn,
                org_id=org_id,
                actor_user_id=actor.user_id,
                action="organization.owner_transfer_confirmed",
                resource_type="owner_transfer",
                resource_id=transfer_id,
                reason=transfer["reason"],
                details={"from_owner_user_id": transfer["from_owner_user_id"], "to_owner_user_id": actor.user_id},
                workspace_id=actor.workspace_id,
            )
            operation = await _enqueue_owner_transfer_propagation(
                conn,
                org_id=org_id,
                workspace_id=actor.workspace_id,
                transfer_id=transfer_id,
                from_owner_user_id=transfer["from_owner_user_id"],
                to_owner_user_id=actor.user_id,
                actor_id=actor.user_id,
                actor_role=actor.role,
            )
            org_result = await conn.execute(
                "SELECT id, name, owner_user_id, status, created_at FROM id_org WHERE id=%s",
                (org_id,),
            )
            updated_org = await org_result.fetchone()
    return {
        "organization": updated_org,
        "transfer_id": transfer_id,
        "status": "confirmed",
        "operation_id": operation["id"],
        "access_token": create_access_token(actor.user_id, actor.workspace_id, "owner", auth_strength=2),
    }


@router.post("/orgs/{org_id}/owner-transfers/{transfer_id}/cancel")
async def cancel_owner_transfer(
    org_id: str,
    transfer_id: str,
    body: OwnerTransferCancelRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    _require_capability(actor, "org:owner_transfer")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        org = await _get_org(conn, actor, org_id, owner_required=True)
        _require_org_status(org)
        async with conn.transaction():
            transfer = await _owner_transfer_row(conn, actor, org_id, transfer_id, for_update=True)
            if transfer["status"] != "pending":
                raise HTTPException(status_code=409, detail="Owner transfer is no longer pending")
            await conn.execute(
                "UPDATE id_org_owner_transfer SET status='cancelled', cancelled_by=%s, cancelled_at=now(), confirmation_token_hash=NULL WHERE id=%s",
                (actor.user_id, transfer_id),
            )
            await _audit(
                conn,
                org_id=org_id,
                actor_user_id=actor.user_id,
                action="organization.owner_transfer_cancelled",
                resource_type="owner_transfer",
                resource_id=transfer_id,
                reason=body.reason.strip(),
                workspace_id=actor.workspace_id,
            )
    return {"id": transfer_id, "status": "cancelled"}


@router.get("/orgs/{org_id}/owner-transfers")
async def list_owner_transfers(
    org_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    _require_capability(actor, "org:read")
    async with pool.connection() as conn:
        await _get_org(conn, actor, org_id)
        result = await conn.execute(
            """
            SELECT id, org_id, from_owner_user_id, to_owner_user_id, initiated_by,
                   status, reason, expires_at, created_at, confirmed_by,
                   confirmed_at, cancelled_by, cancelled_at
            FROM id_org_owner_transfer
            WHERE org_id=%s
            ORDER BY created_at DESC, id DESC
            """,
            (org_id,),
        )
        rows = await result.fetchall()
        facts_result = await conn.execute(
            """
            SELECT id, transfer_id, step, fact_type, actor_user_id, evidence, occurred_at
            FROM id_org_owner_transfer_fact
            WHERE org_id=%s
            ORDER BY occurred_at DESC, id DESC
            """,
            (org_id,),
        )
        facts = await facts_result.fetchall()
    # Contract《720》listOwnerTransfers: ListQuery -> ListResponse<OwnerTransferDTO>
    # 保留 items 与 facts 字段向后兼容
    data = [_owner_transfer_payload(row) for row in rows]
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
        "facts": facts,
    }


async def _request_org_deletion(
    org_id: str,
    body: OrganizationDeletionRequest,
    actor: Actor,
) -> dict[str, Any]:
    _require_user(actor)
    _require_capability(actor, "org:delete")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        org = await _get_org(conn, actor, org_id, owner_required=True)
        _require_org_status(org, allow_deletion_request=True)
        async with conn.transaction():
            existing_result = await conn.execute(
                """
                SELECT id, status, reason, retention_until, requested_at, cancelled_at, updated_at
                FROM id_org_deletion_request
                WHERE org_id=%s AND status IN ('retention', 'deleting')
                FOR UPDATE
                """,
                (org_id,),
            )
            existing = await existing_result.fetchone()
            if existing:
                operation_result = await conn.execute(
                    """
                    SELECT id FROM ops_async_operation
                    WHERE org_id=%s AND operation_type=%s AND idempotency_key=%s
                    """,
                    (org_id, ORG_DELETION_OPERATION_TYPE, f"org-deletion:{existing['id']}"),
                )
                operation_row = await operation_result.fetchone()
                return {
                    "operation_id": operation_row["id"] if operation_row else existing["id"],
                    "request_id": existing["id"],
                    "status": existing["status"],
                    "organization_status": "deletion_requested",
                    "retention_until": existing["retention_until"],
                    "idempotent_replay": True,
                }
            if (org.get("status") or "active") != "active":
                raise HTTPException(status_code=409, detail="Organization already has a deletion lifecycle state")
            retention_until = _now() + timedelta(days=body.retention_days)
            request_id = new_id("odr")
            await conn.execute(
                """
                INSERT INTO id_org_deletion_request(
                    id, org_id, requested_by, status, reason, retention_until
                ) VALUES (%s, %s, %s, 'retention', %s, %s)
                """,
                (request_id, org_id, actor.user_id, body.reason.strip(), retention_until),
            )
            await conn.execute(
                """
                UPDATE id_org
                SET status='deletion_requested', deletion_requested_at=now(),
                    deletion_scheduled_at=%s, deletion_cancelled_at=NULL
                WHERE id=%s
                """,
                (retention_until, org_id),
            )
            await _audit(
                conn,
                org_id=org_id,
                actor_user_id=actor.user_id,
                action="organization.deletion_requested",
                resource_type="organization",
                resource_id=org_id,
                reason=body.reason.strip(),
                details={"retention_until": retention_until.isoformat(), "retention_days": body.retention_days},
                workspace_id=actor.workspace_id,
            )
            operation = await _enqueue_org_deletion(
                conn,
                org_id=org_id,
                workspace_id=actor.workspace_id,
                request_id=request_id,
                retention_until=retention_until,
                actor_id=actor.user_id,
                actor_role=actor.role,
            )
    return {
        "operation_id": operation["id"],
        "request_id": request_id,
        "status": "retention",
        "organization_status": "deletion_requested",
        "retention_until": retention_until,
        "status_url": f"/api/v1/orgs/{org_id}/deletion-requests/{request_id}",
        "idempotent_replay": False,
    }


@router.post("/orgs/{org_id}/deletion-requests", status_code=202)
async def request_organization_deletion(
    org_id: str,
    body: OrganizationDeletionRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    return await _request_org_deletion(org_id, body, actor)


@router.delete("/orgs/{org_id}", status_code=202)
async def delete_organization(
    org_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    body: OrganizationDeletionRequest | None = Body(default=None),
):
    return await _request_org_deletion(org_id, body or OrganizationDeletionRequest(), actor)


@router.get("/orgs/{org_id}/deletion-requests")
async def list_organization_deletion_requests(
    org_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    _require_capability(actor, "org:read")
    async with pool.connection() as conn:
        await _get_org(conn, actor, org_id)
        result = await conn.execute(
            """
            SELECT id, org_id, requested_by, status, reason, retention_until,
                   requested_at, cancelled_by, cancelled_at, cancel_reason,
                   created_at, updated_at
            FROM id_org_deletion_request
            WHERE org_id=%s
            ORDER BY created_at DESC, id DESC
            """,
            (org_id,),
        )
        rows = await result.fetchall()
    # Contract《720》listOrgDeletionRequests: ListQuery -> ListResponse<OrgDeletionRequestDTO>
    # 保留 items 字段向后兼容
    data = list(rows)
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.get("/orgs/{org_id}/deletion-requests/{request_id}")
async def get_organization_deletion_request(
    org_id: str,
    request_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    _require_capability(actor, "org:read")
    async with pool.connection() as conn:
        await _get_org(conn, actor, org_id)
        result = await conn.execute(
            "SELECT * FROM id_org_deletion_request WHERE id=%s AND org_id=%s",
            (request_id, org_id),
        )
        row = await result.fetchone()
    if not row:
        raise _not_found("Organization deletion request")
    return row


@router.post("/orgs/{org_id}/deletion-requests/{request_id}/cancel")
async def cancel_organization_deletion(
    org_id: str,
    request_id: str,
    body: OrganizationDeletionCancelRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    _require_capability(actor, "org:delete")
    _require_high_assurance(actor)
    async with pool.connection() as conn:
        await _get_org(conn, actor, org_id, owner_required=True)
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM id_org_deletion_request WHERE id=%s AND org_id=%s FOR UPDATE",
                (request_id, org_id),
            )
            request = await result.fetchone()
            if not request:
                raise _not_found("Organization deletion request")
            if request["status"] != "retention":
                raise HTTPException(status_code=409, detail="Only a request in retention can be cancelled")
            if _utc(request["retention_until"]) <= _now():
                raise HTTPException(status_code=409, detail="The organization deletion retention window has elapsed")
            await conn.execute(
                """
                UPDATE id_org_deletion_request
                SET status='cancelled', cancelled_by=%s, cancelled_at=now(),
                    cancel_reason=%s, updated_at=now()
                WHERE id=%s
                """,
                (actor.user_id, body.reason.strip(), request_id),
            )
            await conn.execute(
                """
                UPDATE id_org
                SET status='active', deletion_requested_at=NULL,
                    deletion_scheduled_at=NULL, deletion_cancelled_at=now()
                WHERE id=%s
                """,
                (org_id,),
            )
            operation_result = await conn.execute(
                "SELECT id, workspace_id FROM ops_async_operation WHERE org_id=%s AND operation_type=%s AND idempotency_key=%s FOR UPDATE",
                (org_id, ORG_DELETION_OPERATION_TYPE, f"org-deletion:{request_id}"),
            )
            operation = await operation_result.fetchone()
            if operation:
                await request_cancellation(
                    conn,
                    operation_id=operation["id"],
                    workspace_id=operation["workspace_id"],
                    reason=body.reason.strip() or "organization deletion cancelled",
                )
            await _audit(
                conn,
                org_id=org_id,
                actor_user_id=actor.user_id,
                action="organization.deletion_cancelled",
                resource_type="organization_deletion_request",
                resource_id=request_id,
                reason=body.reason.strip(),
                workspace_id=actor.workspace_id,
                details={"async_operation_id": operation["id"] if operation else None},
            )
    return {
        "operation_id": operation["id"] if operation else request_id,
        "request_id": request_id,
        "status": "cancelled",
        "organization_status": "active",
    }
