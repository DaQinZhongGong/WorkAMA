from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field

from workama_platform.core import (
    Actor,
    create_access_token,
    get_actor,
    hash_secret,
    json_dumps,
    new_id,
    pool,
    settings,
)
from workama_platform.modules.jobs import ClaimedJob, submit_operation


router = APIRouter(prefix="/api/v1", tags=["workspaces"])

WORKSPACE_TOKEN_TTL_SECONDS = 300
INVITATION_ROLES = frozenset({"owner", "admin", "member", "viewer"})
ASSIGNABLE_INVITATION_ROLES = frozenset({"admin", "member", "viewer"})
MANAGEMENT_ROLES = frozenset({"owner", "admin"})
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str = Field(min_length=1, max_length=64)
    org_id: str | None = Field(default=None, min_length=1, max_length=80)
    settings: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class WorkspaceTokenRequest(BaseModel):
    workspace_token: str = Field(min_length=20, max_length=2048)


class InvitationCreateRequest(BaseModel):
    email: EmailStr
    role: Literal["owner", "admin", "member", "viewer"] = "member"
    expires_in_seconds: int = Field(default=7 * 24 * 3600, ge=60, le=30 * 24 * 3600)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class InvitationAcceptRequest(BaseModel):
    token: str = Field(min_length=20, max_length=512)


