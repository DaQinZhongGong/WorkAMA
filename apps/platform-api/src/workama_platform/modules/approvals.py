from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from workama_platform.core import Actor, get_actor, json_dumps, new_id, pool, require_internal


router = APIRouter(prefix="/api/v1", tags=["approvals"])
internal_router = APIRouter(prefix="/internal/approvals", tags=["approvals-internal"])


class ApprovalCreate(BaseModel):
    workspace_id: str
    session_id: str
    call_id: str
    requester_id: str
    tool_name: str = Field(min_length=1, max_length=100)
    action_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    risk: Literal["A3", "A4"]
    preview: dict = Field(default_factory=dict)
    ttl_seconds: int = Field(default=120, ge=30, le=600)


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=500)


class ApprovalConsume(BaseModel):
    action_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class GrantCreate(BaseModel):
    tool_name: str = Field(min_length=1, max_length=100)
    scope: Literal["workspace", "session"] = "workspace"
    session_id: str | None = None
    max_risk: Literal["A1", "A2"] = "A2"
    expires_at: datetime | None = None


class RevokeReason(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def require_admin(actor: Actor) -> None:
    if actor.actor_type != "user" or actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Owner or admin user required")


async def expire_pending(conn, workspace_id: str | None = None) -> None:
    if workspace_id:
        await conn.execute("UPDATE ag_approval SET status='expired' WHERE workspace_id=%s AND status IN ('pending','approved') AND expires_at<=now()", (workspace_id,))
    else:
        await conn.execute("UPDATE ag_approval SET status='expired' WHERE status IN ('pending','approved') AND expires_at<=now()")


@internal_router.post("", dependencies=[Depends(require_internal)])
async def create_approval(body: ApprovalCreate):
    approval_id = new_id("apr")
    expires_at = datetime.now(UTC) + timedelta(seconds=body.ttl_seconds)
    async with pool.connection() as conn:
        valid = await conn.execute(
            "SELECT 1 FROM ag_session s JOIN id_member m ON m.workspace_id=s.workspace_id AND m.user_id=%s WHERE s.id=%s AND s.workspace_id=%s",
            (body.requester_id, body.session_id, body.workspace_id),
        )
        if not await valid.fetchone():
            raise HTTPException(status_code=404, detail="Session or requester not found")
        result = await conn.execute(
            """INSERT INTO ag_approval(id,workspace_id,session_id,call_id,requester_id,tool_name,action_hash,risk,preview,expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
               ON CONFLICT(workspace_id,call_id) DO NOTHING
               RETURNING *""",
            (approval_id, body.workspace_id, body.session_id, body.call_id, body.requester_id, body.tool_name, body.action_hash, body.risk, json_dumps(body.preview), expires_at),
        )
        row = await result.fetchone()
        if not row:
            existing = await conn.execute("SELECT * FROM ag_approval WHERE workspace_id=%s AND call_id=%s", (body.workspace_id, body.call_id))
            row = await existing.fetchone()
            if row["action_hash"] != body.action_hash:
                raise HTTPException(status_code=409, detail="Approval action hash cannot change")
        await conn.commit()
    return row


@internal_router.get("/{approval_id}", dependencies=[Depends(require_internal)])
async def internal_get_approval(approval_id: str):
    async with pool.connection() as conn:
        await expire_pending(conn)
        result = await conn.execute("SELECT * FROM ag_approval WHERE id=%s", (approval_id,))
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Approval not found")
    return row


@internal_router.post("/{approval_id}/consume", dependencies=[Depends(require_internal)])
async def consume_approval(approval_id: str, body: ApprovalConsume):
    async with pool.connection() as conn:
        await expire_pending(conn)
        result = await conn.execute(
            """UPDATE ag_approval SET status='consumed',consumed_at=now()
               WHERE id=%s AND status='approved' AND action_hash=%s AND expires_at>now() RETURNING *""",
            (approval_id, body.action_hash),
        )
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=412, detail="Approval is invalid, expired, consumed, or action changed")
    return row