SCHEMA_STATEMENTS = (
    "ALTER TABLE id_workspace ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE id_workspace ADD COLUMN IF NOT EXISTS created_by TEXT REFERENCES id_user(id)",
    "ALTER TABLE id_workspace ADD COLUMN IF NOT EXISTS create_idempotency_key TEXT",
    "ALTER TABLE id_workspace ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()", 
    "ALTER TABLE id_member ALTER COLUMN workspace_id DROP NOT NULL",
    "ALTER TABLE id_member ADD COLUMN IF NOT EXISTS invited_by TEXT REFERENCES id_user(id)",
    "ALTER TABLE id_member ADD COLUMN IF NOT EXISTS joined_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE id_member DROP CONSTRAINT IF EXISTS id_member_workspace_id_user_id_key",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_member_org_workspace_user ON id_member(org_id, workspace_id, user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_workspace_create_idempotency ON id_workspace(org_id, create_idempotency_key) WHERE create_idempotency_key IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS id_invitation (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
        email_normalized TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
        token_hash TEXT NOT NULL UNIQUE,
        idempotency_key TEXT,
        invited_by TEXT NOT NULL REFERENCES id_user(id),
        expires_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'revoked', 'expired')),
        accepted_by TEXT REFERENCES id_user(id),
        accepted_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_invitation_workspace_status ON id_invitation(workspace_id, status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_id_invitation_email_status ON id_invitation(email_normalized, status, expires_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_invitation_active_target ON id_invitation(workspace_id, email_normalized) WHERE status = 'pending'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_id_invitation_idempotency ON id_invitation(workspace_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS id_workspace_audit (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
        workspace_id TEXT REFERENCES id_workspace(id) ON DELETE CASCADE,
        invitation_id TEXT REFERENCES id_invitation(id) ON DELETE SET NULL,
        actor_user_id TEXT REFERENCES id_user(id) ON DELETE SET NULL,
        action TEXT NOT NULL,
        request_id TEXT,
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_id_workspace_audit_tenant_time ON id_workspace_audit(org_id, workspace_id, occurred_at DESC)",
)


async def ensure_workspaces_schema(conn) -> None:
    """Apply the additive workspace/invitation schema to an existing database connection."""
    for statement in SCHEMA_STATEMENTS:
        await conn.execute(statement)


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_slug(value: str) -> str:
    slug = value.strip().lower()
    if not _SLUG_RE.fullmatch(slug):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Slug must contain only lowercase letters, numbers, and internal hyphens",
        )
    return slug


def same_tenant(actor_org_id: str, resource_org_id: str) -> bool:
    return secrets.compare_digest(str(actor_org_id), str(resource_org_id))


def can_manage_workspace(role: str) -> bool:
    return role in MANAGEMENT_ROLES


def can_invite_role(actor_role: str, invited_role: str) -> bool:
    if invited_role not in INVITATION_ROLES or invited_role not in ASSIGNABLE_INVITATION_ROLES:
        return False
    return actor_role in MANAGEMENT_ROLES


def invitation_is_active(
    invitation_status: str,
    expires_at: datetime,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return invitation_status == "pending" and expires_at > current


def issue_workspace_token(
    user_id: str,
    org_id: str,
    workspace_id: str,
    role: str,
    *,
    ttl_seconds: int = WORKSPACE_TOKEN_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    if role not in INVITATION_ROLES:
        raise ValueError("Unsupported workspace role")
    current = now or datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "org": org_id,
            "ws": workspace_id,
            "role": role,
            "jti": new_id("wctx"),
            "type": "workspace_context",
            "iat": int(current.timestamp()),
            "exp": int((current + timedelta(seconds=ttl_seconds)).timestamp()),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_workspace_token(token: str, *, now: datetime | None = None) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Workspace token is invalid or expired") from exc
    if payload.get("type") != "workspace_context":
        raise HTTPException(status_code=401, detail="Unexpected workspace token type")
    required = ("sub", "org", "ws", "role", "jti")
    if any(not payload.get(key) for key in required) or payload.get("role") not in INVITATION_ROLES:
        raise HTTPException(status_code=401, detail="Workspace token claims are invalid")
    if now is not None and datetime.fromtimestamp(int(payload["exp"]), UTC) <= now:
        raise HTTPException(status_code=401, detail="Workspace token is invalid or expired")
    return payload


def _require_user(actor: Actor) -> None:
    if actor.actor_type != "user":
        raise HTTPException(status_code=403, detail="User authentication is required")


def _require_manager(role: str) -> None:
    if not can_manage_workspace(role):
        raise HTTPException(status_code=403, detail="Owner or admin role required")


def _resource_not_found(resource: str = "Workspace") -> HTTPException:
    return HTTPException(status_code=404, detail=f"{resource} not found")


async def _workspace_access(conn, actor: Actor, workspace_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        """
        SELECT w.id, w.org_id, w.name, w.slug, w.settings, w.status,
               w.created_by, w.created_at,
               CASE WHEN o.owner_user_id = %s THEN 'owner'
                    ELSE COALESCE(wm.role, om.role) END AS role
        FROM id_workspace w
        JOIN id_org o ON o.id = w.org_id
        LEFT JOIN id_member wm
          ON wm.workspace_id = w.id AND wm.org_id = w.org_id AND wm.user_id = %s
        LEFT JOIN id_member om
          ON om.workspace_id IS NULL AND om.org_id = w.org_id AND om.user_id = %s
        WHERE w.id = %s AND w.org_id = %s AND w.status = 'active'
          AND (o.owner_user_id = %s OR wm.id IS NOT NULL OR om.id IS NOT NULL)
        """,
        (actor.user_id, actor.user_id, actor.user_id, workspace_id, actor.org_id, actor.user_id),
    )
    return await result.fetchone()


async def _write_audit(
    conn,
    *,
    org_id: str,
    action: str,
    actor_user_id: str | None = None,
    workspace_id: str | None = None,
    invitation_id: str | None = None,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO id_workspace_audit(
            id, org_id, workspace_id, invitation_id, actor_user_id,
            action, request_id, details
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            new_id("w audit").replace(" ", ""),
            org_id,
            workspace_id,
            invitation_id,
            actor_user_id,
            action,
            request_id,
            json_dumps(details or {}),
        ),
    )


def _workspace_response(row: dict[str, Any], *, idempotent_replay: bool = False) -> dict[str, Any]:
    return {
        "id": row["id"],
        "org_id": row["org_id"],
        "name": row["name"],
        "slug": row["slug"],
        "settings": row.get("settings") or {},
        "status": row.get("status", "active"),
        "role": row.get("role"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "idempotent_replay": idempotent_replay,
    }


@router.get("/orgs")
async def list_organizations(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT o.id, o.name, o.owner_user_id, o.created_at,
                   CASE WHEN o.owner_user_id = %s THEN 'owner' ELSE 'member' END AS role
            FROM id_org o
            WHERE o.owner_user_id = %s
               OR EXISTS (
                   SELECT 1 FROM id_member m
                   WHERE m.org_id = o.id AND m.user_id = %s
               )
            ORDER BY o.created_at, o.id
            """,
            (actor.user_id, actor.user_id, actor.user_id),
        )
        rows = await result.fetchall()
    # Contract《720》listOrganizations: ListQuery -> ListResponse<OrgDTO>
    return {
        "items": rows,
        "data": rows,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.get("/workspaces")
async def list_workspaces(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT w.id, w.org_id, w.name, w.slug, w.settings, w.status,
                   w.created_by, w.created_at,
                   CASE WHEN o.owner_user_id = %s THEN 'owner'
                        ELSE COALESCE(wm.role, om.role) END AS role
            FROM id_workspace w
            JOIN id_org o ON o.id = w.org_id
            LEFT JOIN id_member wm
              ON wm.workspace_id = w.id AND wm.org_id = w.org_id AND wm.user_id = %s
            LEFT JOIN id_member om
              ON om.workspace_id IS NULL AND om.org_id = w.org_id AND om.user_id = %s
            WHERE w.org_id = %s AND w.status = 'active'
              AND (o.owner_user_id = %s OR wm.id IS NOT NULL OR om.id IS NOT NULL)
            ORDER BY w.created_at, w.id
            """,
            (actor.user_id, actor.user_id, actor.user_id, actor.org_id, actor.user_id),
        )
        rows = await result.fetchall()
    data = [_workspace_response(row) for row in rows]
    # Contract《720》listWorkspaces: ListQuery -> ListResponse<WorkspaceDTO>
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.get("/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        row = await _workspace_access(conn, actor, workspace_id)
    if not row:
        raise _resource_not_found()
    return _workspace_response(row)


@router.post("/workspaces", status_code=201)
async def create_workspace(
    body: WorkspaceCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _require_user(actor)
    if body.org_id and not same_tenant(actor.org_id, body.org_id):
        raise _resource_not_found("Organization")
    slug = normalize_slug(body.slug)
    request_key = idempotency_header or body.idempotency_key
    async with pool.connection() as conn:
        current = await _workspace_access(conn, actor, actor.workspace_id)
        if not current:
            raise _resource_not_found()
        _require_manager(current["role"])
        async with conn.transaction():
            if request_key:
                existing_result = await conn.execute(
                    "SELECT id, org_id, name, slug, settings, status, created_by, created_at FROM id_workspace WHERE org_id=%s AND create_idempotency_key=%s",
                    (actor.org_id, request_key),
                )
                existing = await existing_result.fetchone()
                if existing:
                    if existing["name"] != body.name.strip() or existing["slug"] != slug:
                        raise HTTPException(status_code=409, detail="Idempotency key was used for a different workspace")
                    existing["role"] = current["role"] if current["role"] == "owner" else "admin"
                    return _workspace_response(existing, idempotent_replay=True)
            slug_result = await conn.execute(
                "SELECT id FROM id_workspace WHERE org_id=%s AND slug=%s AND status='active'",
                (actor.org_id, slug),
            )
            if await slug_result.fetchone():
                raise HTTPException(status_code=409, detail="A workspace with this slug already exists")
            workspace_id = new_id("wsp")
            name = body.name.strip()
            await conn.execute(
                """
                INSERT INTO id_workspace(
                    id, org_id, name, slug, settings, status, created_by, create_idempotency_key
                ) VALUES (%s, %s, %s, %s, %s::jsonb, 'active', %s, %s)
                """,
                (workspace_id, actor.org_id, name, slug, json_dumps(body.settings), actor.user_id, request_key),
            )
            await conn.execute(
                """
                INSERT INTO id_member(id, org_id, workspace_id, user_id, role, invited_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (new_id("mem"), actor.org_id, workspace_id, actor.user_id, current["role"], actor.user_id),
            )
            owner_result = await conn.execute(
                "SELECT owner_user_id FROM id_org WHERE id=%s",
                (actor.org_id,),
            )
            owner = await owner_result.fetchone()
            if owner and owner["owner_user_id"] != actor.user_id:
                await conn.execute(
                    """
                    INSERT INTO id_member(id, org_id, workspace_id, user_id, role)
                    VALUES (%s, %s, %s, %s, 'owner')
                    ON CONFLICT DO NOTHING
                    """,
                    (new_id("mem"), actor.org_id, workspace_id, owner["owner_user_id"]),
                )
            await conn.execute(
                "INSERT INTO bill_account(id, workspace_id, granted_balance) VALUES (%s, %s, 500)",
                (new_id("bacc"), workspace_id),
            )
            await conn.execute(
                """
                INSERT INTO bill_credit_grant(
                    id,workspace_id,source,period_start,expires_at,initial_amount,remaining_amount,idempotency_key
                ) VALUES (%s,%s,'initial',date_trunc('month',now()),date_trunc('month',now()) + interval '1 month',500,500,%s)
                ON CONFLICT(workspace_id,idempotency_key) DO NOTHING
                """,
                (new_id("grant"), workspace_id, f"initial:{workspace_id}"),
            )
            await conn.execute(
                """
                INSERT INTO gw_channel(id, workspace_id, name, provider, base_url, models, last_health)
                VALUES (%s, %s, 'WorkAMA Local', 'mock', 'mock://local', ARRAY['workama-chat', 'workama-embed'], 'healthy')
                """,
                (new_id("chn"), workspace_id),
            )
            await conn.execute(
                """
                INSERT INTO gw_model_price(workspace_id, model, input_per_million, output_per_million, markup_percent)
                VALUES (%s, 'workama-chat', 1, 2, 10)
                ON CONFLICT (workspace_id, model) DO NOTHING
                """,
                (workspace_id,),
            )
            await _write_audit(
                conn,
                org_id=actor.org_id,
                workspace_id=workspace_id,
                actor_user_id=actor.user_id,
                action="workspace.created",
                request_id=request_key,
                details={"slug": slug, "role": current["role"]},
            )
            row_result = await conn.execute(
                "SELECT id, org_id, name, slug, settings, status, created_by, created_at FROM id_workspace WHERE id=%s",
                (workspace_id,),
            )
            row = await row_result.fetchone()
            row["role"] = current["role"]
    return _workspace_response(row)


async def _issue_context_token(workspace_id: str, actor: Actor) -> dict[str, Any]:
    _require_user(actor)
    async with pool.connection() as conn:
        row = await _workspace_access(conn, actor, workspace_id)
        if not row:
            raise _resource_not_found()
        token = issue_workspace_token(actor.user_id, row["org_id"], row["id"], row["role"])
        payload = decode_workspace_token(token)
        await _write_audit(
            conn,
            org_id=row["org_id"],
            workspace_id=row["id"],
            actor_user_id=actor.user_id,
            action="workspace.context_token_issued",
            request_id=payload["jti"],
            details={"role": row["role"], "expires_at": payload["exp"]},
        )
        await conn.commit()
    return {
        "workspace_token": token,
        "token_type": "workspace_context",
        "expires_in": WORKSPACE_TOKEN_TTL_SECONDS,
        "workspace": _workspace_response(row),
    }


@router.post("/workspaces/{workspace_id}/context-token")
async def create_workspace_context_token(
    workspace_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    return await _issue_context_token(workspace_id, actor)


@router.post("/workspaces/{workspace_id}/switch")
async def switch_workspace(
    workspace_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    return await _issue_context_token(workspace_id, actor)


@router.post("/workspaces/context/exchange")
async def exchange_workspace_context(
    body: WorkspaceTokenRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    payload = decode_workspace_token(body.workspace_token)
    if actor.user_id != payload["sub"]:
        raise HTTPException(status_code=403, detail="Workspace token does not belong to this actor")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT w.id, w.org_id, w.name, w.slug, w.settings, w.status, m.role
            FROM id_workspace w
            JOIN id_member m ON m.workspace_id = w.id AND m.org_id = w.org_id AND m.user_id = %s
            WHERE w.id = %s AND w.org_id = %s AND w.status = 'active'
            """,
            (actor.user_id, payload["ws"], payload["org"]),
        )
        row = await result.fetchone()
        if not row or row["role"] != payload["role"]:
            raise _resource_not_found()
        access_token = create_access_token(actor.user_id, row["id"], row["role"], auth_strength=actor.auth_strength)
        await _write_audit(
            conn,
            org_id=row["org_id"],
            workspace_id=row["id"],
            actor_user_id=actor.user_id,
            action="workspace.context_exchanged",
            request_id=payload["jti"],
            details={"role": row["role"]},
        )
        await conn.commit()
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "workspace_id": row["id"],
        "org_id": row["org_id"],
        "role": row["role"],
        "expires_in": 15 * 60,
    }


@router.get("/workspaces/{workspace_id}/invitations")
async def list_invitations(
    workspace_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    async with pool.connection() as conn:
        access = await _workspace_access(conn, actor, workspace_id)
        if not access:
            raise _resource_not_found()
        _require_manager(access["role"])
        await conn.execute(
            "UPDATE id_invitation SET status='expired' WHERE workspace_id=%s AND status='pending' AND expires_at<=now()",
            (workspace_id,),
        )
        result = await conn.execute(
            """
            SELECT id, org_id, workspace_id, email_normalized, role, idempotency_key,
                   invited_by, expires_at, status, accepted_by, accepted_at,
                   revoked_at, created_at
            FROM id_invitation
            WHERE workspace_id=%s
            ORDER BY created_at DESC, id DESC
            """,
            (workspace_id,),
        )
        rows = await result.fetchall()
        await conn.commit()
    # Contract《720》listWorkspaceInvitations: ListQuery -> ListResponse<InvitationDTO>
    return {
        "items": rows,
        "data": rows,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None},
    }


@router.post("/workspaces/{workspace_id}/invitations", status_code=201)
async def create_invitation(
    workspace_id: str,
    body: InvitationCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
    idempotency_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    _require_user(actor)
    request_key = idempotency_header or body.idempotency_key
    email = normalize_email(str(body.email))
    async with pool.connection() as conn:
        access = await _workspace_access(conn, actor, workspace_id)
        if not access:
            raise _resource_not_found()
        _require_manager(access["role"])
        if not can_invite_role(access["role"], body.role):
            raise HTTPException(status_code=403, detail="Owner role cannot be granted by invitation")
        async with conn.transaction():
            await conn.execute(
                "UPDATE id_invitation SET status='expired' WHERE workspace_id=%s AND status='pending' AND expires_at<=now()",
                (workspace_id,),
            )
            if request_key:
                existing_result = await conn.execute(
                    "SELECT * FROM id_invitation WHERE workspace_id=%s AND idempotency_key=%s",
                    (workspace_id, request_key),
                )
                existing = await existing_result.fetchone()
                if existing:
                    if existing["email_normalized"] != email or existing["role"] != body.role:
                        raise HTTPException(status_code=409, detail="Idempotency key was used for a different invitation")
                    return {
                        "id": existing["id"],
                        "workspace_id": existing["workspace_id"],
                        "email": existing["email_normalized"],
                        "role": existing["role"],
                        "status": existing["status"],
                        "expires_at": existing["expires_at"],
                        "token": None,
                        "idempotent_replay": True,
                    }
            member_result = await conn.execute(
                "SELECT 1 FROM id_member WHERE org_id=%s AND workspace_id=%s AND user_id=(SELECT id FROM id_user WHERE email=%s)",
                (access["org_id"], workspace_id, email),
            )
            if await member_result.fetchone():
                raise HTTPException(status_code=409, detail="User is already a workspace member")
            active_result = await conn.execute(
                "SELECT id FROM id_invitation WHERE workspace_id=%s AND email_normalized=%s AND status='pending'",
                (workspace_id, email),
            )
            if await active_result.fetchone():
                raise HTTPException(status_code=409, detail="An active invitation already exists")
            token = secrets.token_urlsafe(32)
            invitation_id = new_id("inv")
            expires_at = datetime.now(UTC) + timedelta(seconds=body.expires_in_seconds)
            await conn.execute(
                """
                INSERT INTO id_invitation(
                    id, org_id, workspace_id, email_normalized, role, token_hash,
                    idempotency_key, invited_by, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    invitation_id,
                    access["org_id"],
                    workspace_id,
                    email,
                    body.role,
                    hash_secret(token),
                    request_key,
                    actor.user_id,
                    expires_at,
                ),
            )
            await _write_audit(
                conn,
                org_id=access["org_id"],
                workspace_id=workspace_id,
                invitation_id=invitation_id,
                actor_user_id=actor.user_id,
                action="workspace.invitation_created",
                request_id=request_key,
                details={"email": email, "role": body.role, "expires_at": expires_at.isoformat()},
            )
    return {
        "id": invitation_id,
        "workspace_id": workspace_id,
        "email": email,
        "role": body.role,
        "status": "pending",
        "expires_at": expires_at,
        "token": token,
        "idempotent_replay": False,
    }


@router.post("/invitations/{invitation_id}/accept")
async def accept_invitation(
    invitation_id: str,
    body: InvitationAcceptRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    email = normalize_email(actor.email)
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                SELECT i.*, w.name AS workspace_name, w.slug AS workspace_slug
                FROM id_invitation i
                JOIN id_workspace w ON w.id=i.workspace_id AND w.org_id=i.org_id
                WHERE i.id=%s AND i.email_normalized=%s
                FOR UPDATE
                """,
                (invitation_id, email),
            )
            invitation = await result.fetchone()
            if not invitation:
                raise _resource_not_found("Invitation")
            if invitation["status"] == "accepted":
                raise HTTPException(status_code=409, detail="Invitation has already been accepted")
            if invitation["status"] == "revoked":
                raise HTTPException(status_code=410, detail="Invitation has been revoked")
            if not invitation_is_active(invitation["status"], invitation["expires_at"]):
                await conn.execute("UPDATE id_invitation SET status='expired' WHERE id=%s", (invitation_id,))
                raise HTTPException(status_code=410, detail="Invitation is expired")
            if not secrets.compare_digest(invitation["token_hash"], hash_secret(body.token)):
                raise HTTPException(status_code=400, detail="Invitation token is invalid")
            existing_member_result = await conn.execute(
                "SELECT id, role FROM id_member WHERE org_id=%s AND workspace_id=%s AND user_id=%s FOR UPDATE",
                (invitation["org_id"], invitation["workspace_id"], actor.user_id),
            )
            existing_member = await existing_member_result.fetchone()
            if existing_member:
                await conn.execute(
                    "UPDATE id_invitation SET status='accepted', accepted_by=%s, accepted_at=now() WHERE id=%s",
                    (actor.user_id, invitation_id),
                )
                member_id = existing_member["id"]
                role = existing_member["role"]
            else:
                member_id = new_id("mem")
                await conn.execute(
                    """
                    INSERT INTO id_member(id, org_id, workspace_id, user_id, role, invited_by, joined_at)
                    VALUES (%s, %s, %s, %s, %s, %s, now())
                    """,
                    (
                        member_id,
                        invitation["org_id"],
                        invitation["workspace_id"],
                        actor.user_id,
                        invitation["role"],
                        invitation["invited_by"],
                    ),
                )
                await conn.execute(
                    "UPDATE id_invitation SET status='accepted', accepted_by=%s, accepted_at=now() WHERE id=%s",
                    (actor.user_id, invitation_id),
                )
                role = invitation["role"]
            await _write_audit(
                conn,
                org_id=invitation["org_id"],
                workspace_id=invitation["workspace_id"],
                invitation_id=invitation_id,
                actor_user_id=actor.user_id,
                action="workspace.invitation_accepted",
                details={"role": role},
            )
    workspace_token = issue_workspace_token(
        actor.user_id, invitation["org_id"], invitation["workspace_id"], role
    )
    return {
        "accepted": True,
        "invitation_id": invitation_id,
        "membership_id": member_id,
        "org_id": invitation["org_id"],
        "workspace_id": invitation["workspace_id"],
        "workspace_name": invitation["workspace_name"],
        "workspace_slug": invitation["workspace_slug"],
        "role": role,
        "workspace_token": workspace_token,
    }


@router.delete("/workspaces/{workspace_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    workspace_id: str,
    invitation_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    _require_user(actor)
    async with pool.connection() as conn:
        access = await _workspace_access(conn, actor, workspace_id)
        if not access:
            raise _resource_not_found()
        _require_manager(access["role"])
        async with conn.transaction():
            result = await conn.execute(
                "SELECT id, org_id, workspace_id, status FROM id_invitation WHERE id=%s AND workspace_id=%s AND org_id=%s FOR UPDATE",
                (invitation_id, workspace_id, access["org_id"]),
            )
            invitation = await result.fetchone()
            if not invitation:
                raise _resource_not_found("Invitation")
            if invitation["status"] == "accepted":
                raise HTTPException(status_code=409, detail="Accepted invitation cannot be revoked")
            if invitation["status"] == "pending":
                await conn.execute(
                    "UPDATE id_invitation SET status='revoked', revoked_at=now() WHERE id=%s",
                    (invitation_id,),
                )
                await _write_audit(
                    conn,
                    org_id=access["org_id"],
                    workspace_id=workspace_id,
                    invitation_id=invitation_id,
                    actor_user_id=actor.user_id,
                    action="workspace.invitation_revoked",
                )
    return Response(status_code=204)