@router.get("/approvals")
async def list_approvals(actor: Annotated[Actor, Depends(get_actor)], status: str | None = None):
    async with pool.connection() as conn:
        await expire_pending(conn, actor.workspace_id)
        if status is None:
            result = await conn.execute(
                "SELECT * FROM ag_approval WHERE workspace_id=%s ORDER BY created_at DESC LIMIT 100",
                (actor.workspace_id,),
            )
        else:
            result = await conn.execute(
                "SELECT * FROM ag_approval WHERE workspace_id=%s AND status=%s ORDER BY created_at DESC LIMIT 100",
                (actor.workspace_id, status),
            )
        rows = await result.fetchall()
        await conn.commit()
    # Contract《720》listApprovals: ListQuery -> ListResponse<ApprovalDTO>
    data = list(rows)
    return {
        "items": data,
        "data": data,
        "next_cursor": None,
        "has_more": False,
        "meta": {"request_id": None, "count": len(data)},
    }


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        await expire_pending(conn, actor.workspace_id)
        result = await conn.execute("SELECT * FROM ag_approval WHERE id=%s AND workspace_id=%s", (approval_id, actor.workspace_id))
        row = await result.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Approval not found")
    return row


@router.post("/approvals/{approval_id}/decisions")
async def decide_approval(approval_id: str, body: ApprovalDecision, actor: Annotated[Actor, Depends(get_actor)]):
    require_admin(actor)
    async with pool.connection() as conn:
        await expire_pending(conn, actor.workspace_id)
        current = await conn.execute("SELECT * FROM ag_approval WHERE id=%s AND workspace_id=%s FOR UPDATE", (approval_id, actor.workspace_id))
        row = await current.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Approval not found")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail="E07002: approval is no longer pending")
        if row["risk"] == "A4" and (row["requester_id"] == actor.user_id or actor.auth_strength < 2):
            raise HTTPException(status_code=403, detail="A4 requires another strongly authenticated approver")
        result = await conn.execute(
            "UPDATE ag_approval SET status=%s,reason=%s,decided_by=%s,decided_at=now() WHERE id=%s RETURNING *",
            (body.decision, body.reason, actor.user_id, approval_id),
        )
        decided = await result.fetchone()
        await conn.commit()
    return decided


@router.get("/tool-grants")
async def list_grants(actor: Annotated[Actor, Depends(get_actor)]):
    require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM ag_tool_grant WHERE workspace_id=%s ORDER BY created_at DESC", (actor.workspace_id,))
        # Contract《720》listToolGrants: ListQuery -> ListResponse<ToolGrantDTO>
        data = list(await result.fetchall())
        return {
            "items": data,
            "data": data,
            "next_cursor": None,
            "has_more": False,
            "meta": {"request_id": None, "count": len(data)},
        }


@router.post("/tool-grants", status_code=201)
async def create_grant(body: GrantCreate, actor: Annotated[Actor, Depends(get_actor)]):
    require_admin(actor)
    if body.scope == "session" and not body.session_id:
        raise HTTPException(status_code=422, detail="session_id is required for a session grant")
    if body.expires_at and body.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
    async with pool.connection() as conn:
        if body.session_id:
            valid = await conn.execute("SELECT 1 FROM ag_session WHERE id=%s AND workspace_id=%s", (body.session_id, actor.workspace_id))
            if not await valid.fetchone():
                raise HTTPException(status_code=404, detail="Session not found")
        result = await conn.execute(
            "INSERT INTO ag_tool_grant(id,workspace_id,tool_name,scope,session_id,max_risk,created_by,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (new_id("grt"), actor.workspace_id, body.tool_name, body.scope, body.session_id, body.max_risk, actor.user_id, body.expires_at),
        )
        row = await result.fetchone()
        await conn.commit()
    return row


@router.delete("/tool-grants/{grant_id}", status_code=204)
async def revoke_grant(grant_id: str, body: RevokeReason, actor: Annotated[Actor, Depends(get_actor)]):
    require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute("UPDATE ag_tool_grant SET revoked_at=now(),revoke_reason=%s WHERE id=%s AND workspace_id=%s AND revoked_at IS NULL RETURNING id", (body.reason, grant_id, actor.workspace_id))
        if not await result.fetchone():
            raise HTTPException(status_code=404, detail="Active grant not found")
        await conn.commit()
    return Response(status_code=204)
